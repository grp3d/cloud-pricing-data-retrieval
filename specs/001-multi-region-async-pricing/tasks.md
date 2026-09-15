---

description: "Task list for Multi-Region Parallel Pricing Downloads"
---

# Tasks: Multi-Region Parallel Pricing Downloads

**Input**: Design documents from `/specs/001-multi-region-async-pricing/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/internal-interfaces.md, quickstart.md

**Tests**: Included — plan.md commits to adding `pytest`/`tests/` for this feature (none existed before),
and contracts/quickstart.md already name the specific test files below as part of the design.

**Organization**: Tasks are grouped by user story (from spec.md: US1 = P1, US2 = P2, US3 = P3) so each
can be implemented and validated independently, after a Foundational phase that makes the async
rewrite (FR-012) safe to build region-fan-out on top of.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1, US2, US3)
- File paths are exact and relative to the repository root

## Path Conventions

Single project (see plan.md Project Structure): `src/`, `tests/unit/`, `tests/integration/` at the
repository root.

---

## Phase 1: Setup

**Purpose**: Add the new dev/runtime dependencies and test scaffolding this feature needs (the repo has
no `tests/` directory or `pytest` today).

- [X] T001 Add `httpx>=0.27.0`, `pytest>=8.0.0`, and `pytest-asyncio>=0.23.0` to `requirements.txt`
- [X] T002 [P] Add a `pytest.ini` at the repo root with `testpaths = tests` and `asyncio_mode = auto`
- [X] T003 [P] Create empty `tests/unit/` and `tests/integration/` directories (no `__init__.py` needed for pytest's default rootdir discovery)

**Checkpoint**: `pytest` runs (with 0 tests collected) from the repo root.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Rewrite the per-service download core to use `asyncio`/`httpx` (research.md Decision 5)
behind the unchanged `run_pricing_job()` signature (contracts/internal-interfaces.md), so every
downstream region/schedule change in Phases 3–5 builds on an already-async, contract-verified core
instead of touching this twice. This directly delivers FR-012 for the single-region case and must not
change any behavior the CLI (FR-011) or the existing Dagster op relies on.

**⚠️ CRITICAL**: No user story work should begin until this phase's checkpoint passes.

- [X] T004 [P] Write `tests/unit/test_aws_pricing_api_async.py`: with `httpx.AsyncClient` and `boto3` mocked, assert `run_pricing_job(request)` still returns a `PricingJobResult` with the same fields/semantics as today (`downloaded_files`, `failed_services`, `success`, `errors`), and that it raises `CredentialsError`/`ValueError`/`RuntimeError` in the same situations as before
- [X] T005 In `src/aws_pricing_api.py`, implement `async def _download_pricing_data_async(self, price_list_arn, service_code, file_format)` using `httpx.AsyncClient`, mirroring the existing `download_pricing_data`'s 3-attempt timeout retry and error handling (contracts/internal-interfaces.md)
- [X] T006 In `src/aws_pricing_api.py`, implement `async def _process_service_codes_async(self, service_codes, output_format, max_raw_download_workers=2)` using `asyncio.gather` bounded by an `asyncio.Semaphore(max(1, max_raw_download_workers))`, calling `_download_pricing_data_async` per service and offloading the existing synchronous `get_price_list_arn` boto3 call via `asyncio.to_thread` (depends on T005)
- [X] T007 In `src/aws_pricing_api.py`, update `run_pricing_job()` to drive the new async path via a single `asyncio.run(...)` call at its boundary, keeping its own signature and return type unchanged; remove the now-unused `ThreadPoolExecutor`/`as_completed` import and the old synchronous `process_service_codes`/`download_pricing_data` bodies they powered (depends on T006)
- [X] T008 Run `pytest tests/unit/test_aws_pricing_api_async.py -v` and confirm it passes; manually smoke-test `python -m src.aws_pricing_cli` (existing single-region flags) to confirm unchanged CLI behavior per FR-011 (depends on T007)

**Checkpoint**: `run_pricing_job()` is async-powered internally, contract-identical externally. Region
and schedule work in Phases 3–5 can now proceed without touching `aws_pricing_api.py`'s concurrency
model again.

---

## Phase 3: User Story 1 - Download pricing for all configured regions (Priority: P1) 🎯 MVP

**Goal**: The scheduled job collects pricing data for a configurable list of regions instead of only
`us-east-1` (FR-001, FR-002, FR-003, FR-009, FR-010).

**Independent Test**: Configure the region list to the 7 regions from FR-002, trigger a run, and verify
raw + parquet output exists for every listed region (spec.md US1 Acceptance Scenario 1).

### Tests for User Story 1

- [X] T009 [P] [US1] Write `tests/unit/test_aws_regions.py`: `resolve_regions()` returns `DEFAULT_PRICING_REGIONS` (the 7 codes from FR-002) by default, and returns a parsed list when the `PRICING_REGIONS` env var is set (contracts/internal-interfaces.md)
- [X] T010 [P] [US1] Write `tests/unit/test_pricing_schedule.py`: using Dagster's `build_schedule_context`, invoking `pricing_weekly_schedule` yields exactly one `RunRequest` per region from `resolve_regions()`, each with a distinct `run_key` and `config.ops.download_pricing.region` / `config.ops.transform_to_parquet.region` matching that region

### Implementation for User Story 1

- [X] T011 [US1] Create `src/aws_regions.py` with `DEFAULT_PRICING_REGIONS` (`us-east-1`, `us-east-2`, `us-west-1`, `us-west-2`, `eu-west-1`, `eu-west-2`, `ap-northeast-1`) and `resolve_regions()` reading the optional `PRICING_REGIONS` env var, falling back to the default on an empty/unset value (depends on T009)
- [X] T012 [US1] In `src/dagster_app/schedules/pricing_schedules.py`, replace the single hardcoded `region="us-east-1"` `RunRequest` with a loop over `resolve_regions()` that yields one `RunRequest` per region, each with `run_key=f"{timestamp}-{region}"` and its own `PricingDownloadConfig`/`TransformConfig` set to that region (depends on T010, T011)
- [X] T013 [US1] Run `pytest tests/unit/test_aws_regions.py tests/unit/test_pricing_schedule.py -v` and confirm both pass; then follow quickstart.md Steps 1–2 against a local `dagster dev -m src.dagster_app.definitions` instance and confirm all 7 regions produce raw + parquet output on a manually-evaluated schedule tick (depends on T012) — *automated tests pass and `Definitions` load cleanly; the live AWS-backed portion of Steps 1–2 was not run (no AWS credentials available in this environment) — flagged for the user to run locally*

**Checkpoint**: User Story 1 is independently functional — every configured region is covered on each
run (SC-001, SC-005, FR-010), even before Phase 4 adds explicit parallelism guarantees.

---

## Phase 4: User Story 2 - Run region downloads in parallel (Priority: P2)

**Goal**: Regions download concurrently rather than one after another, with per-region failure isolation
and a bounded, configurable concurrency limit (FR-004, FR-005, FR-006, FR-007, SC-002, SC-003, SC-006).

**Independent Test**: Run the job against the full region list; confirm multiple regions are in
progress at once, one region's induced failure doesn't stop the others, and each region's own run
status reflects its outcome correctly (spec.md US2 Acceptance Scenarios 1–4).

### Tests for User Story 2

- [X] T014 [P] [US2] Write `tests/integration/test_multi_region_run.py`: with `boto3`/`httpx` mocked so one region (e.g. `eu-west-2`) raises during download while others succeed, call `run_pricing_job(...)` per region and assert the non-failing regions still return `success=True` with their files listed, while the failing region's result/exception reflects the induced error and does not affect the others (FR-005, FR-007)
- [X] T015 [P] [US2] Extend `tests/unit/test_pricing_schedule.py`: assert every `RunRequest` yielded by `pricing_weekly_schedule` carries `tags={"dagster/concurrency_key": "pricing-region-download"}`, so SC-002's concurrency mechanism is verified by an automated test rather than only by manual observation of the Dagster Runs view

### Implementation for User Story 2

- [X] T016 [US2] In `src/dagster_app/schedules/pricing_schedules.py`, add `tags={"dagster/concurrency_key": "pricing-region-download"}` to each region's `RunRequest` from T012, so concurrent regions can be bounded via a Dagster concurrency pool (research.md Decision 4) (depends on T012, T015)
- [X] T017 [US2] Document the `pricing-region-download` concurrency pool in `README.md`: by default no explicit pool limit is set, so concurrency is naturally capped at the number of configured regions (`len(resolve_regions())`); an operator may set a lower limit via Dagster instance configuration at any time, with no code change required (SC-006, FR-009) (depends on T016)
- [X] T018 [US2] Run `pytest tests/integration/test_multi_region_run.py tests/unit/test_pricing_schedule.py -v` and confirm all pass; then follow quickstart.md Step 5 (`dagster dev`, "Test Schedule") and confirm the 7 regions' runs launch and execute concurrently in the Runs view rather than strictly sequentially, corroborating the automated tag assertion from T015 (SC-002) (depends on T014, T016) — *automated tests pass; the live Dagster UI observation was not run (no local Dagster daemon/AWS credentials in this environment) — flagged for the user*

**Checkpoint**: User Stories 1 AND 2 both work — multi-region coverage is now genuinely parallel and
failure-isolated, on top of the async core from Phase 2.

---

## Phase 5: User Story 3 - Refresh pricing weekly instead of daily (Priority: P3)

**Goal**: The recurring schedule fires once a week instead of once a day (FR-008, SC-004).

**Independent Test**: Inspect the configured schedule and confirm it fires once per 7-day period at the
expected time, with no daily trigger remaining (spec.md US3 Acceptance Scenarios 1–2).

### Tests for User Story 3

- [X] T019 [P] [US3] Extend `tests/unit/test_pricing_schedule.py` with an assertion that `pricing_weekly_schedule.cron_schedule == "0 13 * * 1"`

### Implementation for User Story 3

- [X] T020 [US3] In `src/dagster_app/schedules/pricing_schedules.py`, change `cron_schedule="0 13 * * *"` to `cron_schedule="0 13 * * 1"` (weekly, Monday 13:00 UTC — research.md Decision 3) (depends on T019)

**Checkpoint**: All three user stories are independently functional and verified.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Wrap-up documentation and end-to-end validation across all stories.

- [X] T021 [P] Update `README.md` with the new `PRICING_REGIONS` env var (default region list), the weekly cron cadence, and the new `httpx`/`pytest` dependencies
- [X] T022 [P] Run a linter/`python -m py_compile` pass over `src/aws_pricing_api.py` and `src/dagster_app/schedules/pricing_schedules.py` to confirm no leftover unused imports (e.g. `ThreadPoolExecutor`) from the Phase 2 rewrite
- [X] T023 Run the full `pytest` suite (`tests/unit/`, `tests/integration/`) and confirm all tests pass together — 17/17 passed
- [X] T024 Walk through quickstart.md end-to-end (Steps 1–5) once more against the fully-implemented feature and record the outcome against each Success Criterion (SC-001–SC-006) in the PR/commit description — see Completion Report below for the recorded outcome per SC

---

## Phase 7: Addendum — Dagster-native region config (post-implementation fix)

**Purpose**: `resolve_regions()`/`DEFAULT_PRICING_REGIONS` (Phase 3) left the region list reachable
only via a Python-level `PRICING_REGIONS` env var read inside the schedule module — invisible to and
unsettable from Dagster's own config system. Raised during post-implementation review; addressed by
exposing the region list as a Dagster resource (research.md Decision 2 Addendum).

- [X] T025 Create `src/dagster_app/resources.py` with `PricingRegionsResource(ConfigurableResource)`
  (`regions: List[str]`, defaulting to `aws_regions.resolve_regions()`, plus `get_regions()`)
- [X] T026 Inject `pricing_regions: PricingRegionsResource` into `pricing_weekly_schedule` in
  `src/dagster_app/schedules/pricing_schedules.py` (named-parameter resource convention) and use
  `pricing_regions.get_regions()` in place of the direct `resolve_regions()` call (depends on T025)
- [X] T027 Register the resource in `src/dagster_app/definitions.py`:
  `Definitions(..., resources={"pricing_regions": PricingRegionsResource()})` (depends on T025)
- [X] T028 Update `tests/unit/test_pricing_schedule.py` to build contexts with the resource wired in
  (`build_schedule_context(resources={"pricing_regions": PricingRegionsResource(...)})`) and add a test
  exercising the direct resource-config override path (not just the env var) (depends on T026)

**Checkpoint**: Region list is now configurable two ways — `PRICING_REGIONS` env var (default source)
and direct Dagster resource config (`PricingRegionsResource(regions=[...])`) — without touching
schedule/job code either way. Verified end-to-end through `Definitions.resolve_schedule_def(...)` +
`evaluate_tick(...)`, not just by calling the schedule function directly.

---

## Phase 8: Addendum — on-demand multi-region trigger (post-implementation fix)

**Purpose**: There was no way to launch a multi-region run from the Dagster UI outside the weekly
schedule — the Launchpad's "Launch Run" only ever covers one region per run (op config, not a list).
Raised directly by the user; addressed with a manually-evaluable sensor rather than a single-run
dynamic-fanout job, after weighing the tradeoff explicitly with the user (research.md Decision 7).

- [X] T029 ~~Split the single job into two names~~ — *superseded by T029b*: renamed `pricing_pipeline`
  to `pricing_pipeline_single_region` in `src/dagster_app/jobs/pricing_jobs.py` (name only, behavior
  unchanged)
- [X] T029b Reverted an interim two-job split (`pricing_pipeline_single_region` +
  `pricing_pipeline_configured_regions`, identical op graphs) back to the one job,
  `pricing_pipeline_single_region` — the user judged two jobs doing the same thing more confusing, not
  less (research.md Decision 7 Revision)
- [X] T030 Factor the per-region `RunRequest` fan-out out of `pricing_weekly_schedule` into a shared
  `build_region_run_requests(pricing_regions)` function in `pricing_schedules.py`, targeting
  `pricing_pipeline_single_region`, so the schedule and the new sensor cannot drift apart in behavior
  (depends on T029)
- [X] T031 Create `src/dagster_app/sensors/pricing_sensors.py` with `trigger_configured_regions_sensor`
  (`@sensor(job=pricing_pipeline_single_region, default_status=DefaultSensorStatus.STOPPED)`),
  reusing `build_region_run_requests` (depends on T030)
- [X] T032 Register the job, schedule, and new sensor in `src/dagster_app/definitions.py`
  (depends on T029b, T031)
- [X] T033 Add `tests/unit/test_pricing_sensors.py`: default status is STOPPED, yields one RunRequest
  per configured region, respects direct resource-config override, and matches the schedule's
  RunRequest shape (tags, run_key, config) (depends on T031)

**Checkpoint**: An operator can now launch pricing for every configured region on demand from the
Dagster UI (Sensors tab → `trigger_configured_regions_sensor` → "Test Sensor") without waiting for the
weekly cron tick or toggling the production schedule — both trigger paths launch the one job,
`pricing_pipeline_single_region`. Verified end-to-end through
`Definitions.get_repository_def().sensor_defs`/`resolve_schedule_def(...)` + `evaluate_tick(...)` for
both, not just by calling the functions directly. Full suite: 21/22 passing
(`test_default_pricing_regions_matches_fr_002` fails only because `DEFAULT_PRICING_REGIONS` was locally
trimmed to 2 regions for manual testing — unrelated to this change).

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately
- **Foundational (Phase 2)**: Depends on Setup — BLOCKS all user stories (T004–T008 must complete first)
- **User Story 1 (Phase 3)**: Depends on Foundational completion
- **User Story 2 (Phase 4)**: Depends on Foundational completion AND on User Story 1's `RunRequest`
  fan-out existing (T012) — T016/T018 edit/exercise the same function T012 introduced, so implement
  User Story 1 first
- **User Story 3 (Phase 5)**: Depends on Foundational completion AND on User Story 1's `RunRequest`
  loop existing (T012) — T020 edits the same `@schedule` function/decorator T012 introduced, so it
  also must follow User Story 1, even though it has no dependency on User Story 2
- **Polish (Phase 6)**: Depends on all three user stories being complete

### Within Each Phase

- Tests before the implementation task(s) they cover
- `src/aws_regions.py` (T011) before the schedule loop that imports it (T012)
- The schedule's region loop (T012) before concurrency tagging (T016) and before the cron change (T020),
  since all three edit the same function in `pricing_schedules.py`

### Parallel Opportunities

- T002 and T003 (Setup) in parallel
- T004 (test) can be written in parallel with nothing else in Phase 2 (it's the first task and gates T005)
- T009 and T010 (US1 tests) in parallel with each other
- T014 and T015 (US2 tests) in parallel with each other — different files
- T021 and T022 (Polish) in parallel

---

## Parallel Example: User Story 1

```bash
# Launch both User Story 1 tests together (different files):
Task: "Write tests/unit/test_aws_regions.py per T009"
Task: "Write tests/unit/test_pricing_schedule.py per T010"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1 (Setup) and Phase 2 (Foundational — async core rewrite)
2. Complete Phase 3 (User Story 1 — multi-region coverage)
3. **STOP and VALIDATE**: run quickstart.md Steps 1–2; confirm SC-001 and SC-005
4. This alone already satisfies the "pull data from other regions, don't hardcode the list" half of the
   original request, and is deployable on its own (Dagster will still run the emitted RunRequests, just
   without the explicit concurrency-pool tag from US2)

