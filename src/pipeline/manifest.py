"""
Snapshot manifest: model, revision building, status derivation and persistence
(data-model.md, contracts/manifest.schema.json; FR-013–FR-017, FR-022, FR-043).

Each revision is immutable (`revisions/<NNNN>.json`) and the highest one is copied to
`manifest.json`. A new revision starts from the current one and replaces only the
regions that succeeded in this run, so a snapshot's data — and therefore its status —
never regresses (research R6).

Version 1.1 (feature 004) takes table names and schema versions from the provider's spec
and adds optional per-location counters, a GCP `catalog` block and Azure
`excluded_price_types`. Optional fields are omitted when unset, so AWS manifests differ
from 1.0 only in `manifest_version`.
"""

import datetime as dt
import json
from dataclasses import asdict, dataclass, field, fields
from typing import Dict, List, Optional, Set

from src.pipeline import layout, providers
from src.pipeline.storage import PreconditionFailed, Storage

MANIFEST_VERSION = "1.1"
MAX_REASON = 500
# Optional per-location counters (1.1); serialized only when set.
REGION_COUNTERS = ("items_received", "duplicates_removed", "unlocated_rows", "throttled_responses", "requests_made")


class RevisionExists(Exception):
    """The revision file already exists; revisions are never overwritten."""


def iso(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(ts: str) -> dt.datetime:
    return dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def _clip(reason: Optional[str]) -> Optional[str]:
    return reason[:MAX_REASON] if reason is not None else None


# --- model ---------------------------------------------------------------------------

@dataclass
class DataFile:
    path: str
    bytes: int
    sha256: str
    row_count: int


@dataclass
class RegionTableEntry:
    written_by_run: str
    row_count: int
    files: List[DataFile] = field(default_factory=list)


@dataclass
class TableEntry:
    schema_version: int
    row_count: int
    regions: Dict[str, RegionTableEntry] = field(default_factory=dict)


@dataclass
class RawEntry:
    location: str
    file_count: int
    bytes: int
    stored_at: str
    purge_after: str


@dataclass
class FailedRegion:
    region: str
    reason: str
    attempts: int


@dataclass
class RegionResult:
    region: str
    outcome: str  # succeeded | failed | skipped
    attempts: int
    reason: Optional[str] = None
    services_downloaded: int = 0
    services_without_price_list: int = 0
    unparseable_prices: int = 0
    items_received: Optional[int] = None
    duplicates_removed: Optional[int] = None
    unlocated_rows: Optional[int] = None
    throttled_responses: Optional[int] = None
    requests_made: Optional[int] = None


@dataclass
class CatalogInfo:
    """Counters for a catalog-wide download (GCP; FR-031, FR-034)."""

    api_version: str
    requests_made: int = 0
    skus_received: int = 0
    throttled_responses: Optional[int] = None
    skus_out_of_scope: Optional[int] = None
    skus_unconfigured_locations: Optional[int] = None
    skus_unlocated: Optional[int] = None
    skus_without_price: Optional[int] = None
    prices_without_sku: Optional[int] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, d: dict) -> "CatalogInfo":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


@dataclass
class RunInfo:
    trigger: str
    mode: str
    host: str
    started_at: str
    ended_at: str
    pipeline_version: Dict[str, str]
    region_results: List[RegionResult] = field(default_factory=list)


