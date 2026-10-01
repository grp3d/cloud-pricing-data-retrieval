# Contract: Pipeline CLI

**Entry point**: `python -m src.pipeline <command> [options]`, which is also the container `ENTRYPOINT`. The default container `CMD` is `run --trigger scheduled`.

All commands read [configuration](./configuration.md) from environment variables and log structured JSON lines to stdout. Commands that change stored data finish with a RunReport JSON line (see [data-model.md](../data-model.md#run-and-runreport)).

## Exit codes (all commands)

| Code | Meaning |
|---|---|
| `0` | Completed and reported. This includes `partial`/`failed` snapshot outcomes and a refused run, because those are reported by alert, not by exit code. |
| `2` | Invalid settings or arguments. Nothing was changed. |
| `3` | Precondition not met. Nothing was changed. Used by `upload-history` when the target exists without `--overwrite`, by `transform-only` when raw data is purged, and by `raw download` when a snapshot is missing. |
| `124` | The run hit `RUN_TIMEOUT_MINUTES`. The process stops immediately so it can't outlive its claim, after a best-effort `RUN CRASHED` alert. The claim expires on its own. |
| other non-zero | Unhandled crash. The process first makes a best-effort attempt to send a `RUN CRASHED` alert, wherever it runs (FR-057). In the cloud, the ECS task-stopped rule also catches kills and OOMs. |

---

## `run`

Download, transform and publish a snapshot, then run inline retention.

| Option | Default | Notes |
|---|---|---|
| `--snapshot-date YYYY-MM-DD` | UTC date at start | |
| `--regions a,b,c` | all configured (`PRICING_REGIONS`) | A subset gives `run.mode=regions`. |
| `--transform-only` | off | Rebuilds tables from the raw data recorded in the current manifest, with no download. Needs an existing manifest. Exits 3 if any requested region's raw data is gone. |
| `--trigger scheduled\|manual` | `manual` | `scheduled` enables the "refused scheduled run" alert. |
| `--skip-retention` | off | For debugging only. Retention is otherwise always run (FR-048). |

**Behavior**:
- Claim the date; if it's already claimed, the run is refused (exit 0 and a log line, plus an alert for `scheduled`).
- Then follow the run flow in [data-model.md](../data-model.md#run-flow-run_snapshot).

## `retention`

Run retention across all dates on demand (FR-023, FR-048).

| Option | Default | Notes |
|---|---|---|
| `--dry-run` | off | Computes and prints the full plan, deleting and writing nothing. |
| `--output PATH` | none | Also writes the plan (or result) as JSON. |

**Output plan fields**:
- `superseded[]`: `{path, snapshot_date, eligible_at}`
- `orphans[]`
- `purge_snapshots[]`: `{snapshot_date, reason}`
- `keep_monthly[]`: `{month, snapshot_date}`
- `raw_local[]`: local mode only
- `skipped_busy_dates[]`

## `upload-history`

Upload one local table snapshot to the configured store (FR-038–FR-042). This command never uploads raw data.

| Option | Default | Notes |
|---|---|---|
| `--snapshot-date YYYY-MM-DD` | required | |
| `--source PATH` | `$DATA_DIRECTORY_ROOT` | This is a local root holding either the new layout (`<source>/aws/manifests/<D>/manifest.json`, whose contents are kept while its files are uploaded under fresh keys) or the legacy layout (`<source>/pricing_aws/parquet/<table>/snapshot_date=<D>/…`), for which a manifest is generated. |
| `--overwrite` | off | Without it, the command exits 3 before writing anything if any `parquet/*/snapshot_date=<D>/` or `manifests/<D>/` object exists in the target store. With it, it publishes a new revision that supersedes the existing data. |
| `--dry-run` | off | Prints the manifest it would write and the files it would upload. |

A new-layout source whose manifest isn't `succeeded` (partial or failed) is refused with exit 3, with or without `--overwrite`. Only complete snapshots are published.

## `raw list`

Lists raw snapshots with their purge dates (FR-025).

Output columns: `snapshot_date`, `region`, `run_id`, `file_count`, `bytes`, `stored_at`, `purge_after`. Add `--json` for machine-readable output.

## `raw download`

Copies one raw snapshot to a local directory (FR-025).

Files are written to `<dest>/<region>/<run_id>/`, so runs of the same region never overwrite each other.

| Option | Default | Notes |
|---|---|---|
| `--snapshot-date YYYY-MM-DD` | required | |
| `--dest PATH` | required | |
| `--regions a,b` | all regions in the current manifest | |
| `--decompress` | off | Writes `.json` instead of `.json.zst`. |

## `verify`

Checks that every file in active manifests exists and matches its `bytes`, `sha256` and `row_count` (SC-006).

| Option | Default |
|---|---|
| `--snapshot-date YYYY-MM-DD` | all active manifests |

Exits `0` if every file matches and `1` if there is any mismatch. Mismatches are listed.

## `settings`

Prints the effective settings, with secrets redacted, and validates them. Exits 2 if they are invalid.

---

## Unchanged CLI

`python -m src.aws_pricing_cli --region … (--service-code … | --all-services) [--output-dir …]` keeps its current behavior and output layout for ad hoc local use (FR-037).
