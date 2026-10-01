"""
Retention (FR-020–FR-024, FR-045, FR-046, FR-048–FR-050; research R10).

Runs as the last step of every pipeline run and on demand (`retention [--dry-run]`):

1. Thinning: snapshots older than PARQUET_WEEKLY_RETENTION_MONTHS keep only the earliest
   `succeeded` snapshot per calendar month (UTC); the rest are purged. The date latest.json
   names is never purged.
2. Purge: rewrite the date's manifest as a `purged` revision first, then delete its files.
3. Superseded files (listed by an earlier revision, not the current one) are deleted once
   the grace period has passed since the current revision; orphans (never listed) once
   the grace period has passed since their upload. For the run's own date, if the grace
   period is within the inline-wait limit, the step waits out the remainder.
4. Local roots only: raw files older than RAW_RETENTION_DAYS (S3 uses lifecycle rules).

Guard (FR-046): immediately before every delete, the date's current manifest is re-read,
and a file it lists is never deleted. Other dates are only touched while holding their
run claim; busy dates are skipped.

plan_retention() computes everything and changes nothing (the dry run); apply_retention()
carries the plan out.
"""

import calendar
import datetime as dt
import json
import logging
from typing import Callable, Dict, List, Optional, Set

from src.pipeline import claims, layout
from src.pipeline import manifest as m
from src.pipeline.latest import read_latest
from src.pipeline.storage import Storage

logger = logging.getLogger(__name__)

KEEP, DELETE, WAIT = "keep", "delete", "wait"


def _add_months(day: dt.date, months: int) -> dt.date:
    index = day.year * 12 + (day.month - 1) + months
    year, month0 = divmod(index, 12)
    month = month0 + 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _parquet_dates(store: Storage, provider: str) -> Set[str]:
    dates = set()
    for table in layout.TABLES:
        for obj in store.list(layout.parquet_table_prefix(provider, table)):
            for part in obj.key.split("/"):
                if part.startswith("snapshot_date="):
                    dates.add(part[len("snapshot_date="):])
    return dates


def _raw_date(provider: str, key: str) -> str:
    """Snapshot date of a raw key: <provider>/raw/<date>/<region>/<run_id>/<file>."""
    return key[len(layout.raw_prefix(provider)):].split("/", 1)[0]


def _date_files(store: Storage, provider: str, date: str):
    for table in layout.TABLES:
        yield from store.list(layout.parquet_date_prefix(provider, table, date))


def _ever_listed(store: Storage, provider: str, date: str) -> Set[str]:
    paths: Set[str] = set()
    prefix = layout.manifest_date_prefix(provider, date) + "revisions/"
    for obj in store.list(prefix):
        paths |= m.active_file_paths(m.Manifest.from_json(store.get_bytes(obj.key).decode()))
    return paths


def _held_by_other(store: Storage, provider: str, date: str, now: dt.datetime) -> Optional[str]:
    """run_id of an unexpired claim on `date`, without acquiring it."""
    try:
        doc = json.loads(store.get_bytes(layout.claim_key(provider, date)))
    except FileNotFoundError:
        return None
    try:
        if m.parse_iso(doc["expires_at"]) <= now:
            return None
    except (KeyError, ValueError):
        return None
    return doc.get("run_id")


def plan_retention(
    store: Storage, settings, now: dt.datetime, *, run_id: str, own_date: Optional[str] = None
) -> dict:
    provider = settings.provider
    grace = dt.timedelta(minutes=settings.superseded_file_grace_minutes)
    inline_ok = settings.superseded_file_grace_minutes <= settings.superseded_file_inline_wait_max_minutes

    manifest_dates = m.list_snapshot_dates(store, provider)
    current: Dict[str, m.Manifest] = {d: m.read_current(store, provider, d) for d in manifest_dates}
    latest = read_latest(store, provider)
    latest_date = latest["snapshot_date"] if latest else None

    # Local raw files past their retention, by snapshot date (S3 raw data is expired by
    # lifecycle rules instead). Collected first so busy dates cover them too.
    raw_expired: Dict[str, List[str]] = {}
    if store.is_local:
        max_age = dt.timedelta(days=settings.raw_retention_days)
        for obj in store.list(layout.raw_prefix(provider)):
            if obj.last_modified + max_age < now:
                raw_expired.setdefault(_raw_date(provider, obj.key), []).append(obj.key)

    busy = {
        d: holder
        for d in set(manifest_dates) | _parquet_dates(store, provider) | set(raw_expired)
        if d != own_date and (holder := _held_by_other(store, provider, d, now))
    }

    # 1. thinning
    cutoff = _add_months(now.astimezone(dt.timezone.utc).date(), -settings.parquet_weekly_retention_months)
    by_month: Dict[str, List[str]] = {}
    for d in manifest_dates:
        if d < cutoff.isoformat():
            by_month.setdefault(d[:7], []).append(d)
    keep_monthly, purge = [], []
    for month in sorted(by_month):
        dates = sorted(by_month[month])
        keep = next((d for d in dates if current[d].status == "succeeded"), None)
        if keep:
            keep_monthly.append({"month": month, "snapshot_date": keep})
        for d in dates:
            if d == keep or d == latest_date or current[d].status == "purged" or d in busy:
                continue
            purge.append({"snapshot_date": d, "reason": "retention-thinning"})
    purge_dates = {p["snapshot_date"] for p in purge}

    # 2. superseded files and orphans
    superseded, orphans = [], []
    for d in sorted((set(manifest_dates) | _parquet_dates(store, provider)) - purge_dates):
        if d in busy:
            continue
        cur = current.get(d)
        active = m.active_file_paths(cur)
        listed = None
        for obj in _date_files(store, provider, d):
            if obj.key in active:
                continue
            if listed is None:
                listed = _ever_listed(store, provider, d)
            if obj.key in listed and cur is not None:
                eligible_at = m.parse_iso(cur.created_at) + grace
                bucket = superseded
            else:
                eligible_at = obj.last_modified + grace
                bucket = orphans
            if now >= eligible_at:
                action = DELETE
            elif d == own_date and inline_ok:
                action = WAIT
            else:
                action = KEEP
            bucket.append(
                {"path": obj.key, "snapshot_date": d, "eligible_at": m.iso(eligible_at), "action": action}
            )

    # 3. local raw expiry, skipping dates another run is using (e.g. a transform-only run
    #    reading that date's raw files)
    raw_local = sorted(key for d, keys in raw_expired.items() if d not in busy for key in keys)

    return {
        "dry_run": True,
        "superseded": superseded,
        "orphans": orphans,
        "purge_snapshots": purge,
        "keep_monthly": keep_monthly,
        "raw_local": raw_local,
        "skipped_busy_dates": sorted(busy),
    }


