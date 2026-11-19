"""
Copy the snapshot latest.json points at from the store (usually S3) to a local directory.

The copy mirrors the storage layout, so the destination is itself a valid local store
(`PIPELINE_STORAGE_URI=file://<dest>`): every table file the manifest lists, then the current
manifest.json, its revision file and latest.json (unless the local latest.json already
names a newer snapshot). `dest` may be a path or a file:// URI. Each step is logged. Metadata is written last, so an
interrupted copy never looks complete. Each file is checked against the manifest's
sha256; files already present with the right hash are skipped, so re-runs are cheap.
Only reads from the store; never calls a provider's pricing API.
"""

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import List
from urllib.parse import unquote, urlparse

from src.pipeline import layout
from src.pipeline import manifest as m
from src.pipeline.latest import read_latest
from src.pipeline.storage import Storage

log = logging.getLogger("pipeline.download_latest")


class LatestNotFound(Exception):
    """No latest.json, or it names a missing or purged snapshot (CLI exit code 3)."""


class ChecksumMismatch(Exception):
    """A downloaded file doesn't match the sha256 its manifest records."""


@dataclass
class LatestDownload:
    provider: str
    snapshot_date: str
    revision: int
    dest: str
    downloaded: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    bytes: int = 0

    def summary(self) -> dict:
        return {
            "provider": self.provider,
            "snapshot_date": self.snapshot_date,
            "revision": self.revision,
            "dest": self.dest,
            "downloaded": len(self.downloaded),
            "skipped": len(self.skipped),
            "bytes": self.bytes,
        }


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_bytes(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.part"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def resolve_dest(dest: str) -> str:
    """A plain path or a `file://` URI → absolute local path. Other schemes are refused."""
    parsed = urlparse(dest)
    if parsed.scheme == "file":
        return os.path.abspath(unquote(parsed.netloc + parsed.path))
    if parsed.scheme and len(parsed.scheme) > 1:  # one letter = a Windows drive
        raise ValueError(f"--dest must be a local path or file:// URI, got {dest!r}")
    return os.path.abspath(dest)


def _log(event: str, **fields) -> None:
    log.info(json.dumps({"event": event, **fields}, sort_keys=True))


def _local_pointer_is_newer(path: str, pointer: dict) -> bool:
    try:
        with open(path, "rb") as fh:
            local = json.load(fh)
    except (FileNotFoundError, ValueError):
        return False
    mine = (local.get("snapshot_date", ""), local.get("revision", 0))
    theirs = (pointer.get("snapshot_date", ""), pointer.get("revision", 0))
    return mine > theirs


def download_latest(store: Storage, provider: str, dest: str) -> LatestDownload:
    dest = resolve_dest(dest)
    _log("read_latest", source=store.uri, key=layout.latest_key(provider))
    pointer = read_latest(store, provider)
    if pointer is None:
        raise LatestNotFound(f"no latest snapshot published for {provider}")
    date = layout.validate_snapshot_date(pointer.get("snapshot_date", ""))
    manifest_key = layout.manifest_key(provider, date)
    _log("read_manifest", source=store.uri, key=manifest_key, snapshot_date=date)
    try:
        manifest_bytes = store.get_bytes(manifest_key)
    except FileNotFoundError:
        raise LatestNotFound(f"latest.json names {provider} {date}, but it has no manifest")
    manifest = m.Manifest.from_json(manifest_bytes.decode())
    if manifest.status == "purged":
        raise LatestNotFound(f"latest.json names {provider} {date}, but it has been purged")

    files = [f for t in manifest.tables.values() for r in t.regions.values() for f in r.files]
    _log("download_start", snapshot_date=date, revision=manifest.revision, files=len(files),
         bytes=sum(f.bytes for f in files), dest=dest)
    result = LatestDownload(provider, date, manifest.revision, dest)
    for n, f in enumerate(files, 1):
        target = os.path.join(dest, *f.path.split("/"))
        if os.path.exists(target) and os.path.getsize(target) == f.bytes and _sha256(target) == f.sha256:
            _log("skip", n=n, of=len(files), key=f.path, reason="already present, sha256 matches")
            result.skipped.append(target)
            continue
        _log("download", n=n, of=len(files), key=f.path, bytes=f.bytes)
        tmp = f"{target}.part"
        store.download_file(f.path, tmp)
        if _sha256(tmp) != f.sha256:
            os.remove(tmp)
            raise ChecksumMismatch(f"{f.path}: sha256 does not match the manifest")
        os.replace(tmp, target)
        result.downloaded.append(target)
        result.bytes += f.bytes

    revision_key = layout.revision_key(provider, date, manifest.revision)
    _log("read_revision", source=store.uri, key=revision_key)
    try:
        revision_bytes = store.get_bytes(revision_key)
    except FileNotFoundError:
        revision_bytes = manifest_bytes
    for key, body in ((revision_key, revision_bytes), (manifest_key, manifest_bytes)):
        _log("write_metadata", key=key)
        _write_bytes(os.path.join(dest, *key.split("/")), body)
    latest_path = os.path.join(dest, *layout.latest_key(provider).split("/"))
    if _local_pointer_is_newer(latest_path, pointer):
        _log("keep_local_latest", key=layout.latest_key(provider), reason="local latest.json names a newer snapshot")
    else:
        _log("write_metadata", key=layout.latest_key(provider))
        _write_bytes(latest_path, (json.dumps(pointer, sort_keys=True, indent=2) + "\n").encode())
    return result
