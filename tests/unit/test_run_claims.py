"""Per-snapshot-date run claim (FR-005, research R5), local and S3."""

import json
import threading

import pytest

from src.pipeline import claims, layout

DATE = "2026-10-05"
RUN_A = "20261005T130000Z-aaaaaa"
RUN_B = "20261005T130100Z-bbbbbb"


def _acquire(store, run_id, clock, ttl=180):
    return claims.acquire(store, "aws", DATE, run_id, "manual", ttl_minutes=ttl, now=clock())


def test_acquire_writes_claim_document(store, frozen_clock):
    claim = _acquire(store, RUN_A, frozen_clock)
    doc = json.loads(store.get_bytes(layout.claim_key("aws", DATE)))
    assert doc["run_id"] == RUN_A
    assert doc["trigger"] == "manual"
    assert doc["acquired_at"] == "2026-10-05T13:00:00Z"
    assert doc["expires_at"] == "2026-10-05T16:00:00Z"
    assert claim.run_id == RUN_A


def test_second_acquire_is_refused_while_unexpired(store, frozen_clock):
    _acquire(store, RUN_A, frozen_clock)
    frozen_clock.advance(minutes=179)
    with pytest.raises(claims.ClaimHeld) as info:
        _acquire(store, RUN_B, frozen_clock)
    assert info.value.holder_run_id == RUN_A


def test_expired_claim_is_taken_over(store, frozen_clock):
    _acquire(store, RUN_A, frozen_clock)
    frozen_clock.advance(minutes=181)
    claim = _acquire(store, RUN_B, frozen_clock)
    assert claim.run_id == RUN_B
    doc = json.loads(store.get_bytes(layout.claim_key("aws", DATE)))
    assert doc["run_id"] == RUN_B


def test_concurrent_takeover_only_one_wins(store, frozen_clock, monkeypatch):
    """Two acquirers read the same expired claim; the loser's put_if_match fails."""
    _acquire(store, RUN_A, frozen_clock)
    frozen_clock.advance(minutes=200)
    stale = store.head(layout.claim_key("aws", DATE)).etag

    # Winner takes over first...
    _acquire(store, RUN_B, frozen_clock)
    # ...then a racer that read the old etag tries to replace it.
    racer = "20261005T160000Z-cccccc"
    with pytest.raises(claims.ClaimHeld):
        claims._take_over(store, "aws", DATE, racer, "manual", 180, frozen_clock(), stale)


def test_release_only_by_owner(store, frozen_clock):
    _acquire(store, RUN_A, frozen_clock)
    claims.release(store, "aws", DATE, RUN_B)  # not the owner: no-op
    assert store.head(layout.claim_key("aws", DATE)) is not None
    claims.release(store, "aws", DATE, RUN_A)
    assert store.head(layout.claim_key("aws", DATE)) is None


def test_held_claim_releases_on_exception(store, frozen_clock):
    with pytest.raises(RuntimeError):
        with claims.held_claim(store, "aws", DATE, RUN_A, "manual", 180, frozen_clock()):
            assert store.head(layout.claim_key("aws", DATE)) is not None
            raise RuntimeError("boom")
    assert store.head(layout.claim_key("aws", DATE)) is None


def test_try_acquire_returns_none_when_busy(store, frozen_clock):
    _acquire(store, RUN_A, frozen_clock)
    assert claims.try_acquire(store, "aws", DATE, RUN_B, "retention", 180, frozen_clock()) is None
    claims.release(store, "aws", DATE, RUN_A)
    got = claims.try_acquire(store, "aws", DATE, RUN_B, "retention", 180, frozen_clock())
    assert got is not None and got.run_id == RUN_B


def test_simultaneous_acquire_exactly_one_wins(local_store, frozen_clock):
    results = []

    def worker(run_id):
        try:
            _acquire(local_store, run_id, frozen_clock)
            results.append(("won", run_id))
        except claims.ClaimHeld:
            results.append(("refused", run_id))

    threads = [
        threading.Thread(target=worker, args=(f"20261005T13000{i}Z-00000{i}",)) for i in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if r[0] == "won") == 1