def apply_retention(
    store: Storage,
    settings,
    plan: dict,
    now: dt.datetime,
    *,
    sleep: Callable[[float], None],
    run_id: str,
    own_date: Optional[str] = None,
) -> dict:
    provider = settings.provider
    result = dict(plan, dry_run=False, deleted=[], raw_deleted=[], skipped_guard=[], waited_seconds=0)
    result["skipped_busy_dates"] = list(plan["skipped_busy_dates"])

    files = [i for i in plan["superseded"] + plan["orphans"] if i["action"] != KEEP]
    waits = [i for i in files if i["action"] == WAIT]
    if waits:
        remaining = max((m.parse_iso(i["eligible_at"]) - now).total_seconds() for i in waits)
        if remaining > 0:
            sleep(remaining)
            result["waited_seconds"] = remaining

    by_date: Dict[str, List[dict]] = {}
    for item in files:
        by_date.setdefault(item["snapshot_date"], []).append(item)
    purge_dates = [p["snapshot_date"] for p in plan["purge_snapshots"]]
    for d in purge_dates:
        by_date.setdefault(d, [])
    raw_by_date: Dict[str, List[str]] = {}
    for key in plan["raw_local"]:
        raw_by_date.setdefault(_raw_date(provider, key), []).append(key)
    for d in raw_by_date:
        by_date.setdefault(d, [])

    def guarded_delete(date: str, key: str) -> None:
        if key in m.active_file_paths(m.read_current(store, provider, date)):
            result["skipped_guard"].append(key)
            logger.warning(f"retention guard: {key} is referenced again; not deleted")
            return
        store.delete(key)
        result["deleted"].append(key)

    for d in sorted(by_date):
        claim = None
        if d != own_date:
            claim = claims.try_acquire(
                store, provider, d, run_id, "retention", settings.run_claim_ttl_minutes, now
            )
            if claim is None:
                result["skipped_busy_dates"].append(d)
                continue
        try:
            if d in purge_dates:
                latest = read_latest(store, provider)
                cur = m.read_current(store, provider, d)
                if cur is not None and cur.status != "purged" and (not latest or latest["snapshot_date"] != d):
                    purged = m.build_purged_revision(
                        cur, now, revision=m.next_revision_number(store, provider, d)
                    )
                    m.write_revision(store, purged)  # manifest first, then deletes (FR-022)
                    for obj in list(_date_files(store, provider, d)):
                        guarded_delete(d, obj.key)
            for item in by_date[d]:
                guarded_delete(d, item["path"])
            for key in raw_by_date.get(d, []):  # under the same claim as the date's other files
                store.delete(key)
                result["raw_deleted"].append(key)
        finally:
            if claim is not None:
                claims.release(store, provider, d, run_id)

    result["skipped_busy_dates"] = sorted(set(result["skipped_busy_dates"]))
    return result


def run_retention(
    store: Storage,
    settings,
    *,
    now: dt.datetime,
    sleep: Callable[[float], None],
    run_id: str,
    own_date: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    plan = plan_retention(store, settings, now, run_id=run_id, own_date=own_date)
    if dry_run:
        return plan
    return apply_retention(store, settings, plan, now, sleep=sleep, run_id=run_id, own_date=own_date)


def summarize(result: dict) -> dict:
    """The RunReport.retention summary (data-model.md)."""
    deleted = set(result.get("deleted", []))
    return {
        "superseded_deleted": sum(1 for i in result["superseded"] if i["path"] in deleted),
        "orphans_deleted": sum(1 for i in result["orphans"] if i["path"] in deleted),
        "snapshots_purged": len(result["purge_snapshots"]),
        "raw_deleted_local": len(result.get("raw_deleted", [])),
        "waited_seconds": result.get("waited_seconds", 0),
        "skipped_busy_dates": result.get("skipped_busy_dates", []),
        "skipped_guard": len(result.get("skipped_guard", [])),
    }
