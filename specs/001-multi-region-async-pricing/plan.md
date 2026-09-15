# Implementation Plan: Multi-Region Parallel Pricing Downloads

**Branch**: `001-multi-region-async-pricing` | **Date**: 2026-09-14 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-multi-region-async-pricing/spec.md`

**Note**: This template is filled in by the `/speckit-plan` command; its definition describes the execution workflow.

## Summary

Replace the hardcoded `region="us-east-1"` in `pricing_weekly_schedule` with a configurable region
list (default: `us-east-1, us-east-2, us-west-1, us-west-2, eu-west-1, eu-west-2, ap-northeast-1`),
emit one Dagster `RunRequest` per region so regions download and transform in parallel with isolated
per-region failure/status (leveraging Dagster's native multi-run concurrency instead of hand-rolled
fan-out), change the cron cadence from daily to weekly, and replace the `ThreadPoolExecutor` +
synchronous `requests.get` pattern used for per-service file downloads with `asyncio` + an async HTTP
client to reduce overhead on the highest-fan-out, most I/O-bound part of the pipeline — without
changing the existing single-region CLI/API surface used outside the schedule.

## Technical Context

**Language/Version**: Python 3.11+ (project venv currently Python 3.14.3)

**Primary Dependencies**: `boto3` (Pricing API + STS/EC2), `requests` → adding `httpx` (async HTTP
client for pricing file downloads), `pandas` + `pyarrow` (JSON→parquet transform), `dagster` /
`dagster-webserver` (orchestration, scheduling, run concurrency), `click` (CLI, unchanged)

**Storage**: Local filesystem under `DATA_DIRECTORY_ROOT` (or `.` if unset) — raw JSON/CSV per
`pricing_aws/raw/<timestamp>/pricing-<service>-<region>.<ext>`, transformed Parquet under
`pricing_aws/parquet/`. No database. Structure is unchanged by this feature; it simply gains more
region-labeled files per run.

**Testing**: None currently established in the repo (no `pytest` in `requirements.txt`, no `tests/`
directory). This plan adds `pytest` as a dev dependency and a `tests/` tree so the new region-list,
schedule-cadence, and async-download logic is verifiable (see Project Structure and quickstart.md).

**Target Platform**: Server/workstation running the Dagster daemon + webserver (Linux/macOS); AWS
Pricing API is a public HTTPS service, not tied to a specific compute platform.

**Project Type**: Single project — Python library + CLI + Dagster orchestration app (existing `src/`
layout; no frontend/mobile component).

**Performance Goals**: A full 7-region run completes in a small multiple — not ~7x — of a single-region
run's duration (SC-002), because regions run as concurrent Dagster runs instead of sequential ones.

**Constraints**: Must not change observable behavior of the existing single-region CLI/API path
(FR-011); the `pricing` boto3 client itself must keep being created against `us-east-1` regardless of
which region's price lists are being queried — that is an existing AWS Pricing API control-plane
constraint (`_initialize_client` in `aws_pricing_api.py`), not something this feature changes; peak
concurrent regions/connections must stay bounded and configurable, not unbounded (SC-006).

**Scale/Scope**: 7 configured regions × ~100+ AWS service codes per region per run, once per week.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

`.specify/memory/constitution.md` is still the unfilled template (placeholder principles only, never
ratified for this project) — there are no project-specific principles or gates to check against.
Proceeding under general best practice instead: prefer the smallest change that satisfies the spec,
avoid introducing new services/projects (this stays a single project), avoid new infrastructure
(concurrency is achieved via Dagster's existing run coordinator, not a new queue/service), and add a
new runtime dependency (`httpx`) only where it replaces an existing one (`requests`) for a concrete,
documented reason (see research.md Decision 5). No violations to record in Complexity Tracking.

## Project Structure

### Documentation (this feature)

```text
specs/[###-feature]/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output (/speckit-plan command)
├── data-model.md        # Phase 1 output (/speckit-plan command)
├── quickstart.md        # Phase 1 output (/speckit-plan command)
├── contracts/           # Phase 1 output (/speckit-plan command)
└── tasks.md             # Phase 2 output (/speckit-tasks command - NOT created by /speckit-plan)
```

### Source Code (repository root)

```text
src/
├── aws_pricing_api.py                    # per-region download logic; process_service_codes
│                                          #   becomes async (httpx + asyncio.to_thread for boto3)
├── aws_pricing_cli.py                    # existing single-region CLI — unchanged behavior (FR-011),
│                                          #   bridges into async core via asyncio.run()
├── pricing_parquet_transformations.py    # JSON → parquet transform — unchanged
├── aws_regions.py                        # NEW: DEFAULT_PRICING_REGIONS list + resolve_regions()
└── dagster_app/
    ├── definitions.py                    # registers jobs/schedules — unchanged registration
    ├── jobs/pricing_jobs.py              # unchanged: still one job per single-region run
    ├── assets/pricing_assets.py          # ops unchanged in shape; download_pricing awaits the
    │                                      #   now-async core function
    └── schedules/pricing_schedules.py    # CHANGED: loops resolve_regions(), yields one
                                           #   RunRequest per region (run_key + concurrency tag),
                                           #   weekly cron_schedule

tests/                                    # NEW — no tests/ exists yet in this repo
├── unit/
│   ├── test_aws_regions.py               # region list defaults + env override
│   ├── test_pricing_schedule.py          # N RunRequests emitted, one per region, weekly cron
│   └── test_aws_pricing_api_async.py     # async download/consolidation logic, mocked httpx/boto3
└── integration/
    └── test_multi_region_run.py          # multi-region run with one region mocked to fail —
                                           #   asserts the other regions still complete (FR-005)
```

**Structure Decision**: Single project, extending the existing `src/` layout in place (no new
top-level project/package). Only `src/aws_regions.py` is a new module; every other change is inside
existing files. `tests/` is new for this repo — introduced here because this feature adds
region-fan-out and cadence logic that has no reasonable manual-only verification path (see
quickstart.md) and because the async rewrite of `process_service_codes` changes control flow enough
to warrant a regression safety net.

## Post-Design Constitution Check

*Re-checked after Phase 1 design (data-model.md, contracts/, quickstart.md).*

Still no ratified constitution to check against (see Constitution Check above). The Phase 1 design
stayed within the boundaries set out there: one new module (`src/aws_regions.py`), no new services,
one dependency swap (`requests` → `httpx`) justified in research.md Decision 5, and concurrency handled
via Dagster's existing run-coordinator/concurrency-pools rather than new custom infrastructure
(research.md Decisions 1 and 4). No violations to record.

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

No violations — this section is not applicable (see Constitution Check / Post-Design Constitution
Check above).
