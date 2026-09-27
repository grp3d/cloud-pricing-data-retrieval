# Cloud Pricing Data Retrieval

Downloads AWS service pricing data and stores it as raw JSON/CSV plus transformed Parquet tables.

## Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

## Ad hoc CLI usage (single region)

```bash
python -m src.aws_pricing_cli --region us-east-1 --all-services
```

See `python -m src.aws_pricing_cli --help` for all options. This path is unaffected by the
scheduled multi-region pipeline described below — it always operates on the single `--region`
you pass it.

## Scheduled pipeline (Dagster)

```bash
dagster dev -m src.dagster_app.definitions
```

`pricing_weekly_schedule` runs once a week (Monday 13:00 UTC) and downloads + transforms pricing
data for a configurable list of AWS regions, one region per Dagster run, executed concurrently.

- **Region list**: defaults to `us-east-1, us-east-2, us-west-1, us-west-2, eu-west-1, eu-west-2,
  ap-northeast-1` (`src/aws_regions.py`). Configurable two ways, neither requiring a code change:
  - Set the `PRICING_REGIONS` environment variable to a comma-separated list of region codes, or
  - Override the `pricing_regions` resource directly in `Definitions(resources={...})`
    (`src/dagster_app/resources.py::PricingRegionsResource`).
- **Concurrency**: each region's run is tagged with the Dagster concurrency-pool key
  `pricing-region-download`. By default no explicit pool limit is configured, so concurrency is
  naturally capped at the number of configured regions — it can never exceed that. An operator can
  additionally set a lower limit for the `pricing-region-download` pool via Dagster instance
  configuration at any time, with no code change required.
- **Cadence**: `pricing_weekly_schedule`'s cron is `0 13 * * 1` (weekly, Monday 13:00 UTC).

### Job, schedule, and on-demand trigger

There's one job, `pricing_pipeline_single_region` — every run covers exactly one region, whatever
region its `PricingDownloadConfig` says. Three ways to launch it:

- **Manually**, from the Dagster UI Launchpad, for ad hoc / backfill use — set one region yourself.
- **`pricing_weekly_schedule`** — launches one run per configured region, once a week (Monday 13:00 UTC).
- **`trigger_configured_regions_sensor`** — the on-demand version of the above. Open the Dagster UI's
  **Sensors** tab → `trigger_configured_regions_sensor` → **Test Sensor** to launch the same
  one-run-per-region fan-out immediately, without waiting for Monday. It's stopped by default
  (`default_status=DefaultSensorStatus.STOPPED`) so it never fires on its own — "Test Sensor" works
  regardless of whether it's turned on.

### Partition success markers

Each table's snapshot partition gets an empty `_SUCCESS` file once it is complete:

```text
<data_root>/pricing_aws/parquet/<table>/snapshot_date=<YYYY-MM-DD>/_SUCCESS
```

- **For consumers**: wait for `_SUCCESS` before reading a table's partition. If it's there, every
  configured region has finished writing that table for that date. If it isn't, the partition is
  still being written, a region failed, or the table had no rows in any region.
- **When it's written**: only once every region in the configured list (`PRICING_REGIONS` /
  `PricingRegionsResource`, read at the time of the check) has written the table. Whichever
  region's run finishes last writes it. A region whose run fails, or whose input pricing files
  fail to parse, holds the marker back.
- **Re-running a failed region**: launch `pricing_pipeline_single_region` for that region with the
  same `snapshot_date` (Launchpad → `transform_to_parquet` config). When it succeeds and all other
  regions are already done, it writes `_SUCCESS`. Re-running a region also removes the existing
  `_SUCCESS` before it rewrites any data, and restores it on success.
- **Internal files**: `region=<R>/_REGION_COMPLETE` records that one region finished a table, and
  `<parquet_root>/.locks/` holds lock files that coordinate concurrent runs. Neither is meant for
  consumers, and readers skip files and folders starting with `_` or `.`.
- Runs never touch other dates' partitions, and partitions written before this feature have no
  markers.

## Testing

```bash
pip install pytest pytest-asyncio httpx   # already in requirements.txt
pytest
```

- `tests/unit/` — region list resolution, schedule fan-out/cadence/tagging, async download core,
  partition markers and locks
- `tests/integration/` — multi-region failure isolation, partition success marker lifecycle
