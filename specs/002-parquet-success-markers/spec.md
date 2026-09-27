# Feature Specification: Parquet Partition Success Markers

**Feature Branch**: `002-parquet-success-markers`

**Created**: 2026-09-26

**Status**: Draft

**Input**: User description: "The process that creates parquet files needs to drop a _SUCCESS file into the partition directory of each of the tables upon the successful completion of the table files being written. The _SUCCESS files should only be written into the directory of the NEW parition being processed. For example, if we're processing day 2026-09-27, then the success files will be written into .../parquet/<table>/snapshot_date=2026-09-27/"

## Context

The pricing pipeline produces five tables (`service_dim`, `region_dim`, `product_dim`, `product_attribute`, `price_fact`). Each table is laid out as `.../parquet/<table>/snapshot_date=<YYYY-MM-DD>/region=<region>/`. A single snapshot date is populated by several independent per-region runs (one run per configured region, all sharing the same snapshot date), each of which writes only its own `region=<region>` sub-directory.

Downstream consumers currently have no reliable way to tell whether a snapshot-date partition is finished or still being written. A `_SUCCESS` marker in the snapshot-date partition directory is the widely used convention for signalling "this partition is complete and safe to read."

## Clarifications

### Session 2026-09-26

- Q: When deciding whether "every configured region" has finished a table for a date, which list of regions should the pipeline check against? → A: The current configured region list at the moment the check runs, regardless of which run triggered it (scheduled batch, sensor, or manual single-region re-run).
- Q: Once a region run has finished writing a table, how should the pipeline remember that, so a later run can tell which regions are already done? → A: A per-region completion file with a distinct name (`_REGION_COMPLETE`) inside each `region=<region>` folder. It is removed before that region is rewritten, and `_SUCCESS` stays at the snapshot-date level only.
- Q: When a region run succeeds but has zero rows for a table, how should that region be recorded as done for the table? → A: Create the `region=<region>` folder containing only `_REGION_COMPLETE` (no data file).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Consumers can detect a completed snapshot partition (Priority: P1)

As a downstream consumer of the pricing tables (a query job, a sync process, or an analyst), I want a `_SUCCESS` marker to appear in each table's snapshot-date partition directory once that table's data for the date has been fully written, so that I can wait for the marker before reading and never read a half-written partition.

**Why this priority**: This is the entire value of the feature — a reliable "ready to read" signal per table per snapshot date.

**Independent Test**: Run the pipeline for snapshot date 2026-09-27 and verify that `.../parquet/<table>/snapshot_date=2026-09-27/_SUCCESS` exists for every table that received data, and that it appears only after that table's data files for the date are all present.

**Acceptance Scenarios**:

1. **Given** a run for snapshot date 2026-09-27, **When** all data files for table `price_fact` for that date have been written successfully, **Then** a `_SUCCESS` file exists at `.../parquet/price_fact/snapshot_date=2026-09-27/_SUCCESS`.
2. **Given** a run for snapshot date 2026-09-27 that is still writing data files for a table, **When** a consumer checks that table's partition directory, **Then** no `_SUCCESS` file is present yet.
3. **Given** a run for snapshot date 2026-09-27 completes, **When** the five table directories are inspected, **Then** each table that received data has its own `_SUCCESS` marker in its own `snapshot_date=2026-09-27` directory, independent of the others.
4. **Given** 7 configured regions for snapshot date 2026-09-27, **When** 6 region runs have written `price_fact` and the 7th is still running, **Then** `price_fact/snapshot_date=2026-09-27/` has no `_SUCCESS` marker; **When** the 7th region then writes `price_fact` successfully, **Then** the marker is written.
5. **Given** 7 configured regions for snapshot date 2026-09-27, **When** 6 regions succeed and 1 region fails, **Then** no table has a `_SUCCESS` marker for 2026-09-27; **When** the failed region is later re-run successfully for the same date, **Then** the markers are written.

---

### User Story 2 - Only the partition being processed is touched (Priority: P1)

As the operator of the pipeline, I want the marker to be written only into the snapshot-date partition currently being processed, so that historical partitions are never modified, re-stamped, or given misleading timestamps.

**Why this priority**: Writing markers into old partitions would corrupt the "ready" signal consumers rely on and could trigger unwanted reprocessing downstream.

**Independent Test**: With existing partitions for 2026-09-20 (with or without markers) already on disk, run the pipeline for 2026-09-27 and verify the 2026-09-20 directories — including their marker presence and modification times — are unchanged.

**Acceptance Scenarios**:

1. **Given** existing partitions `snapshot_date=2026-09-20` for every table, **When** a run for 2026-09-27 completes, **Then** no file in any `snapshot_date=2026-09-20` directory is created, modified, or deleted.
2. **Given** an existing partition from an earlier date that has no `_SUCCESS` marker, **When** a run for a later date completes, **Then** the earlier partition still has no marker.
3. **Given** a run for 2026-09-27, **When** markers are written, **Then** they are written only at the snapshot-date level (`snapshot_date=2026-09-27/_SUCCESS`), not at the table root and not inside `region=<region>` sub-directories.

---

### User Story 3 - Failed or re-run partitions are never falsely marked complete (Priority: P2)

As a downstream consumer, I want a partition to be un-marked while it is being re-written and never marked if its write fails, so that the presence of `_SUCCESS` is always trustworthy.

**Why this priority**: A marker that can be present on incomplete or failed data is worse than no marker at all, but this is a robustness refinement on top of the core behavior.

**Independent Test**: Force a write failure for one table during a run and confirm that table has no marker for the date while the others do; then re-run the same date and confirm the marker is removed at the start of the re-write and restored only when the re-write succeeds.

**Acceptance Scenarios**:

1. **Given** writing table `product_attribute` fails for snapshot date 2026-09-27, **When** the run finishes, **Then** `product_attribute/snapshot_date=2026-09-27/` contains no `_SUCCESS` file, while tables that were written successfully do have one.
2. **Given** `price_fact/snapshot_date=2026-09-27/_SUCCESS` already exists from a previous run, **When** the pipeline starts re-writing `price_fact` for 2026-09-27, **Then** the existing marker is removed before any data file in that partition is changed, and a new marker is written only after the re-write completes successfully.
3. **Given** a table produced no rows for the snapshot date **in every configured region** (and so no data files were written for it), **When** the last region run finishes, **Then** no `_SUCCESS` marker is written for that table for that date.

---

### Edge Cases

- **Concurrent per-region runs**: multiple region runs for the same snapshot date finish at different times and may finish simultaneously; the marker must not appear until every configured region has succeeded (FR-004), and simultaneous completions must not produce errors, a duplicate write race, or a corrupt marker.
- **Region list changes mid-date**: completion is judged against the configured region list as it stands when the completion check runs. Adding a region means that region must also succeed before the marker is written. Removing a region means it is no longer required, even if its `region=` sub-directory exists.
- **Overlapping runs of the same region**: two runs for the same region and snapshot date (e.g., a manual run launched while the scheduled one is still going) must not write that region's data at the same time. The second waits until the first has finished its writes and markers (FR-017).
- **Manual single-region re-run**: a manual re-run of one region for a date is subject to the same completion check as a batch run. If it brings the last missing region to success, it writes the marker.
- **Partial region failure**: some regions succeed and others fail for the same snapshot date — no marker is written for affected tables until every configured region succeeds (FR-004).
- **Re-run of an already-completed date**: the existing `_SUCCESS` marker and the re-run region's `_REGION_COMPLETE` file must be removed before data in that partition changes (FR-006, FR-013).
- **Crash mid-write**: a region run that dies partway through leaves data files but no `_REGION_COMPLETE` file, so that region is not counted as done (FR-014).
- **Empty table for a date**: if no region wrote any data for the table, no `_SUCCESS` marker is written (FR-007).
- **Empty table in one region only**: that region's folder holds only `_REGION_COMPLETE`. Readers see zero rows from it, and it counts as done (FR-015).
- **Marker write itself fails** (e.g., disk full, permissions): the failure must be reported as an error for that table and must not be silently ignored.
- **Snapshot date directory does not yet exist**: the marker is only written after data files exist, so the directory will always already exist at marker time.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST write a file named exactly `_SUCCESS` into `.../parquet/<table>/snapshot_date=<date>/` for each table whose data for that snapshot date has been completely and successfully written.
- **FR-002**: The system MUST write the marker only after all data files that make up the partition (per FR-004) are fully written; the marker MUST never be visible while data files for that partition are still being written.
- **FR-003**: The system MUST write markers only into the snapshot-date partition of the date currently being processed. It MUST NOT create, modify, or delete any file in any other snapshot-date partition.
- **FR-004**: A table's snapshot-date partition MUST be considered complete, and its marker written, only when **every region in the current configured region list** (read at the time of the check) has successfully written that table for that snapshot date. The check applies to every run that writes the partition, whether scheduled, sensor-triggered, or a manual single-region re-run. If any configured region fails to write the table (or its run fails), the marker MUST NOT be written for that table and date; it is written once a later re-run brings every configured region to success.
- **FR-005**: Markers MUST be evaluated and written per table, independently: a failure or empty result in one table MUST NOT prevent markers from being written for other tables that completed successfully.
- **FR-006**: When the system begins re-writing a table's partition for a snapshot date that already has a `_SUCCESS` marker, it MUST remove that marker before changing any data in the partition.
- **FR-007**: The system MUST NOT write a marker for a table that had no data written for the snapshot date (e.g., the table was skipped because it had no rows).
- **FR-008**: The marker MUST be an empty file, following the common data-lake `_SUCCESS` convention.
- **FR-009**: The `_SUCCESS` marker MUST be placed at the snapshot-date level only — not at the table root and not inside `region=<region>` sub-directories.
- **FR-012**: After a region run has fully and successfully written a table's data for a snapshot date, the system MUST write an empty `_REGION_COMPLETE` file inside that table's `snapshot_date=<date>/region=<region>/` folder. This is the durable record the completion check (FR-004) uses to decide whether that region is done.
- **FR-013**: When the system begins re-writing a region's data for a table and date, it MUST remove that region's `_REGION_COMPLETE` file (and, per FR-006, the snapshot-date `_SUCCESS` marker) before changing any data in that region folder.
- **FR-014**: The completion check (FR-004) MUST treat a region as done for a table only if that region's `_REGION_COMPLETE` file is present. Data files without a `_REGION_COMPLETE` file MUST NOT count as done.
- **FR-015**: A region that succeeds but produces no rows for a table MUST still be recorded as done for that table: the system MUST create that table's `snapshot_date=<date>/region=<region>/` folder containing only a `_REGION_COMPLETE` file and no data file, so the region does not block the snapshot-date marker.
- **FR-016**: If any input pricing file for a region fails to parse, the system MUST NOT write `_REGION_COMPLETE` for that region in any table for that snapshot date, and MUST report that region's run as failed (after writing whatever data it could), naming the files that failed.
- **FR-017**: At most one run at a time MUST write a given region's data and markers for a given snapshot date. A second run for the same region and date MUST wait until the first has finished, rather than writing alongside it.
- **FR-010**: The system MUST log, for each table, whether a marker was written for the snapshot date, and MUST surface a failure to write a marker as an error for that run.
- **FR-011**: The presence of the `_SUCCESS` file MUST NOT interfere with reading the partition's data (consumers reading the table data must continue to see the same rows as before this feature).

