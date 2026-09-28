"""
run_snapshot(): one pipeline run for one snapshot date (data-model.md "Run flow").

    claim the date → download regions concurrently → compress + upload raw →
    transform one region at a time → upload Parquet → write revision + manifest →
    move latest.json (succeeded only) → report

Works identically against a local directory or S3 (FR-008). Data files are written
before the manifest, and the manifest before latest.json (FR-013).
"""

import datetime as dt
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from compression import zstd
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional

from src.aws_pricing_transformations import TransformRequest, transform_pricing_to_parquet
from src.pipeline import claims, layout
from src.pipeline import manifest as m
from src.pipeline.clock import check_clock_skew
from src.pipeline.config import PipelineSettings
from src.pipeline.downloader import AwsPricingDownloader, Downloader, RegionDownload
from src.pipeline.latest import update_latest
from src.pipeline.notify import Notifier, alert_kinds_for
from src.pipeline.retention import run_retention, summarize
from src.pipeline.storage import Storage, open_storage

logger = logging.getLogger(__name__)

ZSTD_LEVEL = 3


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class RunTimeout(Exception):
    """The run exceeded RUN_TIMEOUT_MINUTES."""


class PreconditionError(Exception):
    """A precondition failed before any write (CLI exit code 3)."""


@dataclass
class RunRequest:
    snapshot_date: Optional[str] = None
    regions: Optional[List[str]] = None
    trigger: str = "manual"  # scheduled | manual
    transform_only: bool = False
    skip_retention: bool = False


@dataclass
class RunReport:
    run_id: str
    trigger: str
    mode: str
    snapshot_date: str
    regions: List[str]
    outcome: str = "completed"  # completed | refused | error
    snapshot_status: Optional[str] = None
    revision: Optional[int] = None
    latest_moved: bool = False
    region_results: List[dict] = field(default_factory=list)
    row_counts: Dict[str, int] = field(default_factory=dict)
    duration_seconds: float = 0.0
    retention: dict = field(default_factory=dict)
    alerts_sent: List[str] = field(default_factory=list)
    error: Optional[str] = None
    refused_by: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps({"run_report": asdict(self)}, sort_keys=True)


def _compress_to(src_path: str, dest_path: str) -> None:
    with open(src_path, "rb") as fin, zstd.open(dest_path, "wb", level=ZSTD_LEVEL) as fout:
        shutil.copyfileobj(fin, fout, length=1 << 20)


def _decompress_to(src_path: str, dest_path: str) -> None:
    with zstd.open(src_path, "rb") as fin, open(dest_path, "wb") as fout:
        shutil.copyfileobj(fin, fout, length=1 << 20)


class _Deadline:
    def __init__(self, now: Callable[[], dt.datetime], minutes: int):
        self._now = now
        self._end = now() + dt.timedelta(minutes=minutes)

    def check(self) -> None:
        if self._now() > self._end:
            raise RunTimeout("run exceeded RUN_TIMEOUT_MINUTES")


