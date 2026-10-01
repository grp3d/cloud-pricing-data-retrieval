"""
latest.json pointer, updated with compare-and-swap (FR-012, FR-014, research R7).

It only ever names a `succeeded` snapshot, and never moves to an older snapshot date
(for the same date, only to a higher revision), even when runs for different dates race.
"""

import datetime as dt
import json
from typing import Optional

from src.pipeline import layout
from src.pipeline.manifest import MANIFEST_VERSION, Manifest, iso
from src.pipeline.storage import PreconditionFailed, Storage

MAX_ATTEMPTS = 5


class LatestUpdateConflict(Exception):
    """latest.json kept changing underneath us; gave up after MAX_ATTEMPTS."""


def read_latest(store: Storage, provider: str) -> Optional[dict]:
    try:
        return json.loads(store.get_bytes(layout.latest_key(provider)))
    except FileNotFoundError:
        return None


def _pointer(manifest: Manifest, now: dt.datetime) -> bytes:
    return json.dumps(
        {
            "manifest_version": MANIFEST_VERSION,
            "provider": manifest.provider,
            "snapshot_date": manifest.snapshot_date,
            "revision": manifest.revision,
            "run_id": manifest.run_id,
            "manifest_path": layout.manifest_key(manifest.provider, manifest.snapshot_date),
            "updated_at": iso(now),
        },
        sort_keys=True,
        indent=2,
    ).encode() + b"\n"


def _is_newer(manifest: Manifest, current: dict) -> bool:
    if manifest.snapshot_date != current.get("snapshot_date"):
        return manifest.snapshot_date > current.get("snapshot_date", "")
    return manifest.revision > current.get("revision", 0)


def update_latest(store: Storage, manifest: Manifest, now: dt.datetime) -> bool:
    """Point latest.json at `manifest` if it is succeeded and newer. Returns True if moved."""
    if manifest.status != "succeeded":
        return False
    key = layout.latest_key(manifest.provider)
    body = _pointer(manifest, now)
    for _ in range(MAX_ATTEMPTS):
        info = store.head(key)
        try:
            if info is None:
                store.put_if_absent(key, body)
                return True
            current = json.loads(store.get_bytes(key))
            if not _is_newer(manifest, current):
                return False
            store.put_if_match(key, body, info.etag)
            return True
        except (PreconditionFailed, FileNotFoundError):
            continue  # another writer got there first: re-read and decide again
    raise LatestUpdateConflict(f"could not update {key} after {MAX_ATTEMPTS} attempts")
