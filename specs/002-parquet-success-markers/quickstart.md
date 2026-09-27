# Quickstart: Validating Parquet Partition Success Markers

## Prerequisites

- Repo virtualenv: `source venv/bin/activate` (Python 3.14, dagster 1.13, pyarrow 25)
- No AWS credentials needed for the automated tests. The manual run needs the same credentials as today.

## 1. Automated tests

```bash
pytest tests/unit/test_partition_markers.py tests/unit/test_region_run_lock.py \
       tests/unit/test_pricing_parquet_transformations.py \
       tests/integration/test_success_markers_multi_region.py -v
pytest            # full suite — existing tests must still pass
```

The tests must cover these scenarios (spec reference in brackets):

| # | Scenario | Expected |
|---|---|---|
| 1 | 3 expected regions, 2 transformed | no `_SUCCESS` in any table. Outcome `INCOMPLETE (missing: <3rd>)` [US1-2, US1-4] |
| 2 | 3rd region transformed | `_SUCCESS` in each table that has data [US1-1, US1-3] |
| 3 | One region's input JSON is corrupt | that region has no `_REGION_COMPLETE`, no `_SUCCESS` anywhere. Re-running it with good input → `_SUCCESS` [US1-5, Decision 4] |
| 4 | Re-run one region of a READY date | `_SUCCESS` is removed before data changes and comes back after the re-run [US3-2, FR-006/013] |
| 5 | Region has 0 rows for a table | `region=<R>/` holds only `_REGION_COMPLETE`. Counts as done [FR-015] |
| 6 | Every region has 0 rows for a table | no `_SUCCESS`, outcome `NO_DATA` [FR-007] |
| 7 | Older date `snapshot_date=D-7` exists | tree is byte-for-byte unchanged, including mtimes [US2, FR-003] |
| 8 | Region folder has data but no `_REGION_COMPLETE` (simulated crash) | not counted as done [FR-014] |
| 9 | N processes finalize and invalidate at the same time (`multiprocessing`) | never `_SUCCESS` while a region is incomplete. Exactly one final `_SUCCESS` when all are complete [Edge: concurrency] |
| 10 | Marker write fails (read-only folder) | `marker_errors` non-empty, the op raises [FR-010] |
| 11 | Read the table with `pyarrow.dataset` with markers present | same row count as without them [FR-011, SC-004] |
| 12 | Configured list shrinks; a stale `region=<old>/` has no marker | ignored, `_SUCCESS` written [Edge: region list change] |
| 13 | Two processes transform the same region and date at once | they run one after the other (region-run lock). The data file is valid and complete [FR-017] |
| 14 | Dagster op with a corrupt input file | data written, no `_REGION_COMPLETE`, op raises `RuntimeError` naming the file [FR-016] |

## 2. Manual end-to-end check (Dagster)

```bash
PRICING_REGIONS=us-east-1,us-west-2 dagster dev -m src.dagster_app.definitions
```

1. In the UI, open `trigger_configured_regions_sensor` → **Test Sensor** → launch the 2 runs.
2. While the runs are going: `ls <data_root>/pricing_aws/parquet/price_fact/snapshot_date=<today>/` has **no** `_SUCCESS`.
3. After both runs succeed:
   ```bash
   find <data_root>/pricing_aws/parquet -path '*snapshot_date=<today>*' -name '_*' | sort
   ```
   Expected: one `_SUCCESS` per table with data, and one `_REGION_COMPLETE` per (table, region).
4. Each run's logs contain a marker-outcome line per table. The first run to finish shows `INCOMPLETE (missing: …)`, the last shows `WRITTEN`.
5. Launch `pricing_pipeline_single_region` manually for `us-west-2` with the same `snapshot_date`. While it runs, `_SUCCESS` disappears. It comes back when the run succeeds.
6. Earlier `snapshot_date=` folders show no new files and no new modification times.
