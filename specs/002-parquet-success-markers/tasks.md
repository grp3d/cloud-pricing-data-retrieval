---

description: "Task list for Parquet Partition Success Markers"
---

# Tasks: Parquet Partition Success Markers

**Input**: Design documents from `/specs/002-parquet-success-markers/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/internal-interfaces.md, quickstart.md

**Tests**: Included. plan.md and quickstart.md name specific test files and 12 validation scenarios as part of the design. Within each story, write the tests first and confirm they fail before implementing.

**Organization**: Grouped by user story from spec.md (US1 = P1, US2 = P1, US3 = P2), after a Foundational phase that builds the shared file/lock helpers in `src/partition_markers.py`.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: Which user story the task belongs to (US1, US2, US3)
- File paths are relative to the repository root

## Path Conventions

Single project (plan.md Project Structure): `src/`, `tests/unit/`, `tests/integration/` at the repository root. Run tests with `venv/bin/pytest`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Confirm a green baseline before changing the transform.

- [X] T001 Run the full existing suite with `venv/bin/pytest` from the repository root and record the pass count. Every existing test in `tests/unit/` and `tests/integration/` must pass before any change (a regression baseline for FR-011).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Path helpers, atomic marker creation, and the cross-process lock. Every story depends on these.

**⚠️ CRITICAL**: No user-story work can begin until this phase is complete.

- [X] T002 Create `src/partition_markers.py` with a module docstring summarising the consumer contract (contracts/internal-interfaces.md §1), constants `SUCCESS_MARKER = "_SUCCESS"`, `REGION_COMPLETE_MARKER = "_REGION_COMPLETE"`, `LOCK_DIR_NAME = ".locks"`, and private path helpers `_partition_dir(parquet_root, table, snapshot_date) -> <root>/<table>/snapshot_date=<date>` and `_region_dir(parquet_root, table, snapshot_date, region) -> .../region=<region>`. Build paths with `os.path.join`, using the same `snapshot_date=`/`region=` naming as `_write_parquet_table` in `src/pricing_parquet_transformations.py`.
- [X] T003 Add `_atomic_touch(path: str) -> None` to `src/partition_markers.py`. It creates the parent folder (`os.makedirs(..., exist_ok=True)`), writes an empty temp file `.<basename>.tmp` in the same folder, then `os.replace`s it onto `path`, giving an empty, atomically appearing file (research.md Decision 7, FR-008).
- [X] T004 Add the `partition_lock(parquet_root, table, snapshot_date)` context manager to `src/partition_markers.py`. It opens (creating if needed) `<parquet_root>/.locks/<table>/snapshot_date=<date>.lock`, takes `fcntl.flock(fd, fcntl.LOCK_EX)` (blocking), yields, and releases and closes in `finally`. The lock file must never be created inside any `<table>/snapshot_date=*` folder (research.md Decision 2).
- [X] T005 [P] Create `tests/unit/test_partition_markers.py` with foundational tests using `tmp_path`:
  - `_atomic_touch` produces a zero-byte file and leaves no `.tmp` file behind.
  - `partition_lock` creates its lock file under `.locks/` and nothing under `<table>/`.
  - A second process (`multiprocessing.Process`, `spawn` context) that tries `partition_lock` on the same (table, date) blocks until the first releases. Verify with timestamps or a shared `multiprocessing.Event`.
  - Locks for different dates or tables do not block each other.

**Checkpoint**: `venv/bin/pytest tests/unit/test_partition_markers.py` passes. The helpers are ready.

---

## Phase 3: User Story 1 — Consumers can detect a completed snapshot partition (Priority: P1) 🎯 MVP

**Goal**: Each region records `_REGION_COMPLETE` after writing a table. The last configured region to finish writes `<table>/snapshot_date=<D>/_SUCCESS`, and never earlier (FR-001, FR-002, FR-004, FR-005, FR-007, FR-008, FR-009, FR-011, FR-012, FR-014, FR-015).

**Independent Test**: On a shared `tmp_path` parquet root with `expected_regions=["us-east-1","eu-west-1","ap-northeast-1"]`, call `transform_pricing_to_parquet` for 2 regions, then check there is no `_SUCCESS`. Transform the 3rd, then check `_SUCCESS` exists for every table with data (quickstart scenarios 1, 2, 5, 6, 8, 11).

### Tests for User Story 1 ⚠️ (write first; they must fail)

- [X] T006 [P] [US1] In `tests/unit/test_partition_markers.py`, add `finalize_partition` / `mark_region_complete` tests:
  - (a) Missing regions → `FinalizeStatus.INCOMPLETE` with `missing_regions` listing exactly the missing ones, in expected order; no `_SUCCESS`.
  - (b) All expected regions have `_REGION_COMPLETE` and ≥1 has `part-0.parquet` → `WRITTEN`, and a zero-byte `_SUCCESS` exists at the `snapshot_date=` level only (not in `region=` folders, not at the table root).
  - (c) All complete but no `*.parquet` anywhere → `NO_DATA`, no `_SUCCESS`.
  - (d) A region folder with `part-0.parquet` but no `_REGION_COMPLETE` counts as missing (FR-014).
  - (e) An extra `region=<not-expected>/` folder without a marker is ignored.
  - (f) Calling finalize twice on a ready partition returns `WRITTEN` both times.
  - (g) `mark_region_complete` for a region with no folder creates the folder holding only `_REGION_COMPLETE` (FR-015).
- [X] T007 [P] [US1] Create `tests/integration/test_success_markers_multi_region.py`. Add a JSON fixture helper modelled on `_write_json` in `tests/unit/test_pricing_parquet_transformations.py` (products whose `regionCode` matches the target region, plus one `OnDemand` term so `price_fact` has rows). Add:
  - `test_success_written_only_after_last_region`: 3 regions, check after each transform (quickstart 1–2).
  - `test_region_with_zero_rows_for_table_counts_as_done`: one region's fixture has no terms, so its `price_fact/.../region=<R>/` holds only `_REGION_COMPLETE`, and `price_fact` still gets `_SUCCESS` (quickstart 5).
  - `test_all_regions_empty_table_gets_no_success` (quickstart 6, FR-007).
  - `test_markers_do_not_change_row_counts`: read each table with `pyarrow.dataset.dataset(<root>/<table>, partitioning="hive")`; row counts equal the sum of the per-region DataFrames (quickstart 11, FR-011).
  - `test_removed_region_folder_ignored` (quickstart 12).
- [X] T008 [P] [US1] In `tests/unit/test_pricing_parquet_transformations.py`, add tests that `TransformResult.marker_outcomes` has one entry per table (all 5 keys). With `expected_regions=[request.region]`, tables with rows report `"WRITTEN"`. The existing two tests must keep passing unchanged.

### Implementation for User Story 1

- [X] T009 [US1] In `src/partition_markers.py`, add `FinalizeStatus(str, Enum)` (`WRITTEN`, `INCOMPLETE`, `NO_DATA`) and the `FinalizeOutcome` dataclass (`status`, `missing_regions: List[str] = field(default_factory=list)`), as in contracts §2.
- [X] T010 [US1] In `src/partition_markers.py`, implement `mark_region_complete(parquet_root, table, snapshot_date, region) -> str` using `_atomic_touch` on `_region_dir(...)/_REGION_COMPLETE`. Return the path.
- [X] T011 [US1] In `src/partition_markers.py`, implement `finalize_partition(parquet_root, table, snapshot_date, expected_regions) -> FinalizeOutcome`, which runs entirely inside `partition_lock(...)`:
  1. `missing = [r for r in expected_regions if not os.path.isfile(<region_dir>/_REGION_COMPLETE)]`; if any → `INCOMPLETE(missing)`.
  2. If none of the expected region folders contains a file matching `*.parquet` → `NO_DATA`.
  3. Otherwise `_atomic_touch(<partition_dir>/_SUCCESS)` → `WRITTEN`.
  Only the expected regions' folders and the one `snapshot_date=` folder are read (research.md Decision 5).
- [X] T012 [US1] In `src/pricing_parquet_transformations.py`:
  - Add `expected_regions: Optional[List[str]] = None` to `TransformRequest`. In `__post_init__`, when it's `None`, set it to `aws_regions.resolve_regions()` (import from `src.aws_regions`).
  - Add `marker_outcomes: Dict[str, str] = field(default_factory=dict)` and `marker_errors: List[str] = field(default_factory=list)` to `TransformResult`.
- [X] T013 [US1] Rework the per-table loop in `transform_pricing_to_parquet` (`src/pricing_parquet_transformations.py`). For each table in `table_data`:
  - If `df` is non-empty, write it with `_write_parquet_table` (unchanged).
  - Then, if the write succeeded or `df` was empty, call `mark_region_complete(...)` and then `finalize_partition(..., request.expected_regions)`. Store `outcome.status.value`, or `f"INCOMPLETE (missing: {', '.join(outcome.missing_regions)})"`, in `result.marker_outcomes[table_name]`.
  - If the data write failed, store `"SKIPPED (write failed)"` and don't mark or finalize.
  - Wrap the marker calls in `try/except OSError as e`, appending `f"Marker error for {table_name}: {e}"` to `result.marker_errors`.
  - Keep `tables_written`, `tables_skipped` and `success` exactly as today.
- [X] T014 [US1] In `src/dagster_app/assets/pricing_assets.py`:
  - Add a `pricing_regions: PricingRegionsResource` parameter to the `transform_to_parquet` op (import from `src.dagster_app.resources`).
  - Pass `expected_regions=pricing_regions.get_regions()` into `TransformRequest`.
  - After the existing checks, log each `result.marker_outcomes` item as `f"Marker {table}: {outcome}"`. Use `context.log.info` for `WRITTEN`/`NO_DATA` and `context.log.warning` for `INCOMPLETE (...)`/`SKIPPED (...)`, so incomplete regions stand out in the Dagster UI.
  - If `result.marker_errors` is non-empty, log each with `context.log.error` and raise `RuntimeError("Failed to write partition markers: ...")` (FR-010).
- [X] T015 [US1] Check that the Dagster wiring still loads: run `venv/bin/python -c "from src.dagster_app.definitions import defs; defs.get_job_def('pricing_pipeline_single_region')"`. The `pricing_regions` resource is already registered in `src/dagster_app/definitions.py`; fix anything that fails. Then run `venv/bin/pytest tests/unit/test_pricing_schedule.py tests/unit/test_pricing_sensors.py`.

**Checkpoint**: T006–T008 pass. After a clean multi-region run, the `_SUCCESS` markers are correct. (Re-runs are not yet safe; that's US3.)

---

## Phase 4: User Story 2 — Only the partition being processed is touched (Priority: P1)

**Goal**: Show and guarantee that a run for date D never creates, modifies or deletes anything under any other `snapshot_date=` folder, and that markers are never written at the table root or in `region=` folders (FR-003, FR-009, SC-003).

**Independent Test**: Seed `snapshot_date=2026-09-20` for every table (with and without `_SUCCESS`), run all regions for 2026-09-27, and check the 2026-09-20 tree is identical: same file list, sizes and `st_mtime_ns` (quickstart scenario 7).

### Tests for User Story 2 ⚠️

- [X] T016 [P] [US2] In `tests/integration/test_success_markers_multi_region.py`, add `test_other_snapshot_dates_untouched`:
  - Build `2026-09-20` partitions for all 5 tables by running the transform with `snapshot_date="2026-09-20"`.
  - Delete `_SUCCESS` from two of those tables so both states are covered.
  - Snapshot `{relpath: (size, st_mtime_ns)}` for everything under `*/snapshot_date=2026-09-20/`.
  - Run all regions for `2026-09-27`.
  - Assert the snapshot is identical and no new files appeared under `2026-09-20`.
- [X] T017 [US2] In `tests/integration/test_success_markers_multi_region.py`, add `test_marker_locations`: after a complete run, `glob` for `_SUCCESS` under the parquet root and assert every match's parent folder name starts with `snapshot_date=`. Glob for `_REGION_COMPLETE` and assert every parent starts with `region=`. Assert no `_SUCCESS` sits directly under `<root>/<table>/` (FR-009).

### Implementation for User Story 2

- [X] T018 [US2] In `src/partition_markers.py`, add a `_validate_snapshot_date(snapshot_date: str)` guard called at the top of every public function. It raises `ValueError` unless the value matches `^\d{4}-\d{2}-\d{2}$`, so an empty or malformed date can never resolve to the table root or another partition (FR-003). Add a unit test for `""`, `"2026-9-1"` and `"../x"` in `tests/unit/test_partition_markers.py`.

**Checkpoint**: T016–T018 pass. US1 tests still pass.

---

## Phase 5: User Story 3 — Failed or re-run partitions are never falsely marked complete (Priority: P2)

**Goal**: Remove `_SUCCESS` and the region's `_REGION_COMPLETE` (and stale data) before a region rewrites. Never mark a region with input parse errors or failed writes. Serialize invalidate and finalize so concurrent regions can't produce a false `_SUCCESS`; serialize overlapping runs of the same region; fail the run on input parse errors (FR-006, FR-010, FR-013, FR-016, FR-017, research.md Decisions 2, 4, 6, 8).

**Independent Test**: Make one table's write fail and check only that table lacks `_SUCCESS`. Re-run a region of a ready date and check `_SUCCESS` is gone before the data changes and back afterwards. Run concurrent processes and check no `_SUCCESS` appears while a region is incomplete (quickstart scenarios 3, 4, 9, 10).

### Tests for User Story 3 ⚠️

- [X] T019 [P] [US3] In `tests/unit/test_partition_markers.py`, add `invalidate_region` tests:
  - It removes `snapshot_date=<D>/_SUCCESS`, `region=<R>/_REGION_COMPLETE` and `region=<R>/*.parquet`.
  - It leaves other regions' folders untouched.
  - Missing files don't raise.
  - It never touches another date.
- [X] T020 [US3] In `tests/unit/test_partition_markers.py`, add a concurrency stress test with `multiprocessing` (`spawn`).
  - Setup: 3 expected regions; each worker process loops N=50 times: `invalidate_region` → write a dummy `part-0.parquet` → `mark_region_complete` → `finalize_partition`.
  - Checker process: loops for the workers' lifetime. Each time it takes `partition_lock`, reads whether `_SUCCESS` exists and which of the 3 `_REGION_COMPLETE` files exist, then releases.
  - Assert on every check: if `_SUCCESS` exists, all 3 region markers exist.
  - After all workers finish and one last `finalize_partition` runs: `_SUCCESS` exists (quickstart 9, SC-002).
- [X] T021 [P] [US3] In `tests/integration/test_success_markers_multi_region.py`, add:
  - `test_rerun_region_removes_then_restores_success`: complete the date; monkeypatch `src.pricing_parquet_transformations._write_parquet_table` to assert `_SUCCESS` is absent at the moment of writing, then delegate to the original. Re-run one region and assert `_SUCCESS` exists afterwards (quickstart 4, FR-006).
  - `test_rerun_with_now_empty_table_removes_stale_data`: a region previously had `price_fact` rows and re-runs with no terms, so `region=<R>/` holds only `_REGION_COMPLETE` (research.md Decision 6).
  - `test_corrupt_input_file_blocks_region`: one region gets an extra invalid JSON file, so that region has no `_REGION_COMPLETE` in any table and there's no `_SUCCESS`. Re-running it with valid files gives `_SUCCESS` (quickstart 3).
  - `test_failed_table_write_only_blocks_that_table`: monkeypatch `_write_parquet_table` to raise for `product_attribute` only, so `product_attribute` lacks `_SUCCESS` and the other tables have it (US3-1, FR-005).
- [X] T022 [P] [US3] In `tests/unit/test_pricing_parquet_transformations.py`, add `test_marker_failure_recorded`: monkeypatch `src.pricing_parquet_transformations.mark_region_complete` to raise `PermissionError`, so `result.marker_errors` is non-empty and `result.success` is still `True` when data was written. Add a Dagster op test (`build_op_context` with `resources={"pricing_regions": PricingRegionsResource(regions=[...])}` and a `tmp_path` raw dir, with `_data_root` monkeypatched to `tmp_path`) asserting `transform_to_parquet` raises `RuntimeError` when `marker_errors` is non-empty (quickstart 10, FR-010).
- [X] T023 [P] [US3] In `tests/unit/test_region_run_lock.py`, test that:
  - two `spawn` processes taking `region_run_lock` for the same (date, region) run one after the other;
  - different regions don't block each other;
  - a process that holds `region_run_lock` and then takes `partition_lock` doesn't deadlock against another process that only calls `finalize_partition` (FR-017).

### Implementation for User Story 3

- [X] T024 [US3] In `src/partition_markers.py`, implement `invalidate_region(parquet_root, table, snapshot_date, region) -> None` inside `partition_lock(...)`. In this order, ignoring `FileNotFoundError`:
  1. Remove `<partition_dir>/_SUCCESS`.
  2. Remove `<region_dir>/_REGION_COMPLETE`.
  3. Remove every `*.parquet` in `<region_dir>`.
  It must not touch other regions or dates.
- [X] T025 [US3] In `src/partition_markers.py`, implement `region_run_lock(parquet_root, snapshot_date, region)` the same way as `partition_lock` (refactor both onto a shared private `_flock(path)` helper), with lock path `<parquet_root>/.locks/_regions/snapshot_date=<date>/region=<region>.lock` (FR-017).
- [X] T026 [US3] In `transform_pricing_to_parquet` (`src/pricing_parquet_transformations.py`), call `invalidate_region(...)` for every table **before** writing it, including tables whose `df` is empty (inside the same `try/except OSError` that feeds `marker_errors`). If invalidation fails, skip the write for that table: record `"SKIPPED (invalidate failed)"` and add the table to `result.errors`.
- [X] T027 [US3] In `transform_pricing_to_parquet`, add `parse_failed_files: List[str] = field(default_factory=list)` to `TransformResult` and append each input JSON path skipped in the parse loop (the existing `except Exception` branch). Treat the region as `parse_failed` when that list is non-empty. When `parse_failed`, still write data but don't mark or finalize any table. Record `"SKIPPED (input parse errors)"` for each and log one warning: `"Region <R> not marked complete: <n> input file(s) failed to parse"` (research.md Decision 4).
- [X] T028 [US3] In `transform_pricing_to_parquet` (`src/pricing_parquet_transformations.py`), wrap the whole per-table remove/write/mark/check loop in `with region_run_lock(request.parquet_root, request.snapshot_date, request.region):`. Parsing JSON stays outside the lock (FR-017).
- [X] T029 [US3] In `src/dagster_app/assets/pricing_assets.py::transform_to_parquet`, after logging marker outcomes, raise `RuntimeError(f"Input files failed to parse; region not marked complete: {result.parse_failed_files}")` if `result.parse_failed_files` is non-empty (FR-016). Extend T022's op test to cover this raise.

**Checkpoint**: The whole suite passes: `venv/bin/pytest`.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T030 [P] Add a "Partition success markers" section to `README.md`. Cover:
  - The consumer contract: wait for `<table>/snapshot_date=<D>/_SUCCESS`.
  - `_REGION_COMPLETE` is internal.
  - `.locks/` is safe to ignore.
  - The marker is withheld until every region in `PRICING_REGIONS` / `PricingRegionsResource` succeeds.
  - How to re-run a failed region for a date.
- [X] T031 [P] Add `.locks/` handling notes (it's harmless, and deleting it while no run is active is safe) to the module docstring in `src/partition_markers.py`.
- [X] T032 Run the full suite `venv/bin/pytest` and compare with the T001 baseline: every existing test still passes and all new tests pass.
- [ ] T033 Carry out the manual Dagster check in `specs/002-parquet-success-markers/quickstart.md` §2 with `PRICING_REGIONS=us-east-1,us-west-2`. Record the observed `find ... -name '_*'` output in the PR description.

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (T001)** → **Foundational (T002–T005)** → user stories.
- **US1 (T006–T015)** depends on Foundational.
- **US2 (T016–T018)** depends on US1 (it checks where US1's markers are written). T018 only needs Foundational.
- **US3 (T019–T029)** depends on US1 (it extends the same transform loop). T019, T020, T023, T024 and T025 only need Foundational plus T009–T011.
- **Polish (T030–T033)** after all stories.

### Within-story order

- Tests first (they must fail), then `partition_markers.py`, then `pricing_parquet_transformations.py`, then `pricing_assets.py`.
- T009 → T010 → T011 (same file). T012 → T013 (same file). T013 → T014.
- T024 → T025 (same file). T026 → T027 → T028 (same function). T028 → T029.

### Story completion order

```text
T001 → T002–T005 → US1 (MVP) → US2 ─┐
                             └→ US3 ─┴→ Polish
```

## Parallel Examples

**Foundational**: T005 (tests) can be written while T002–T004 are being implemented; it's a different file.

**US1**: T006, T007 and T008 are three different test files and can be written in parallel:

```text
T006 tests/unit/test_partition_markers.py
T007 tests/integration/test_success_markers_multi_region.py
T008 tests/unit/test_pricing_parquet_transformations.py
```

**US2**: T016 and T018 in parallel (different files); T017 after T016 (same file).

**US3**: T019, T021, T022 and T023 are in different files and can be written in parallel; T020 after T019 (same file). T024 can start while T021 and T022 are being written.

## Implementation Strategy

### MVP (US1 only)

1. T001–T005, then T006–T015.
2. **Stop and validate**: a clean multi-region run writes `_SUCCESS` only after the last region. That already gives consumers a usable "ready" signal for normal weekly runs.

### Incremental delivery

1. MVP (US1): markers on clean runs.
2. US2: guarantees against touching other partitions, locked in by tests.
3. US3: safe re-runs, failure handling, concurrency hardening. **Do this before relying on markers in production**, because without invalidation a re-run of a ready date keeps its old `_SUCCESS` while data is rewritten.
4. Polish: docs and the manual check.
