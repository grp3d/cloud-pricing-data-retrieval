# Phase 1 Data Model: Multi-Region Parallel Pricing Downloads

This feature has no database or persisted schema change — output stays as region-labeled files on
disk, exactly as today (`pricing-<service>-<region>.<ext>` raw files; region-partitioned parquet).
The entities below are the in-memory/config domain objects that realize the spec's Key Entities, and
where each one already exists vs. is new.

## Region

Represents one AWS region pricing is collected for.

| Field | Type | Notes |
|---|---|---|
| `region_code` | `str` | e.g. `"us-east-1"`. Same shape as the existing `PricingDownloadConfig.region` / `TransformConfig.region` fields — no change to those. |

**Source**: `src/aws_regions.py` (**new**) — `DEFAULT_PRICING_REGIONS: List[str]` holds the 7 codes
from FR-002; `resolve_regions() -> List[str]` is the single place the effective list is computed
(FR-001, FR-009), reading the optional `PRICING_REGIONS` env var override.

**Validation**: No new validation is introduced by this feature. Invalid/unsupported region codes
continue to surface exactly as they do today — as a `ValueError` from
`PricingDataManager.validate_region()` — but now scoped to that region's own Dagster run, so it
becomes that region's Region Run Result failure rather than aborting a shared process (Edge Cases).

## Region Run Result

The outcome of processing one region. **Not a new persisted type** — it *is* the existing
`PricingJobResult` (from `aws_pricing_api.py`) plus `TransformResult` (from
`pricing_parquet_transformations.py`), scoped to one Dagster run per Decision 1 in research.md.

| Field (existing) | Type | Meaning |
|---|---|---|
| `downloaded_files` | `List[str]` | Raw files successfully downloaded for this region. |
| `failed_services` | `List[str]` | Services with no data / failed downloads in this region. |
| `success` | `bool` | `True` once at least one service downloaded (existing semantics, unchanged). |
| `errors` | `List[str]` | Non-fatal warnings surfaced during the run. |
| `tables_written` / `tables_skipped` (Transform) | `List[str]` | Parquet tables produced/skipped for this region. |

**Mapping to FR-006/FR-007**: "success" = this region's Dagster run succeeded with `success=True`;
"partial success" = run succeeded but `failed_services`/`tables_skipped` is non-empty; "failed" = the
run raised (e.g. `RuntimeError("No pricing files were downloaded.")`, already raised today). Because
each region is its own independent run (Decision 1), FR-007 requires only that each run report its own
status correctly and not affect any other region's run — there is no combined "did every region fail"
status to compute across a tick, and no aggregation code is needed.

## Scheduled Run

One weekly trigger of `pricing_weekly_schedule`. **Not a new type** — it corresponds to one schedule
"tick", which now yields *N* `RunRequest`s (one per resolved region) instead of one.

| Field | Type | Notes |
|---|---|---|
| `timestamp` | `str` | Existing `_run_timestamp()`, shared by all regions in the same tick so their raw output lands under the same `raw/<timestamp>/` directory grouping, distinguished by filename region suffix. |
| `snapshot_date` | `str` | Existing `_snapshot_date()`, shared across the tick's regions. |
| `run_key` (per region) | `str` | **New**: `f"{timestamp}-{region}"` — makes each region's `RunRequest` unique/idempotent per tick (Dagster requires distinct `run_key`s to dedupe retried schedule evaluations). |

## Schedule Cadence

The recurring trigger definition. **Not a new type** — it's the existing `@schedule(...)` decorator's
`cron_schedule` argument, changed per Decision 3 in research.md from `"0 13 * * *"` (daily) to
`"0 13 * * 1"` (weekly, Monday 13:00 UTC), per FR-008.