@dataclass
class Manifest:
    manifest_version: str
    provider: str
    snapshot_date: str
    run_id: str
    revision: int
    previous_revision: Optional[int]
    created_at: str
    origin: str
    status: str
    requested: List[str]
    succeeded: List[str]
    failed: List[FailedRegion]
    run: RunInfo
    raw: Optional[Dict[str, RawEntry]]
    tables: Dict[str, TableEntry]
    purged: Optional[Dict[str, str]]
    catalog: Optional[CatalogInfo] = None
    excluded_price_types: Optional[List[str]] = None

    # -- serialization --

    def to_dict(self) -> dict:
        def region_result(r: RegionResult) -> dict:
            d = {
                "region": r.region,
                "outcome": r.outcome,
                "attempts": r.attempts,
                "services_downloaded": r.services_downloaded,
                "services_without_price_list": r.services_without_price_list,
                "unparseable_prices": r.unparseable_prices,
            }
            if r.reason is not None:
                d["reason"] = r.reason
            for name in REGION_COUNTERS:
                if getattr(r, name) is not None:
                    d[name] = getattr(r, name)
            return d

        doc = {
            "manifest_version": self.manifest_version,
            "provider": self.provider,
            "snapshot_date": self.snapshot_date,
            "run_id": self.run_id,
            "revision": self.revision,
            "previous_revision": self.previous_revision,
            "created_at": self.created_at,
            "origin": self.origin,
            "status": self.status,
            "regions": {
                "requested": list(self.requested),
                "succeeded": list(self.succeeded),
                "failed": [
                    {"region": f.region, "reason": f.reason, "attempts": f.attempts}
                    for f in self.failed
                ],
            },
            "run": {
                "trigger": self.run.trigger,
                "mode": self.run.mode,
                "host": self.run.host,
                "started_at": self.run.started_at,
                "ended_at": self.run.ended_at,
                "pipeline_version": dict(self.run.pipeline_version),
                "region_results": [region_result(r) for r in self.run.region_results],
            },
            "raw": None
            if self.raw is None
            else {
                region: {
                    "location": e.location,
                    "file_count": e.file_count,
                    "bytes": e.bytes,
                    "stored_at": e.stored_at,
                    "purge_after": e.purge_after,
                }
                for region, e in self.raw.items()
            },
            "tables": {
                name: {
                    "schema_version": t.schema_version,
                    "row_count": t.row_count,
                    "regions": {
                        region: {
                            "written_by_run": r.written_by_run,
                            "row_count": r.row_count,
                            "files": [
                                {
                                    "path": f.path,
                                    "bytes": f.bytes,
                                    "sha256": f.sha256,
                                    "row_count": f.row_count,
                                }
                                for f in r.files
                            ],
                        }
                        for region, r in t.regions.items()
                    },
                }
                for name, t in self.tables.items()
            },
            "purged": dict(self.purged) if self.purged is not None else None,
        }
        if self.catalog is not None:
            doc["catalog"] = self.catalog.to_dict()
        if self.excluded_price_types:
            doc["excluded_price_types"] = list(self.excluded_price_types)
        return doc

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=2) + "\n"

    @classmethod
    def from_dict(cls, d: dict) -> "Manifest":
        run = d["run"]
        return cls(
            manifest_version=d["manifest_version"],
            provider=d["provider"],
            snapshot_date=d["snapshot_date"],
            run_id=d["run_id"],
            revision=d["revision"],
            previous_revision=d["previous_revision"],
            created_at=d["created_at"],
            origin=d["origin"],
            status=d["status"],
            requested=list(d["regions"]["requested"]),
            succeeded=list(d["regions"]["succeeded"]),
            failed=[FailedRegion(**f) for f in d["regions"]["failed"]],
            run=RunInfo(
                trigger=run["trigger"],
                mode=run["mode"],
                host=run["host"],
                started_at=run["started_at"],
                ended_at=run["ended_at"],
                pipeline_version=dict(run["pipeline_version"]),
                region_results=[
                    RegionResult(
                        region=r["region"],
                        outcome=r["outcome"],
                        attempts=r["attempts"],
                        reason=r.get("reason"),
                        services_downloaded=r.get("services_downloaded", 0),
                        services_without_price_list=r.get("services_without_price_list", 0),
                        unparseable_prices=r.get("unparseable_prices", 0),
                        **{name: r.get(name) for name in REGION_COUNTERS},
                    )
                    for r in run["region_results"]
                ],
            ),
            raw=None
            if d["raw"] is None
            else {region: RawEntry(**e) for region, e in d["raw"].items()},
            tables={
                name: TableEntry(
                    schema_version=t["schema_version"],
                    row_count=t["row_count"],
                    regions={
                        region: RegionTableEntry(
                            written_by_run=r["written_by_run"],
                            row_count=r["row_count"],
                            files=[DataFile(**f) for f in r["files"]],
                        )
                        for region, r in t["regions"].items()
                    },
                )
                for name, t in d["tables"].items()
            },
            purged=dict(d["purged"]) if d["purged"] is not None else None,
            catalog=CatalogInfo.from_dict(d["catalog"]) if d.get("catalog") else None,
            excluded_price_types=list(d["excluded_price_types"]) if d.get("excluded_price_types") else None,
        )

    @classmethod
    def from_json(cls, text: str) -> "Manifest":
        return cls.from_dict(json.loads(text))


