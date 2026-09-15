# Feature Specification: Multi-Region Parallel Pricing Downloads

**Feature Branch**: `001-multi-region-async-pricing`

**Created**: 2026-09-14

**Status**: Draft

**Input**: User description: "(1) We need to pull down data from other aws regions: us-east-2, us-west-1, us-west-2, eu-west-1, eu-west-2, ap-northeast-1. At the moment, us-east-1 is hard coded in the dagster schedule - let's not hard code regions here, but move them into a list of regions that can be parameterized. The existing scheduled job can run downloads for n regions in parallel. Also at the moment, the crontab for pricing_weekly_schedule is every day, this can be changed to once a week. (2) review the code and where appropriate, lets use async methods if it will improve efficiencies"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Download pricing for all configured regions (Priority: P1)

As the operator of the pricing data pipeline, I want the scheduled job to collect AWS pricing data for a configurable set of regions (not just `us-east-1`), so that downstream consumers have pricing coverage across every region the organization cares about.

**Why this priority**: This is the core value of the feature — without it, no additional region coverage exists regardless of how fast or how often the job runs.

**Independent Test**: Configure the region list to include `us-east-1, us-east-2, us-west-1, us-west-2, eu-west-1, eu-west-2, ap-northeast-1`, trigger a run, and verify raw and transformed (parquet) output exists for every listed region.

**Acceptance Scenarios**:

1. **Given** a configured list of 7 regions, **When** the scheduled job runs, **Then** raw pricing files and parquet tables are produced for all 7 regions.
2. **Given** the region list is changed (a region added or removed), **When** the next scheduled run executes, **Then** the run reflects the updated list without any code change to the scheduling or job-definition logic.
3. **Given** a single ad hoc region download (existing CLI usage), **When** it is run, **Then** it continues to behave exactly as before, unaffected by the multi-region list.

---

### User Story 2 - Run region downloads in parallel (Priority: P2)

As the operator of the pricing data pipeline, I want the job to download data for multiple regions concurrently instead of one after another, so that adding more regions does not cause the total run time to grow roughly linearly with the number of regions.

**Why this priority**: Directly follows from User Story 1 — once multiple regions are in scope, sequential processing would make each scheduled run take several times longer and risks not finishing within its window.

**Independent Test**: Run the job against the full region list and compare total wall-clock duration to the duration of a single-region run; confirm the multi-region run completes in a small multiple (not N times) the single-region duration, and confirm one region's failure does not block or abort the others.

**Acceptance Scenarios**:

1. **Given** the full list of configured regions, **When** the scheduled job runs, **Then** downloads for multiple regions are in progress at the same time (not strictly one-at-a-time).
2. **Given** one region's download fails (e.g., a transient network or permissions error), **When** the run continues, **Then** the other regions still complete successfully and their results are stored.
3. **Given** all regions fail, **When** their runs finish, **Then** each region's own run is reported as failed, with failure detail specific to that region.
4. **Given** at least one region succeeds and others fail, **When** their runs finish, **Then** the succeeding regions' runs are reported as successful with their output retained, and the failing regions' runs are reported as failed with per-region failure detail — each region's run status stands on its own, with no single combined status across regions.

---

### User Story 3 - Refresh pricing weekly instead of daily (Priority: P3)

As the operator of the pricing data pipeline, I want the recurring schedule to trigger once a week instead of once a day, so that the pipeline's run frequency matches how often AWS pricing actually changes, avoiding unnecessary runs, API calls, and stored snapshots.

**Why this priority**: An operational cost/efficiency improvement, independent of region scope or parallelism — it can be delivered and verified on its own.

**Independent Test**: Inspect the configured recurring schedule and confirm it fires once per 7-day period at the expected time, and confirm no daily trigger remains.

**Acceptance Scenarios**:

1. **Given** the updated schedule, **When** a full calendar week elapses, **Then** exactly one scheduled run has been triggered (barring manual/backfill runs).
2. **Given** the previous daily cron definition, **When** this feature is complete, **Then** the schedule no longer fires more than once per week.

---

### Edge Cases

