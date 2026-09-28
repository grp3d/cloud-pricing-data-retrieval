"""
Pipeline CLI entry point: `python -m src.pipeline <command>` (contracts/cli.md).

Also the container ENTRYPOINT. Commands are thin wrappers over the core modules in
this package (constitution Principle III). Logs are JSON lines on stdout.

Exit codes: 0 completed and reported (any snapshot status, or a refused run);
2 invalid settings/arguments; 3 precondition not met (nothing changed); 1 verify
mismatches or an unhandled crash.
"""

import json
import logging
import sys
import time
import traceback

import click

from src.pipeline.config import PipelineSettings, SettingsError

EXIT_SETTINGS = 2
EXIT_PRECONDITION = 3


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if message.startswith("{"):
            try:
                payload = json.loads(message)
                payload.setdefault("level", record.levelname.lower())
                return json.dumps(payload, sort_keys=True)
            except ValueError:
                pass
        entry = {"level": record.levelname.lower(), "logger": record.name, "message": message}
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, sort_keys=True)


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    for noisy in ("botocore", "boto3", "urllib3", "s3transfer", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _load_settings() -> PipelineSettings:
    try:
        return PipelineSettings.from_env()
    except SettingsError as e:
        click.echo(json.dumps({"level": "error", "message": f"invalid settings: {e}"}), err=True)
        sys.exit(EXIT_SETTINGS)


class _crash_alert:
    """Best-effort RUN CRASHED alert on any unhandled error, then exit 1 (FR-057)."""

    def __init__(self, settings, context: dict):
        self.settings = settings
        self.context = context

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None or issubclass(exc_type, (SystemExit, KeyboardInterrupt)):
            return False
        logging.getLogger("pipeline").exception("unhandled error")
        detail = "".join(traceback.format_exception(exc_type, exc, tb))[-3000:]
        try:
            from src.pipeline.notify import RUN_CRASHED, Notifier

            Notifier.from_settings(self.settings).send(RUN_CRASHED, self.context, detail=detail)
        except Exception:  # the alert is best effort; never mask the original error
            logging.getLogger("pipeline").exception("could not send RUN CRASHED alert")
        sys.exit(1)


def _split_regions(value):
    return [r.strip() for r in value.split(",") if r.strip()] if value else None


@click.group(context_settings=dict(help_option_names=["-h", "--help"]))
def cli():
    """Cloud pricing pipeline: run, retention, upload-history, raw, verify, settings."""
    _configure_logging()


@cli.command()
@click.option("--snapshot-date", default=None, help="YYYY-MM-DD (default: UTC date at start)")
@click.option("--regions", default=None, help="Comma-separated subset of regions")
@click.option("--trigger", type=click.Choice(["scheduled", "manual"]), default="manual")
@click.option("--transform-only", is_flag=True, help="Rebuild tables from stored raw data; no download")
@click.option("--skip-retention", is_flag=True, help="Debugging only: skip the inline retention step")
def run(snapshot_date, regions, trigger, transform_only, skip_retention):
    """Download, transform and publish a snapshot."""
    from src.pipeline import runner
    from src.pipeline.runner import PreconditionError, RunRequest

    settings = _load_settings()
    request = RunRequest(
        snapshot_date=snapshot_date,
        regions=_split_regions(regions),
        trigger=trigger,
        transform_only=transform_only,
        skip_retention=skip_retention,
    )
    with _crash_alert(settings, {"snapshot_date": snapshot_date, "trigger": trigger, "mode": "run"}):
        try:
            runner.run_snapshot(settings, request)
        except SettingsError as e:  # e.g. clock skew with the storage service
            logging.getLogger("pipeline").error(f"invalid settings: {e}")
            sys.exit(EXIT_SETTINGS)
        except PreconditionError as e:
            logging.getLogger("pipeline").error(f"precondition failed: {e}")
            sys.exit(EXIT_PRECONDITION)
        except ValueError as e:  # malformed date/region arguments
            logging.getLogger("pipeline").error(f"invalid argument: {e}")
            sys.exit(EXIT_SETTINGS)


@cli.command()
@click.option("--dry-run", is_flag=True, help="Report what would be deleted; change nothing")
@click.option("--output", "output_path", default=None, help="Also write the plan/result as JSON")
def retention(dry_run, output_path):
    """Apply retention across all snapshot dates (FR-021–FR-024, FR-045)."""
    from src.pipeline import layout
    from src.pipeline.clock import check_clock_skew
    from src.pipeline.retention import run_retention
    from src.pipeline.runner import utcnow
    from src.pipeline.storage import open_storage

    settings = _load_settings()
    with _crash_alert(settings, {"mode": "retention"}):
        now = utcnow()
        store = open_storage(settings.storage_uri)
        try:
            check_clock_skew(store, settings, now)
        except SettingsError as e:
            click.echo(json.dumps({"level": "error", "message": str(e)}), err=True)
            sys.exit(EXIT_SETTINGS)
        result = run_retention(
            store,
            settings,
            now=now,
            sleep=time.sleep,
            run_id=layout.new_run_id(now),
            dry_run=dry_run,
        )
    text = json.dumps({"retention": result}, sort_keys=True, indent=2)
    click.echo(text)
    if output_path:
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")


@cli.command("upload-history")
@click.option("--snapshot-date", required=True)
@click.option(
    "--source",
    default=None,
    help="Local root with the new layout or legacy pricing_aws/ (default: $DATA_DIRECTORY_ROOT)",
)
@click.option("--overwrite", is_flag=True, help="Replace existing data for this date with a new revision")
@click.option("--dry-run", is_flag=True, help="Print the manifest and files; write nothing")
def upload_history_cmd(snapshot_date, source, overwrite, dry_run):
    """Upload one local table snapshot to the store (raw data is never uploaded)."""
    import os

    from src.pipeline.backfill import BackfillPrecondition, upload_history
    from src.pipeline.storage import open_storage

    settings = _load_settings()
    source = source or os.environ.get("DATA_DIRECTORY_ROOT")
    if not source:
        click.echo(json.dumps({"level": "error", "message": "--source or DATA_DIRECTORY_ROOT is required"}), err=True)
        sys.exit(EXIT_SETTINGS)
    with _crash_alert(settings, {"snapshot_date": snapshot_date, "trigger": "backfill", "mode": "backfill"}):
        try:
            result = upload_history(
                open_storage(settings.storage_uri),
                settings,
                snapshot_date,
                source,
                overwrite=overwrite,
                dry_run=dry_run,
            )
        except BackfillPrecondition as e:
            click.echo(json.dumps({"level": "error", "message": str(e)}), err=True)
            sys.exit(EXIT_PRECONDITION)
        except SettingsError as e:
            click.echo(json.dumps({"level": "error", "message": str(e)}), err=True)
            sys.exit(EXIT_SETTINGS)
        except ValueError as e:
            click.echo(json.dumps({"level": "error", "message": f"invalid argument: {e}"}), err=True)
            sys.exit(EXIT_SETTINGS)
    click.echo(json.dumps(result, sort_keys=True, indent=2))


@cli.group()
def raw():
    """Inspect and download raw pricing snapshots before they are purged (FR-025)."""


@raw.command("list")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
def raw_list(as_json):
    """List raw snapshots with their purge dates."""
    from dataclasses import asdict

    from src.pipeline.raw_access import list_raw
    from src.pipeline.storage import open_storage

    settings = _load_settings()
    rows = list_raw(open_storage(settings.storage_uri), settings.provider, settings.raw_retention_days)
    if as_json:
        click.echo(json.dumps([asdict(r) for r in rows], indent=2))
        return
    header = f"{'snapshot_date':<13} {'region':<16} {'run_id':<24} {'files':>5} {'bytes':>12}  {'stored_at':<20} purge_after"
    click.echo(header)
    for r in rows:
        click.echo(
            f"{r.snapshot_date:<13} {r.region:<16} {r.run_id:<24} {r.file_count:>5} {r.bytes:>12}  {r.stored_at:<20} {r.purge_after}"
        )


@raw.command("download")
@click.option("--snapshot-date", required=True)
@click.option("--dest", required=True, type=click.Path(file_okay=False))
@click.option("--regions", default=None, help="Comma-separated subset of regions")
@click.option("--decompress", is_flag=True, help="Write .json instead of .json.zst")
def raw_download(snapshot_date, dest, regions, decompress):
    """Copy one raw snapshot to a local directory."""
    from src.pipeline.raw_access import RawNotFound, download_raw
    from src.pipeline.storage import open_storage

    settings = _load_settings()
    try:
        files = download_raw(
            open_storage(settings.storage_uri),
            settings.provider,
            snapshot_date,
            dest,
            regions=_split_regions(regions),
            decompress=decompress,
        )
    except RawNotFound as e:
        click.echo(json.dumps({"level": "error", "message": str(e)}), err=True)
        sys.exit(EXIT_PRECONDITION)
    click.echo(json.dumps({"level": "info", "downloaded": len(files), "dest": dest}))


@cli.command()
@click.option("--snapshot-date", default=None, help="Verify one date (default: all active manifests)")
def verify(snapshot_date):
    """Check every file in active manifests against its size, sha256 and row count."""
    from src.pipeline.storage import open_storage
    from src.pipeline.verify import verify_snapshot

    settings = _load_settings()
    problems = verify_snapshot(open_storage(settings.storage_uri), settings.provider, snapshot_date)
    for p in problems:
        click.echo(json.dumps({"level": "error", "snapshot_date": p.snapshot_date, "path": p.path, "problem": p.problem}))
    click.echo(json.dumps({"level": "info", "verify": "ok" if not problems else "mismatch", "problems": len(problems)}))
    sys.exit(1 if problems else 0)


@cli.command()
def settings():
    """Print the effective settings (secrets redacted) and validate them."""
    click.echo(json.dumps(_load_settings().redacted(), sort_keys=True, indent=2))


if __name__ == "__main__":
    cli()