# --- building revisions ---------------------------------------------------------------

@dataclass
class RegionOutcome:
    """This run's result for one region, as the runner hands it to build_revision."""

    region: str
    succeeded: bool
    attempts: int
    reason: Optional[str] = None
    services_downloaded: int = 0
    services_without_price_list: int = 0
    unparseable_prices: int = 0
    tables: Optional[Dict[str, RegionTableEntry]] = None
    raw: Optional[RawEntry] = None
    items_received: Optional[int] = None
    duplicates_removed: Optional[int] = None
    unlocated_rows: Optional[int] = None
    throttled_responses: Optional[int] = None
    requests_made: Optional[int] = None

    def to_result(self) -> RegionResult:
        return RegionResult(
            region=self.region,
            outcome="succeeded" if self.succeeded else "failed",
            attempts=self.attempts,
            reason=_clip(self.reason),
            services_downloaded=self.services_downloaded,
            services_without_price_list=self.services_without_price_list,
            unparseable_prices=self.unparseable_prices,
            **{name: getattr(self, name) for name in REGION_COUNTERS},
        )


def make_raw_entry(
    location: str, file_count: int, total_bytes: int, stored_at: dt.datetime, raw_retention_days: int
) -> RawEntry:
    # S3 lifecycle expiry rounds up to the next midnight UTC, hence the extra day.
    purge_after = stored_at.astimezone(dt.timezone.utc).date() + dt.timedelta(days=raw_retention_days + 1)
    return RawEntry(
        location=location,
        file_count=file_count,
        bytes=total_bytes,
        stored_at=iso(stored_at),
        purge_after=purge_after.isoformat(),
    )


def new_run_info(settings, trigger: str, mode: str, started_at: dt.datetime, ended_at: dt.datetime) -> RunInfo:
    return RunInfo(
        trigger=trigger,
        mode=mode,
        host=settings.host_label,
        started_at=iso(started_at),
        ended_at=iso(ended_at),
        pipeline_version={"git_sha": settings.git_sha, "image_tag": settings.image_tag},
    )


def derive_status(requested: List[str], regions_with_data: Set[str]) -> str:
    have = [r for r in requested if r in regions_with_data]
    if requested and len(have) == len(requested):
        return "succeeded"
    if have:
        return "partial"
    return "failed"


def build_revision(
    previous: Optional[Manifest],
    *,
    provider: str,
    snapshot_date: str,
    run_id: str,
    run_info: RunInfo,
    outcomes: List[RegionOutcome],
    requested_regions: List[str],
    created_at: dt.datetime,
    origin: str = "pipeline",
    revision: Optional[int] = None,
    raw: Optional[Dict[str, RawEntry]] = None,
    catalog: Optional[CatalogInfo] = None,
    excluded_price_types: Optional[List[str]] = None,
) -> Manifest:
    """`raw` adds raw entries for units that aren't locations (the GCP `catalog`)."""
    unit_raw = dict(raw or {})
    carry = previous is not None and previous.status != "purged"

    region_tables: Dict[str, Dict[str, RegionTableEntry]] = {}
    raw: Dict[str, RawEntry] = {}
    prior_failures: Dict[str, FailedRegion] = {}
    requested: Set[str] = set(requested_regions)
    if carry:
        requested |= set(previous.requested)
        for name, table in previous.tables.items():
            for region, entry in table.regions.items():
                region_tables.setdefault(region, {})[name] = entry
        raw.update(previous.raw or {})
        prior_failures = {f.region: f for f in previous.failed}

    this_run_failures: Dict[str, RegionOutcome] = {}
    for outcome in outcomes:
        if outcome.succeeded:
            region_tables[outcome.region] = dict(outcome.tables or {})
            if outcome.raw is not None:
                raw[outcome.region] = outcome.raw
        else:
            this_run_failures[outcome.region] = outcome
    raw.update(unit_raw)

    regions_with_data = {r for r, tables in region_tables.items() if tables and r in requested}
    ordered_requested = sorted(requested)
    status = derive_status(ordered_requested, regions_with_data)

    failed: List[FailedRegion] = []
    for region in ordered_requested:
        if region in regions_with_data:
            continue
        if region in this_run_failures:
            o = this_run_failures[region]
            failed.append(FailedRegion(region, _clip(o.reason or "failed"), o.attempts))
        elif region in prior_failures:
            failed.append(prior_failures[region])
        else:
            failed.append(FailedRegion(region, "not attempted", 0))

    tables: Dict[str, TableEntry] = {}
    if regions_with_data:
        for name, version in providers.get_provider(provider).tables.items():
            regions = {
                region: region_tables[region][name]
                for region in sorted(regions_with_data)
                if name in region_tables[region]
            }
            tables[name] = TableEntry(
                schema_version=version,
                row_count=sum(r.row_count for r in regions.values()),
                regions=regions,
            )

    run_info.region_results = [o.to_result() for o in outcomes]
    number = revision or ((previous.revision + 1) if previous else 1)
    return Manifest(
        manifest_version=MANIFEST_VERSION,
        provider=provider,
        snapshot_date=snapshot_date,
        run_id=run_id,
        revision=number,
        previous_revision=number - 1 if number > 1 else None,
        created_at=iso(created_at),
        origin=origin,
        status=status,
        requested=ordered_requested,
        succeeded=sorted(regions_with_data),
        failed=failed,
        run=run_info,
        # Location entries only for locations with data; unit entries (e.g. `catalog`) always.
        raw=None
        if origin == "backfill"
        else {u: raw[u] for u in sorted(raw) if u in regions_with_data or u not in requested},
        tables=tables,
        purged=None,
        catalog=catalog,
        excluded_price_types=list(excluded_price_types) if excluded_price_types else None,
    )