- What happens when the configured region list is empty? The scheduled run should fail fast with a clear error rather than silently doing nothing.
- What happens when a region code in the list is invalid or unsupported by the AWS Pricing API? That region should be reported as a failed region (per Edge Case handling in User Story 2), without aborting the other regions.
- What happens when a service has no pricing data in a particular region (already possible today, e.g., a service that isn't offered in that region)? That remains a per-service "no data" outcome within that region's run, not a region-level failure.
- What happens when the number of configured regions exceeds any configured concurrency limit? Remaining regions should queue and start as capacity frees up, rather than being skipped or erroring.
- What happens if a scheduled weekly run is still in progress when the next weekly trigger time arrives? The existing single-run behavior (no overlapping runs of the same job) continues to apply; this feature does not change that guarantee.
- What happens to existing single-region raw/parquet output already stored under `us-east-1`? It remains valid historical data and is unaffected; new runs simply add data for additional regions alongside it.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST define the set of AWS regions to download pricing for as a configurable list rather than a single hardcoded region value.
- **FR-002**: The default configured region list MUST include: `us-east-1`, `us-east-2`, `us-west-1`, `us-west-2`, `eu-west-1`, `eu-west-2`, `ap-northeast-1`.
- **FR-003**: The scheduled pricing job MUST process every region in the configured list on each scheduled run, without requiring a separate schedule or job per region.
- **FR-004**: The scheduled pricing job MUST process multiple regions concurrently rather than strictly one after another.
- **FR-005**: A failure while downloading or transforming data for one region MUST NOT prevent the other regions in the same run from completing.
- **FR-006**: The system MUST report, per region, whether that region's download/transform succeeded, partially succeeded (e.g., some services had no data), or failed — consistent with how per-service outcomes are already reported today.
- **FR-007**: Each region's run MUST report its own success or failure independently of every other region's run for the same scheduled tick; a region's run MUST only be marked failed when that region itself produced no usable output (mirroring today's per-service failure handling), and one region's run failing MUST NOT be reported as, or cause, a failure of any other region's run.
- **FR-008**: The recurring schedule that triggers the pricing pipeline MUST fire once per week instead of once per day.
- **FR-009**: The region list, region-level concurrency limit, and schedule cadence MUST each be defined in a single, clearly identifiable place, so they can be changed without modifying orchestration/pipeline logic.
- **FR-010**: Raw and transformed (parquet) output MUST continue to be identifiable per region in storage, so downstream consumers can distinguish pricing data by region, exactly as single-region output is identifiable today.
- **FR-011**: Existing single-region, ad hoc usage of the pricing download capability (outside the scheduled job) MUST continue to work unchanged.
- **FR-012**: The system's handling of concurrent, I/O-bound work (across regions and, within a region, across services) MUST be reviewed, and changed where it measurably reduces total run time or resource use, without changing any of the observable behaviors described in FR-001 through FR-011.

### Key Entities

- **Region**: An AWS region code (e.g., `us-east-1`) that pricing data is collected for. Attributes: region code, inclusion in the active configured list.
- **Region Run Result**: The outcome of processing one region within a scheduled run. Attributes: region code, status (success / partial success / failed), downloaded file count, failed service list, error detail.
- **Scheduled Run**: One trigger ("tick") of the recurring pricing pipeline, covering all configured regions. Attributes: trigger timestamp, snapshot date, list of Region Run Results — each region's run status is independent; there is no single aggregate status field for the tick as a whole (see FR-007).
- **Schedule Cadence**: The recurring trigger definition for the pipeline. Attributes: frequency (weekly), trigger time.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: After each scheduled run, pricing data (raw and parquet) is available for all 7 currently configured regions.
- **SC-002**: A run covering all 7 configured regions completes in noticeably less time than 7 sequential single-region runs would take — regions are demonstrably processed concurrently, not one after another.
- **SC-003**: When exactly one of the configured regions fails, 100% of the remaining regions' runs still complete and their data is stored, and the failure is visible in that region's own run status without affecting the others.
- **SC-004**: The pricing pipeline triggers on its own schedule once every 7 days (down from once per day), a 7x reduction in scheduled run frequency.
- **SC-005**: Adding or removing a region from scope, or changing the region-level concurrency limit, requires updating only the region configuration — no change to scheduling or job-orchestration code is needed.
- **SC-006**: Overall resource use (e.g., peak concurrent connections/threads) during a full multi-region run stays bounded and configurable, rather than growing without limit as regions are added.

## Assumptions

- `us-east-1` remains part of the default region list (extended, not replaced) — the feature adds the six newly requested regions alongside the existing one.
- Each region's raw and parquet output continues to use the existing per-region file/partition naming already in place today; no change to the storage schema itself is required, only that it now covers more regions per run.
- Region-level concurrency (how many regions run at once) and the existing per-service concurrency within a single region's download (`max_raw_download_workers`) are independent, separately configurable settings.
- A region-level failure is treated the same way per-service failures are treated today: it is logged and reported, but does not stop or affect any other region's run. Because each region is its own independent run rather than part of one combined run, there is no aggregate "did the whole tick succeed" status to compute or report (see FR-007).
- The weekly schedule keeps the same trigger time already used today (13:00 UTC) and simply reduces frequency from daily to weekly, on a fixed day of the week chosen during implementation; the exact day is an operational detail, not a functional requirement.
- "Review the code and use async methods where it improves efficiency" (point 2 of the request) is an efficiency/non-functional goal (FR-012, SC-002, SC-006): the specific concurrency mechanism used to hit it is a technical decision made during implementation planning, not a change to any user-observable behavior described in this spec.
- This feature covers the scheduled Dagster pipeline only; the existing single-region CLI/ad hoc workflow is out of scope for the region-list and parallelism changes (FR-011).