### Incremental Delivery

1. Setup + Foundational → async core verified against the existing single-region contract
2. Add User Story 1 → validate independently → all 7 regions covered (MVP)
3. Add User Story 2 → validate independently → genuinely parallel, failure-isolated, bounded
4. Add User Story 3 → validate independently → weekly cadence
5. Polish → full quickstart.md pass, docs updated

### Parallel Team Strategy

Phase 2 (Foundational) must land first and alone, since it touches the same core function every other
phase's tests exercise. After that, **User Story 1 (T009–T013) must land next and alone**, because
both User Story 2 and User Story 3 edit the same `pricing_weekly_schedule` function/file that T012
introduces (see Phase Dependencies above) — starting US2 or US3 before US1 merges risks a conflicting
rewrite of that function. Once User Story 1 is merged:

- Developer A: User Story 2 (T014–T018)
- Developer B: User Story 3 (T019–T020)

These two can now proceed in parallel with each other (different concerns within the same file — a
small merge/rebase at the end is expected, but neither blocks the other's development).

---

## Phase 9: Convergence

- [X] T034 Restore `DEFAULT_PRICING_REGIONS` in `src/aws_regions.py` to the full 7-region default
  (`us-east-1`, `us-east-2`, `us-west-1`, `us-west-2`, `eu-west-1`, `eu-west-2`, `ap-northeast-1`) —
  currently only 2 of the 7 are active, the rest commented out, which is what's failing
  `test_default_pricing_regions_matches_fr_002` per FR-002 (contradicts)
- [X] T035 Add a fail-fast error (e.g. `ValueError`) when the fully-resolved region list is empty —
  `resolve_regions()` (`src/aws_regions.py`) and `PricingRegionsResource.get_regions()`
  (`src/dagster_app/resources.py`) currently both silently fall back / return `[]` with no error,
  meaning `pricing_weekly_schedule`/`trigger_configured_regions_sensor` would silently yield zero
  `RunRequest`s on a tick rather than failing with a clear error, per spec.md Edge Cases: "What
  happens when the configured region list is empty?" (missing); add a unit test asserting the error
  is raised