class _SnapshotRun:
    def __init__(self, settings, request, store, downloader, now, sleep, work_dir, log):
        self.s: PipelineSettings = settings
        self.req: RunRequest = request
        self.store: Storage = store
        self.downloader: Downloader = downloader
        self.now = now
        self.sleep = sleep
        self.work_dir = work_dir
        self.log = log
        self.provider = settings.provider
        self.started_at = now()
        self.date = request.snapshot_date or self.started_at.date().isoformat()
        layout.validate_snapshot_date(self.date)
        self.run_id = layout.new_run_id(self.started_at)
        self.regions = [layout.validate_region(r) for r in (request.regions or settings.regions)]
        if request.transform_only:
            self.mode = "transform-only"
        elif request.regions and sorted(request.regions) != sorted(settings.regions):
            self.mode = "regions"
        else:
            self.mode = "full"
        self.deadline = _Deadline(now, settings.run_timeout_minutes)
        self.report = RunReport(
            run_id=self.run_id,
            trigger=request.trigger,
            mode=self.mode,
            snapshot_date=self.date,
            regions=list(self.regions),
        )

    # --- per-region work ------------------------------------------------------------

    def _download(self, region: str, region_dir: str) -> RegionDownload:
        raw_dir = os.path.join(region_dir, "raw")
        os.makedirs(raw_dir, exist_ok=True)
        if not self.req.transform_only:
            return self.downloader.download_region(region, raw_dir)
        return self._fetch_stored_raw(region, raw_dir)

    def _fetch_stored_raw(self, region: str, raw_dir: str) -> RegionDownload:
        """Transform-only: decompress this region's raw files recorded in the manifest."""
        entry = (self.previous.raw or {}).get(region) if self.previous else None
        files = []
        for obj in self.store.list(entry.location) if entry else []:
            name = os.path.basename(obj.key)
            local_zst = os.path.join(raw_dir, name)
            self.store.download_file(obj.key, local_zst)
            local_json = local_zst[: -len(".zst")]
            _decompress_to(local_zst, local_json)
            os.remove(local_zst)
            files.append(local_json)
        return RegionDownload(region=region, files=sorted(files))

    def _store_raw(self, region: str, download: RegionDownload) -> m.RawEntry:
        prefix = layout.raw_run_prefix(self.provider, self.date, region, self.run_id)
        total = 0
        for path in download.files:
            compressed = path + ".zst"
            _compress_to(path, compressed)
            total += os.path.getsize(compressed)
            self.store.put_file(
                layout.raw_file_key(self.provider, self.date, region, self.run_id, os.path.basename(path)),
                compressed,
            )
            os.remove(compressed)
        return m.make_raw_entry(prefix, len(download.files), total, self.now(), self.s.raw_retention_days)

    def _transform_and_upload(self, region: str, download: RegionDownload, region_dir: str):
        result = transform_pricing_to_parquet(
            TransformRequest(
                json_files=download.files,
                staging_dir=os.path.join(region_dir, "staging"),
                region=region,
                run_id=self.run_id,
                snapshot_date=self.date,
                log_callback=self.log,
            )
        )
        if result.parse_failed_files or not result.success:
            reason = "; ".join(result.errors) or "transform produced no tables"
            return None, result, reason
        tables: Dict[str, m.RegionTableEntry] = {}
        for table in layout.TABLES:
            written = [f for f in result.files if f.table == table]
            files = []
            for f in written:
                key = layout.parquet_file_key(self.provider, table, self.date, region, self.run_id)
                self.store.put_file(key, f.local_path)
                files.append(m.DataFile(path=key, bytes=f.bytes, sha256=f.sha256, row_count=f.row_count))
            tables[table] = m.RegionTableEntry(
                written_by_run=self.run_id,
                row_count=sum(f.row_count for f in files),
                files=files,
            )
        return tables, result, None

    def _process_region(self, region: str, transform_lock: threading.Semaphore) -> m.RegionOutcome:
        region_dir = tempfile.mkdtemp(prefix=f"{region}-", dir=self.work_dir)
        try:
            self.deadline.check()
            download = self._download(region, region_dir)
            if self.req.transform_only and not download.files:
                return m.RegionOutcome(region, False, 0, reason="raw data purged or missing")
            if not download.succeeded:
                return m.RegionOutcome(
                    region,
                    False,
                    download.max_attempts,
                    reason=download.failure_reason(),
                    services_downloaded=len(download.files),
                    services_without_price_list=download.services_without_price_list,
                )
            raw_entry = (
                self.previous.raw[region]
                if self.req.transform_only
                else self._store_raw(region, download)
            )
            with transform_lock:  # transforms are memory-bound: one region at a time (R1)
                self.deadline.check()
                tables, result, reason = self._transform_and_upload(region, download, region_dir)
            if tables is None:
                return m.RegionOutcome(
                    region,
                    False,
                    download.max_attempts,
                    reason=reason,
                    services_downloaded=len(download.files),
                    services_without_price_list=download.services_without_price_list,
                    unparseable_prices=result.unparseable_prices,
                )
            return m.RegionOutcome(
                region,
                True,
                download.max_attempts,
                services_downloaded=len(download.files),
                services_without_price_list=download.services_without_price_list,
                unparseable_prices=result.unparseable_prices,
                tables=tables,
                raw=raw_entry,
            )
        except RunTimeout:
            raise
        except Exception as e:  # one region's failure never stops the others (FR-003)
            logger.exception("Region %s failed", region)
            return m.RegionOutcome(region, False, 0, reason=f"{type(e).__name__}: {e}")
        finally:
            shutil.rmtree(region_dir, ignore_errors=True)

    # --- whole run ------------------------------------------------------------------

    def check_transform_only_preconditions(self) -> None:
        if not self.req.transform_only:
            return
        if self.previous is None:
            raise PreconditionError(f"no manifest for {self.date}; nothing to transform")
        missing = []
        for region in self.regions:
            entry = (self.previous.raw or {}).get(region)
            if entry is None or not any(True for _ in self.store.list(entry.location)):
                missing.append(region)
        if missing:
            raise PreconditionError(f"raw data purged or missing for: {', '.join(missing)}")

    def execute(self) -> m.Manifest:
        self.previous = m.read_current(self.store, self.provider, self.date)
        self.check_transform_only_preconditions()

        transform_lock = threading.Semaphore(self.s.transform_concurrency)
        with ThreadPoolExecutor(max_workers=max(1, len(self.regions))) as pool:
            outcomes = list(pool.map(lambda r: self._process_region(r, transform_lock), self.regions))

        self.deadline.check()
        revision = m.next_revision_number(self.store, self.provider, self.date)
        run_info = m.new_run_info(self.s, self.req.trigger, self.mode, self.started_at, self.now())
        manifest = m.build_revision(
            self.previous,
            provider=self.provider,
            snapshot_date=self.date,
            run_id=self.run_id,
            run_info=run_info,
            outcomes=outcomes,
            requested_regions=self.regions,
            created_at=self.now(),
            revision=revision,
        )
        m.write_revision(self.store, manifest)
        self.report.latest_moved = update_latest(self.store, manifest, self.now())
        self._retention()
        return manifest

    def _retention(self) -> None:
        """Inline retention while still holding the claim (FR-048–FR-050).

        Any failure is recorded and alerted, but never changes the snapshot's status or
        latest.json.
        """
        if self.req.skip_retention:
            self.report.retention = {"skipped": True}
            return
        try:
            result = run_retention(
                self.store,
                self.s,
                now=self.now(),
                sleep=self.sleep,
                run_id=self.run_id,
                own_date=self.date,
            )
            self.report.retention = summarize(result)
        except Exception as e:
            logger.exception("retention step failed")
            self.report.retention = {"error": f"{type(e).__name__}: {e}"[:500]}


