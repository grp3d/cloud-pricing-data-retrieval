"""
upload-history: publish one existing local table snapshot to the store (US8; FR-038–FR-042,
research R18). The operator chooses which snapshots to upload, one date at a time.

Sources, checked in this order:
- new layout (a local-directory run of this pipeline): `<source>/<provider>/manifests/<D>/
  manifest.json`, used as-is (FR-039)
- legacy layout (before feature 003): `<source>/pricing_aws/parquet/<table>/snapshot_date=<D>/
  region=<R>/*.parquet`, for which a `backfill` manifest is generated

Raw pricing data is never uploaded (FR-042). If anything for the date already exists in
the target, nothing is written unless `overwrite` is set, in which case the upload is
published as a new revision that supersedes the old data (FR-040). Order: data files,
then revision + manifest, then latest.json (FR-041).
"""

import datetime as dt
import glob
import hashlib
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pyarrow.parquet as pq

from src.pipeline import claims, layout
from src.pipeline.clock import check_clock_skew
from src.pipeline import manifest as m
from src.pipeline.latest import update_latest
from src.pipeline.storage import Storage

LEGACY_DIR = os.path.join("pricing_aws", "parquet")


class BackfillPrecondition(Exception):
    """Nothing was written: the target already has data, or there is no local data (exit 3)."""


@dataclass
class LocalFile:
    table: str
    region: str
    local_path: str


@dataclass
class LocalSnapshot:
    snapshot_date: str
    layout: str  # "manifest" | "legacy"
    root: str
    files: List[LocalFile] = field(default_factory=list)
    manifest: Optional[m.Manifest] = None

    @property
    def regions(self) -> List[str]:
        return sorted({f.region for f in self.files})


def find_local_snapshot(source: str, provider: str, snapshot_date: str) -> LocalSnapshot:
    layout.validate_snapshot_date(snapshot_date)
    manifest_path = os.path.join(source, *layout.manifest_key(provider, snapshot_date).split("/"))
    if os.path.isfile(manifest_path):
        with open(manifest_path, encoding="utf-8") as fh:
            man = m.Manifest.from_json(fh.read())
        files = [
            LocalFile(table, region, os.path.join(source, *f.path.split("/")))
            for table, entry in man.tables.items()
            for region, r in entry.regions.items()
            for f in r.files
        ]
        return LocalSnapshot(snapshot_date, "manifest", source, files, man)

    files = []
    for table in layout.TABLES:
        base = os.path.join(source, LEGACY_DIR, table, f"snapshot_date={snapshot_date}")
        for region_dir in sorted(glob.glob(os.path.join(base, "region=*"))):
            region = os.path.basename(region_dir)[len("region="):]
            for path in sorted(glob.glob(os.path.join(region_dir, "*.parquet"))):
                files.append(LocalFile(table, region, path))
    if not files:
        raise BackfillPrecondition(
            f"no local table data for {snapshot_date} under {source} "
            f"(looked for {provider}/manifests/{snapshot_date}/manifest.json and {LEGACY_DIR}/)"
        )
    return LocalSnapshot(snapshot_date, "legacy", source, files)


def _stats(path: str) -> Tuple[int, str, int]:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return os.path.getsize(path), digest.hexdigest(), pq.ParquetFile(path).metadata.num_rows


