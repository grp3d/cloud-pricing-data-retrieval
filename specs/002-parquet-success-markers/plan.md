# Implementation Plan: Parquet Partition Success Markers

**Branch**: `002-parquet-success-markers` | **Date**: 2026-09-26 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/002-parquet-success-markers/spec.md`

## Summary

Write an empty `_SUCCESS` file into `<table>/snapshot_date=<D>/` once **every currently configured region** has written that table for `D`. Partitions for other dates are never touched.

Each region run already writes only its own `region=<R>/` folder. It will now also record its own success with an empty `_REGION_COMPLETE` file there. After that, still inside the same `transform_to_parquet` op, it runs a completion check. So whichever region finishes last writes `_SUCCESS`, and a manual re-run of a failed region completes the date automatically. The check (plus the "remove markers before rewriting" step) runs under a cross-process `fcntl.flock` lock per (table, date), which rules out races between regions finishing at once and re-runs. A second per-(date, region) lock, held for the whole write loop, stops two runs of the same region from writing at the same time (FR-017). If an input file fails to parse, the region isn't marked and the run fails (FR-016). All marker logic lives in a new `src/partition_markers.py`. The transform calls it, and the Dagster op passes in the configured region list from the existing `PricingRegionsResource`.

## Technical Context

**Language/Version**: Python 3.14 (repo `venv`)

**Primary Dependencies**: dagster 1.13, pyarrow 25, pandas. Standard library `fcntl`, `os`, `contextlib`. No new dependencies.

**Storage**: Local/mounted filesystem under `<data_root>/pricing_aws/parquet`, Hive-style `snapshot_date=`/`region=` layout (unchanged)

**Testing**: pytest (`tests/unit`, `tests/integration`), `tmp_path`, `multiprocessing` for lock contention

**Target Platform**: macOS/Linux host running `dagster dev` / the default multiprocess run launcher (POSIX `flock` required)

**Project Type**: Data pipeline: a library module plus Dagster orchestration

**Performance Goals**: Marker work adds a handful of small file operations per table per region. Negligible next to the download and transform.

**Constraints**: Lock held only for invalidate and finalize (milliseconds), never while writing data, so regions keep writing in parallel. `flock` is not guaranteed on NFS/SMB (out of scope per spec).

**Scale/Scope**: 5 tables × 7 configured regions per snapshot date. Weekly schedule.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

`.specify/memory/constitution.md` is still the unfilled template: no principles have been ratified, so there are no gates to enforce. **Result: PASS (no constraints).** The design follows the conventions established in feature 001 anyway: library logic kept separate from the Dagster layer, the resource as single source of truth for regions, and unit plus integration tests.

*Post-design re-check (after Phase 1)*: still PASS. One new module, no new dependencies, no new jobs, schedules or sensors.

## Project Structure

### Documentation (this feature)

```text
specs/002-parquet-success-markers/
├── plan.md              # This file
├── research.md          # Phase 0 — design decisions (lock, check placement, completion rule)
├── data-model.md        # Phase 1 — on-disk layout, marker entities, state transitions
├── quickstart.md        # Phase 1 — validation scenarios + manual Dagster check
├── contracts/
│   └── internal-interfaces.md   # consumer contract + Python interfaces
├── checklists/requirements.md
└── tasks.md             # Phase 2 (/speckit-tasks — not created here)
```

### Source Code (repository root)

```text
src/
├── partition_markers.py                 # NEW — lock, invalidate_region, mark_region_complete, finalize_partition
├── pricing_parquet_transformations.py   # CHANGED — per-table invalidate → write → mark → finalize; new request/result fields
├── aws_regions.py                       # unchanged (resolve_regions used as fallback)
└── dagster_app/
    ├── assets/pricing_assets.py         # CHANGED — inject PricingRegionsResource, log outcomes, raise on marker_errors
    ├── resources.py                     # unchanged
    ├── jobs/ schedules/ sensors/        # unchanged
    └── definitions.py                   # unchanged (pricing_regions resource already registered)

tests/
├── unit/
│   ├── test_partition_markers.py                    # NEW — completion rule, invalidate, atomicity, lock contention
│   ├── test_region_run_lock.py                      # NEW — same-region serialization, no deadlock (FR-017)
│   └── test_pricing_parquet_transformations.py      # EXTENDED — marker fields, empty-table, parse-error cases
└── integration/
    └── test_success_markers_multi_region.py         # NEW — multi-region lifecycle on a shared parquet_root
```

**Structure Decision**: Single-project layout, as it is today. The new module sits beside `pricing_parquet_transformations.py` because it's pure filesystem logic that doesn't depend on Dagster. That keeps it testable without the orchestrator, following the same library/Dagster split used in 001.

## Implementation Outline (for /speckit-tasks)

1. **`partition_markers.py`**: constants, `partition_lock`, `invalidate_region`, `mark_region_complete`, `finalize_partition`, and `FinalizeStatus`/`FinalizeOutcome` (contracts §2). Add unit tests alongside.
2. **Transform integration**: add `expected_regions`, `marker_outcomes` and `marker_errors`. Change the per-table loop to always invalidate (including 0-row tables), write, then mark and finalize only when the region is fully successful (research.md Decisions 4 and 6).
3. **Dagster op**: inject `pricing_regions`, pass `expected_regions`, log outcomes, raise on `marker_errors`.
4. **Integration and concurrency tests**: quickstart scenarios 1–12.
5. **Docs**: add a short README section on the `_SUCCESS` consumer contract.

## Risks

| Risk | Mitigation |
|---|---|
| A region with a persistently corrupt input file blocks `_SUCCESS` for that date | Intended (it's incomplete). The op logs a clear warning naming the file. A re-run downloads it again. |
| Config changes between region runs (a region added mid-date) | Spec behavior: the new region becomes required. Its outcome is logged as `INCOMPLETE (missing: …)` so the cause is obvious. |
| Filesystem without working `flock` | Out of scope (local FS only). Documented in research.md Decision 2. |

## Complexity Tracking

No constitution violations; nothing to justify.