### Key Entities

- **Table**: one of the five pricing tables (`service_dim`, `region_dim`, `product_dim`, `product_attribute`, `price_fact`), each stored as its own directory tree.
- **Snapshot-date partition**: the `snapshot_date=<YYYY-MM-DD>` directory under a table; the unit that receives a `_SUCCESS` marker.
- **Region sub-partition**: the `region=<region>` directory under a snapshot-date partition, written by a single per-region run. It holds a `_REGION_COMPLETE` file when that region's write succeeded, and never holds `_SUCCESS`.
- **Region completion record**: an empty `_REGION_COMPLETE` file inside a region sub-partition. Its presence means "this region's data for this table and date is fully written." It is used by the completion check and is not intended for consumers.
- **Success marker**: an empty `_SUCCESS` file whose presence means "this table's partition for this date is complete and safe to read."

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: After a successful run for a snapshot date, 100% of tables that received data for that date have a `_SUCCESS` marker in their snapshot-date directory.
- **SC-002**: 0 markers are ever observed in a snapshot-date directory unless every configured region's data for that table and date has been written successfully.
- **SC-003**: 0 files in snapshot-date partitions other than the one being processed are created, modified, or deleted by a run.
- **SC-004**: Reading any table's data for a marked partition returns the same row counts as reading it without the marker present.
- **SC-005**: A downstream consumer can determine whether a table's partition for a given date is ready with a single existence check, without inspecting data files.

## Assumptions

- The snapshot date being processed is the one supplied to (or derived by) the run today; "new partition" means that snapshot date's directory, even if it already existed from a previous attempt on the same date.
- The marker file name is exactly `_SUCCESS` (case-sensitive), with no extension and no content.
- Existing partitions from before this feature ships will not be back-filled with markers; back-filling is out of scope.
- Parquet data is stored on the local/mounted filesystem used today; other storage backends are out of scope for this feature.
- Consumers and readers of the tables already ignore files beginning with `_` (the common convention), so the marker does not affect data reads.
- "Configured regions" means the configured region list (the same list the schedule and sensor fan out over) as read at the moment the completion check runs, not a snapshot taken when the batch was launched.
- A region whose run succeeds but legitimately produces no rows for a particular table counts as complete for that table (recorded per FR-015); only a failed region run — including one where an input file failed to parse (FR-016) — withholds the marker. FR-007 still applies: if no region wrote any data for the table, no `_SUCCESS` marker is written, even though every region has a `_REGION_COMPLETE` file.
- The existing directory layout (`<table>/snapshot_date=<date>/region=<region>/`) is unchanged.