def build_backfill_manifest(
    snap: LocalSnapshot, settings, *, run_id: str, revision: int, now: dt.datetime
) -> Tuple[m.Manifest, List[Tuple[str, str]]]:
    """Returns the manifest to publish and the (local_path, key) uploads it needs."""
    provider = settings.provider
    uploads: List[Tuple[str, str]] = []

    if snap.layout == "manifest":
        # Keep the local manifest's contents (status, regions, checksums, row counts), but give
        # every file a fresh key under this upload's run ID: a published file is never
        # overwritten, even when the same source is uploaded again with --overwrite (FR-043).
        man = snap.manifest
        for table, entry in man.tables.items():
            for region, regional in entry.regions.items():
                for part, f in enumerate(regional.files):
                    key = layout.parquet_file_key(
                        provider, table, snap.snapshot_date, region, run_id,
                        part=part if len(regional.files) > 1 else None,
                    )
                    uploads.append((os.path.join(snap.root, *f.path.split("/")), key))
                    f.path = key
                regional.written_by_run = run_id
        man.run_id = run_id
        man.revision = revision
        man.previous_revision = revision - 1 if revision > 1 else None
        man.created_at = m.iso(now)  # the grace period for superseded files starts now
        man.raw = None if man.origin == "backfill" else {}  # raw data stays local (FR-042)
        return man, uploads

    per_region: Dict[str, Dict[str, List[LocalFile]]] = {}
    for f in snap.files:
        per_region.setdefault(f.region, {}).setdefault(f.table, []).append(f)

    outcomes = []
    for region in sorted(per_region):
        tables = {}
        for table in layout.TABLES:
            local_files = per_region[region].get(table, [])
            entries = []
            for i, lf in enumerate(local_files):
                key = layout.parquet_file_key(
                    provider, table, snap.snapshot_date, region, run_id, part=i if len(local_files) > 1 else None
                )
                size, sha, rows = _stats(lf.local_path)
                entries.append(m.DataFile(key, size, sha, rows))
                uploads.append((lf.local_path, key))
            tables[table] = m.RegionTableEntry(run_id, sum(e.row_count for e in entries), entries)
        outcomes.append(m.RegionOutcome(region, True, 0, tables=tables))

    man = m.build_revision(
        None,  # a backfill supersedes everything; nothing is carried forward
        provider=provider,
        snapshot_date=snap.snapshot_date,
        run_id=run_id,
        run_info=m.new_run_info(settings, "backfill", "backfill", now, now),
        outcomes=outcomes,
        requested_regions=snap.regions,
        created_at=now,
        origin="backfill",
        revision=revision,
    )
    return man, uploads


def _target_has_data(store: Storage, provider: str, date: str) -> bool:
    prefixes = [layout.parquet_date_prefix(provider, t, date) for t in layout.TABLES]
    prefixes.append(layout.manifest_date_prefix(provider, date))
    return any(next(iter(store.list(p)), None) is not None for p in prefixes)


def upload_history(
    store: Storage,
    settings,
    snapshot_date: str,
    source: str,
    *,
    overwrite: bool = False,
    dry_run: bool = False,
    now: Optional[dt.datetime] = None,
) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    check_clock_skew(store, settings, now)
    provider = settings.provider
    snap = find_local_snapshot(source, provider, snapshot_date)
    run_id = layout.new_run_id(now)

    if dry_run:
        revision = m.next_revision_number(store, provider, snapshot_date)
        man, uploads = build_backfill_manifest(snap, settings, run_id=run_id, revision=revision, now=now)
        return {
            "outcome": "dry-run",
            "snapshot_date": snapshot_date,
            "source_layout": snap.layout,
            "target_has_data": _target_has_data(store, provider, snapshot_date),
            "manifest": man.to_dict(),
            "files": [key for _, key in uploads],
        }

    if not overwrite and _target_has_data(store, provider, snapshot_date):
        raise BackfillPrecondition(
            f"data for {snapshot_date} already exists in {store.uri}; use --overwrite to replace it"
        )

    try:
        with claims.held_claim(
            store, provider, snapshot_date, run_id, "backfill", settings.run_claim_ttl_minutes, now
        ):
            # Re-check under the claim: another run may have published this date since the
            # check above (FR-040).
            if not overwrite and _target_has_data(store, provider, snapshot_date):
                raise BackfillPrecondition(
                    f"data for {snapshot_date} already exists in {store.uri}; use --overwrite to replace it"
                )
            revision = m.next_revision_number(store, provider, snapshot_date)
            man, uploads = build_backfill_manifest(snap, settings, run_id=run_id, revision=revision, now=now)
            for local_path, key in uploads:  # data files first (FR-041)
                store.put_file(key, local_path)
            m.write_revision(store, man)
            moved = update_latest(store, man, now)
    except claims.ClaimHeld as held:
        return {"outcome": "refused", "snapshot_date": snapshot_date, "refused_by": held.holder_run_id}

    return {
        "outcome": "uploaded",
        "snapshot_date": snapshot_date,
        "source_layout": snap.layout,
        "revision": man.revision,
        "status": man.status,
        "files_uploaded": len(uploads),
        "latest_moved": moved,
    }
