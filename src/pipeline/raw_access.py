"""
Owner access to raw snapshots before they are purged (FR-025): list them with their
purge dates, and download one to a local directory (optionally decompressed).
"""

import datetime as dt
import os
from compression import zstd
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from src.pipeline import layout
from src.pipeline import manifest as m
from src.pipeline.storage import Storage


class RawNotFound(Exception):
    """No raw data is stored for the requested snapshot (CLI exit code 3)."""


@dataclass
class RawSnapshot:
    snapshot_date: str
    region: str
    run_id: str
    file_count: int
    bytes: int
    stored_at: str
    purge_after: str


def _parse_key(provider: str, key: str) -> Optional[Tuple[str, str, str]]:
    parts = key[len(layout.raw_prefix(provider)):].split("/")
    if len(parts) != 4:
        return None
    return parts[0], parts[1], parts[2]


def list_raw(store: Storage, provider: str, raw_retention_days: int) -> List[RawSnapshot]:
    groups: Dict[Tuple[str, str, str], List] = {}
    for obj in store.list(layout.raw_prefix(provider)):
        ident = _parse_key(provider, obj.key)
        if ident:
            groups.setdefault(ident, []).append(obj)

    recorded: Dict[str, m.RawEntry] = {}
    for date in sorted({d for d, _, _ in groups}):
        man = m.read_current(store, provider, date)
        for entry in (man.raw or {}).values() if man else []:
            recorded[entry.location] = entry

    rows = []
    for (date, region, run_id), objs in sorted(groups.items()):
        entry = recorded.get(layout.raw_run_prefix(provider, date, region, run_id))
        if entry:
            stored_at, purge_after = entry.stored_at, entry.purge_after
        else:
            uploaded = max(o.last_modified for o in objs)
            fallback = m.make_raw_entry("", len(objs), 0, uploaded, raw_retention_days)
            stored_at, purge_after = fallback.stored_at, fallback.purge_after
        rows.append(
            RawSnapshot(
                snapshot_date=date,
                region=region,
                run_id=run_id,
                file_count=len(objs),
                bytes=sum(o.size for o in objs),
                stored_at=stored_at,
                purge_after=purge_after,
            )
        )
    return rows


def download_raw(
    store: Storage,
    provider: str,
    snapshot_date: str,
    dest: str,
    regions: Optional[List[str]] = None,
    decompress: bool = False,
) -> List[str]:
    """Copy one snapshot's raw files to `dest/<region>/`. Uses the runs the current
    manifest records; without a manifest, every stored run for the date."""
    man = m.read_current(store, provider, snapshot_date)
    if man and man.raw:
        prefixes = {r: e.location for r, e in man.raw.items()}
    else:
        prefixes = {}
        for obj in store.list(layout.raw_date_prefix(provider, snapshot_date)):
            ident = _parse_key(provider, obj.key)
            if ident:
                prefixes.setdefault(ident[1], layout.raw_run_prefix(provider, *ident))
    if regions:
        prefixes = {r: p for r, p in prefixes.items() if r in regions}

    written = []
    for region, prefix in sorted(prefixes.items()):
        for obj in store.list(prefix):
            target = os.path.join(dest, region, os.path.basename(obj.key))
            store.download_file(obj.key, target)
            if decompress and target.endswith(".zst"):
                plain = target[: -len(".zst")]
                with zstd.open(target, "rb") as fin, open(plain, "wb") as fout:
                    while chunk := fin.read(1 << 20):
                        fout.write(chunk)
                os.remove(target)
                target = plain
            written.append(target)
    if not written:
        raise RawNotFound(f"no raw data stored for {provider} {snapshot_date}")
    return written
