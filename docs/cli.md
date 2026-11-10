# CLI reference

**Entry point**: `python -m src.pipeline <command> [options]`. This is also the container's
`ENTRYPOINT`; the default `CMD` is `run --trigger scheduled`.

Every command works for every provider, selected by `PIPELINE_PROVIDER` (`aws`, `azure`, `gcp`),
and touches only that provider's `<root>/<provider>/` prefix. Settings come from environment
variables ([configuration.md](configuration.md)). Commands log JSON lines to stdout; commands
that change stored data end with a run report line.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Completed. Includes `partial` and `failed` snapshots and refused runs, which are reported by alert, not exit code. |
| `1` | `verify` found a mismatch. |
| `2` | Invalid settings or arguments (including a missing `GCP_PRICING_API_KEY` for `gcp`). Nothing was changed. |
| `3` | Precondition not met: `upload-history` target exists without `--overwrite`, `transform-only` raw data is purged, or `raw download` snapshot is missing. Nothing was changed. |
| `124` | Hit `RUN_TIMEOUT_MINUTES`; the process stops after a best-effort `RUN CRASHED` alert. |
| other | Unhandled crash, after a best-effort `RUN CRASHED` alert. |

## `run`

Download, transform and publish a snapshot, then run retention.

| Option | Default | Notes |
|---|---|---|
| `--snapshot-date YYYY-MM-DD` | UTC date at start | |
| `--regions a,b` | all configured (`PRICING_REGIONS`) | Locations to (re)build. Azure and GCP always add `global`. For GCP the whole catalog is still downloaded; `--regions` limits which locations are built. A subset publishes a new revision that keeps the other locations. |
| `--transform-only` | off | Rebuild tables from the stored raw data, with no download. Exits 3 if the raw data is gone (after 30 days). |
| `--trigger scheduled\|manual` | `manual` | `scheduled` enables the `RUN REFUSED` alert. |
| `--skip-retention` | off | Debugging only. |

If another run holds the date's claim, the run is refused (exit 0, plus an alert when scheduled).

## `retention`

Apply retention across all dates on demand. It also runs at the end of every `run`.

| Option | Notes |
|---|---|
| `--dry-run` | Print the plan; delete nothing. |
| `--output PATH` | Also write the plan or result as JSON. |

The plan lists `superseded`, `orphans`, `purge_snapshots`, `keep_monthly`, `raw_local` (local
roots only) and `skipped_busy_dates`.

## `verify`

Check every file in active manifests exists and matches its `bytes`, `sha256` and `row_count`.
Exits 0 if all match, 1 otherwise (mismatches are listed).

| Option | Default |
|---|---|
| `--snapshot-date YYYY-MM-DD` | all active manifests |

## `raw list` / `raw download`

`raw list` shows raw snapshots with their purge dates (`--json` for machine-readable output).

`raw download` copies one raw snapshot to `<dest>/<location>/<run_id>/`.

| Option | Default | Notes |
|---|---|---|
| `--snapshot-date YYYY-MM-DD` | required | |
| `--dest PATH` | required | |
| `--regions a,b` | all in the current manifest | Ignored for GCP (the raw unit is `catalog`). |
| `--decompress` | off | Write `.json` instead of `.json.zst`. |

## `settings`

Print the effective settings, with secrets shown as `***`, and validate them (exit 2 if invalid).

## `upload-history` (AWS only)

Upload one local AWS table snapshot to the configured store. Never uploads raw data. Exits 2
for any other provider.

| Option | Default | Notes |
|---|---|---|
| `--snapshot-date YYYY-MM-DD` | required | |
| `--source PATH` | `$DATA_DIRECTORY_ROOT` | A local root in the current layout, or the legacy `pricing_aws/parquet/…` tree (a manifest is generated). |
| `--overwrite` | off | Without it, exits 3 if the date already exists in the target. With it, publishes a superseding revision. |
| `--dry-run` | off | Print the manifest and file list only. |

A source manifest that isn't `succeeded` is refused (exit 3).

## Ad hoc AWS download

`python -m src.aws.cli --region us-east-1 (--service-code <code> | --all-services) [--output-dir …]`
downloads one region's AWS price lists into `--output-dir` (or `$DATA_DIRECTORY_ROOT`) for
exploration. It publishes no manifest and isn't part of the pipeline. See `--help`.
