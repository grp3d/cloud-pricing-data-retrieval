"""
Per-snapshot-date run claim (FR-005, research R5).

A claim is a small JSON object at `<provider>/claims/<date>.json`, created with a
conditional write so only one run per date can hold it. A second run is refused
while the claim is unexpired; an expired claim (a crashed run) is taken over with a
compare-and-swap on its etag, so exactly one of several racing runs wins.
"""

import datetime as dt
import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

from src.pipeline import layout
from src.pipeline.storage import PreconditionFailed, Storage


class ClaimHeld(Exception):
    """Another run holds the claim for this snapshot date."""

    def __init__(self, snapshot_date: str, holder_run_id: Optional[str]):
        super().__init__(f"run already in progress for {snapshot_date} (run_id={holder_run_id})")
        self.snapshot_date = snapshot_date
        self.holder_run_id = holder_run_id


@dataclass(frozen=True)
class RunClaim:
    provider: str
    snapshot_date: str
    run_id: str
    trigger: str
    acquired_at: dt.datetime
    expires_at: dt.datetime


def _iso(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> dt.datetime:
    return dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def _document(claim: RunClaim) -> bytes:
    return json.dumps(
        {
            "run_id": claim.run_id,
            "trigger": claim.trigger,
            "acquired_at": _iso(claim.acquired_at),
            "expires_at": _iso(claim.expires_at),
        },
        sort_keys=True,
    ).encode()


def _new_claim(provider, snapshot_date, run_id, trigger, ttl_minutes, now) -> RunClaim:
    return RunClaim(
        provider=provider,
        snapshot_date=snapshot_date,
        run_id=run_id,
        trigger=trigger,
        acquired_at=now,
        expires_at=now + dt.timedelta(minutes=ttl_minutes),
    )


def _take_over(store, provider, snapshot_date, run_id, trigger, ttl_minutes, now, etag) -> RunClaim:
    key = layout.claim_key(provider, snapshot_date)
    claim = _new_claim(provider, snapshot_date, run_id, trigger, ttl_minutes, now)
    try:
        store.put_if_match(key, _document(claim), etag)
    except PreconditionFailed:
        raise ClaimHeld(snapshot_date, _holder(store, key))
    return claim


def _holder(store: Storage, key: str) -> Optional[str]:
    try:
        return json.loads(store.get_bytes(key)).get("run_id")
    except (FileNotFoundError, ValueError):
        return None


def acquire(
    store: Storage,
    provider: str,
    snapshot_date: str,
    run_id: str,
    trigger: str,
    ttl_minutes: int,
    now: dt.datetime,
) -> RunClaim:
    """Acquire the date's claim or raise ClaimHeld."""
    key = layout.claim_key(provider, snapshot_date)
    claim = _new_claim(provider, snapshot_date, run_id, trigger, ttl_minutes, now)
    try:
        store.put_if_absent(key, _document(claim))
        return claim
    except PreconditionFailed:
        pass

    info = store.head(key)
    if info is None:  # released between our write and head: try once more
        try:
            store.put_if_absent(key, _document(claim))
            return claim
        except PreconditionFailed:
            raise ClaimHeld(snapshot_date, _holder(store, key))
    try:
        current = json.loads(store.get_bytes(key))
        expires_at = _parse(current["expires_at"])
    except (FileNotFoundError, ValueError, KeyError):
        expires_at = now  # unreadable claim: treat as expired
        current = {}
    if expires_at > now:
        raise ClaimHeld(snapshot_date, current.get("run_id"))
    return _take_over(store, provider, snapshot_date, run_id, trigger, ttl_minutes, now, info.etag)


def try_acquire(
    store: Storage,
    provider: str,
    snapshot_date: str,
    run_id: str,
    trigger: str,
    ttl_minutes: int,
    now: dt.datetime,
) -> Optional[RunClaim]:
    """Like acquire(), but returns None instead of raising when the date is busy."""
    try:
        return acquire(store, provider, snapshot_date, run_id, trigger, ttl_minutes, now)
    except ClaimHeld:
        return None


def release(store: Storage, provider: str, snapshot_date: str, run_id: str) -> None:
    """Delete the claim if (and only if) this run still owns it.

    The delete is conditional on the version this run just read, so if another run took
    the claim over in between (possible once this run has outlived its TTL), the delete
    fails and the other run's claim stays in place.
    """
    key = layout.claim_key(provider, snapshot_date)
    info = store.head(key)
    if info is None or _holder(store, key) != run_id:
        return
    try:
        store.delete_if_match(key, info.etag)
    except PreconditionFailed:
        pass  # replaced since we looked: it's no longer ours to delete


@contextmanager
def held_claim(
    store: Storage,
    provider: str,
    snapshot_date: str,
    run_id: str,
    trigger: str,
    ttl_minutes: int,
    now: dt.datetime,
) -> Iterator[RunClaim]:
    claim = acquire(store, provider, snapshot_date, run_id, trigger, ttl_minutes, now)
    try:
        yield claim
    finally:
        release(store, provider, snapshot_date, run_id)
