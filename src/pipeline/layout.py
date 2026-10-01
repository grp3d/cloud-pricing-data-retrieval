"""
Storage key builders — the single source of truth for contracts/storage-layout.md.

    <provider>/raw/<date>/<region>/<run_id>/pricing-<Service>-<region>.json.zst
    <provider>/parquet/<table>/snapshot_date=<date>/region=<region>/part-<run_id>[-<n>].parquet
    <provider>/manifests/latest.json
    <provider>/manifests/<date>/manifest.json
    <provider>/manifests/<date>/revisions/<NNNN>.json
    <provider>/claims/<date>.json

Inputs are validated so a malformed value can never resolve to another snapshot's keys.
"""

import datetime as dt
import re
import secrets
from typing import Optional

TABLES = ("service_dim", "product_dim", "product_attribute", "region_dim", "price_fact")

_PROVIDER_RE = re.compile(r"[a-z0-9]+")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_REGION_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)+")
_RUN_ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}")
_REVISION_KEY_RE = re.compile(r".*/revisions/([0-9]{4,})\.json")


def _provider(value: str) -> str:
    if not _PROVIDER_RE.fullmatch(value or ""):
        raise ValueError(f"Invalid provider {value!r}")
    return value


def _date(value: str) -> str:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise ValueError(f"Invalid snapshot_date {value!r}; expected YYYY-MM-DD")
    dt.date.fromisoformat(value)
    return value


def _region(value: str) -> str:
    if not _REGION_RE.fullmatch(value or ""):
        raise ValueError(f"Invalid region {value!r}")
    return value


def _run_id(value: str) -> str:
    if not _RUN_ID_RE.fullmatch(value or ""):
        raise ValueError(f"Invalid run_id {value!r}")
    return value


def _table(value: str) -> str:
    if value not in TABLES:
        raise ValueError(f"Invalid table {value!r}")
    return value


def validate_snapshot_date(value: str) -> str:
    return _date(value)


def validate_region(value: str) -> str:
    return _region(value)


def new_run_id(now: dt.datetime) -> str:
    """Sortable, unique run identifier: `<UTC yyyymmddThhmmssZ>-<6 hex>`."""
    stamp = now.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


# --- raw -----------------------------------------------------------------------------

def raw_prefix(provider: str) -> str:
    return f"{_provider(provider)}/raw/"


def raw_date_prefix(provider: str, snapshot_date: str) -> str:
    return f"{raw_prefix(provider)}{_date(snapshot_date)}/"


def raw_run_prefix(provider: str, snapshot_date: str, region: str, run_id: str) -> str:
    return f"{raw_date_prefix(provider, snapshot_date)}{_region(region)}/{_run_id(run_id)}/"


def raw_file_key(provider: str, snapshot_date: str, region: str, run_id: str, file_name: str) -> str:
    if "/" in file_name or not file_name:
        raise ValueError(f"Invalid raw file name {file_name!r}")
    name = file_name if file_name.endswith(".zst") else f"{file_name}.zst"
    return raw_run_prefix(provider, snapshot_date, region, run_id) + name


# --- parquet -------------------------------------------------------------------------

def parquet_table_prefix(provider: str, table: str) -> str:
    return f"{_provider(provider)}/parquet/{_table(table)}/"


def parquet_date_prefix(provider: str, table: str, snapshot_date: str) -> str:
    return f"{parquet_table_prefix(provider, table)}snapshot_date={_date(snapshot_date)}/"


def parquet_file_key(
    provider: str, table: str, snapshot_date: str, region: str, run_id: str, part: Optional[int] = None
) -> str:
    suffix = f"-{part}" if part is not None else ""
    return (
        f"{parquet_date_prefix(provider, table, snapshot_date)}region={_region(region)}/"
        f"part-{_run_id(run_id)}{suffix}.parquet"
    )


# --- manifests, pointer, claims ------------------------------------------------------

def manifests_prefix(provider: str) -> str:
    return f"{_provider(provider)}/manifests/"


def manifest_date_prefix(provider: str, snapshot_date: str) -> str:
    return f"{manifests_prefix(provider)}{_date(snapshot_date)}/"


def manifest_key(provider: str, snapshot_date: str) -> str:
    return f"{manifest_date_prefix(provider, snapshot_date)}manifest.json"


def revision_key(provider: str, snapshot_date: str, revision: int) -> str:
    if not isinstance(revision, int) or revision < 1:
        raise ValueError(f"Invalid revision {revision!r}")
    return f"{manifest_date_prefix(provider, snapshot_date)}revisions/{revision:04d}.json"


def parse_revision_key(key: str) -> Optional[int]:
    match = _REVISION_KEY_RE.fullmatch(key)
    return int(match.group(1)) if match else None


def latest_key(provider: str) -> str:
    return f"{manifests_prefix(provider)}latest.json"


def claim_key(provider: str, snapshot_date: str) -> str:
    return f"{_provider(provider)}/claims/{_date(snapshot_date)}.json"