def run_snapshot(
    settings: PipelineSettings,
    request: RunRequest,
    *,
    store: Optional[Storage] = None,
    downloader: Optional[Downloader] = None,
    now: Callable[[], dt.datetime] = utcnow,
    sleep: Callable[[float], None] = time.sleep,
    work_dir: Optional[str] = None,
    log: Optional[Callable[[str], None]] = None,
    notifier=None,
) -> RunReport:
    store = store or open_storage(settings.storage_uri)
    check_clock_skew(store, settings, now())
    notifier = notifier or Notifier.from_settings(settings)
    if work_dir:
        os.makedirs(work_dir, exist_ok=True)
    downloader = downloader or AwsPricingDownloader(settings, log=log)
    wall_start = time.monotonic()
    run = _SnapshotRun(settings, request, store, downloader, now, sleep, work_dir, log or logger.info)
    report = run.report

    try:
        with claims.held_claim(
            store, settings.provider, run.date, run.run_id, request.trigger, settings.run_claim_ttl_minutes, now()
        ):
            manifest = run.execute()
    except claims.ClaimHeld as held:
        # Another run owns this date: refuse without writing anything (FR-005).
        report.outcome = "refused"
        report.refused_by = held.holder_run_id
        report.duration_seconds = round(time.monotonic() - wall_start, 3)
        logger.warning(f"refused: run already in progress (run_id={held.holder_run_id})")
        _notify(notifier, report, settings)
        logger.info(report.to_json())
        return report

    report.snapshot_status = manifest.status
    report.revision = manifest.revision
    report.region_results = [asdict(r) for r in manifest.run.region_results]
    report.row_counts = {name: t.row_count for name, t in manifest.tables.items()}
    report.duration_seconds = round(time.monotonic() - wall_start, 3)
    _notify(notifier, report, settings)
    logger.info(report.to_json())
    return report


def _notify(notifier, report: RunReport, settings) -> None:
    for kind in alert_kinds_for(report, settings):
        if notifier.send(kind, report):
            report.alerts_sent.append(kind)