def build_purged_revision(current: Manifest, now: dt.datetime, revision: Optional[int] = None) -> Manifest:
    """Retention purge (FR-022): same location, status purged, no file list."""
    number = revision or current.revision + 1
    return Manifest(
        manifest_version=MANIFEST_VERSION,
        provider=current.provider,
        snapshot_date=current.snapshot_date,
        run_id=current.run_id,
        revision=number,
        previous_revision=number - 1,
        created_at=iso(now),
        origin=current.origin,
        status="purged",
        requested=list(current.requested),
        succeeded=[],
        failed=[],
        run=RunInfo(
            trigger=current.run.trigger if current.run.trigger != "backfill" else "manual",
            mode="retention-purge",
            host=current.run.host,
            started_at=iso(now),
            ended_at=iso(now),
            pipeline_version=dict(current.run.pipeline_version),
            region_results=[],
        ),
        raw=None if current.origin == "backfill" else {},
        tables={},
        purged={"purged_at": iso(now), "reason": "retention-thinning"},
    )


def active_file_paths(manifest: Optional[Manifest]) -> Set[str]:
    if manifest is None or manifest.status == "purged":
        return set()
    return {
        f.path
        for table in manifest.tables.values()
        for region in table.regions.values()
        for f in region.files
    }


# --- persistence ----------------------------------------------------------------------

def write_revision(store: Storage, manifest: Manifest) -> None:
    """Write revisions/<NNNN>.json (never overwritten), then the current manifest.json."""
    body = manifest.to_json().encode()
    try:
        store.put_if_absent(
            layout.revision_key(manifest.provider, manifest.snapshot_date, manifest.revision), body
        )
    except PreconditionFailed:
        raise RevisionExists(f"{manifest.snapshot_date} revision {manifest.revision} already exists")
    store.put_bytes(layout.manifest_key(manifest.provider, manifest.snapshot_date), body)


def read_current(store: Storage, provider: str, snapshot_date: str) -> Optional[Manifest]:
    try:
        return Manifest.from_json(store.get_bytes(layout.manifest_key(provider, snapshot_date)).decode())
    except FileNotFoundError:
        return None


def next_revision_number(store: Storage, provider: str, snapshot_date: str) -> int:
    prefix = layout.manifest_date_prefix(provider, snapshot_date) + "revisions/"
    numbers = [n for n in (layout.parse_revision_key(o.key) for o in store.list(prefix)) if n]
    return max(numbers, default=0) + 1


def list_snapshot_dates(store: Storage, provider: str) -> List[str]:
    """Dates that have a current manifest.json."""
    prefix = layout.manifests_prefix(provider)
    dates = set()
    for obj in store.list(prefix):
        rest = obj.key[len(prefix):]
        parts = rest.split("/")
        if len(parts) == 2 and parts[1] == "manifest.json":
            dates.add(parts[0])
    return sorted(dates)
