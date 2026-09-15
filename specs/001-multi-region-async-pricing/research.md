# Phase 0 Research: Multi-Region Parallel Pricing Downloads

All items from the Technical Context were resolvable from the existing codebase and Dagster's
documented capabilities — no unresolved `NEEDS CLARIFICATION` markers remain.

## Decision 1: How regions run in parallel

**Decision**: Change `pricing_weekly_schedule` to loop over the configured region list and `yield`
one `RunRequest` per region (each with its own `run_key` like `f"{timestamp}-{region}"` and its
existing per-region `PricingDownloadConfig`/`TransformConfig`), instead of building a single
`RunRequest` for `"us-east-1"`. The job (`pricing_pipeline_single_region`) and its two ops
(`download_pricing`, `transform_to_parquet`) stay exactly as they are today — still a single-region
job, now also targeted by an on-demand sensor alongside the schedule (see Decision 7).

**Rationale**: Dagster already executes multiple runs from one schedule tick concurrently (subject to
the instance's run-coordinator concurrency limits), so this gives real parallelism across regions with
almost no new code, and — critically — it gives per-region isolation and status for free: each region
is its own Dagster run with its own success/failure/logs, which directly satisfies FR-005 (one
region's failure can't affect another — they're separate runs), FR-006/FR-007 (per-region status is
just each run's status; "at least one region succeeded" is answerable by querying run statuses for
the same scheduled tick), and FR-009 (the region list lives in exactly one place: the schedule
function via `resolve_regions()`).

**Alternatives considered**:
- *Single run, Dagster Dynamic Outputs fanning out per-region ops*: rejected — needs a multi-process/
  threaded executor configuration and custom fan-out/fan-in op wiring for no added benefit, since
  regions are fully independent (there's no cross-region aggregation step that needs them in one run).
- *Single run, `asyncio.gather` across regions inside one op*: rejected — this reinvents, inside a
  single op, exactly the concurrent-run scheduling Dagster already provides, and it collapses
  per-region run-level status/retry/observability into one run's status, which regresses FR-006/FR-007
  relative to the separate-RunRequest approach.

## Decision 2: Where the region list lives

**Decision**: New `src/aws_regions.py` module exporting `DEFAULT_PRICING_REGIONS: List[str]` (the 7
regions from FR-002) and `resolve_regions() -> List[str]`, which returns `DEFAULT_PRICING_REGIONS`
unless the `PRICING_REGIONS` environment variable is set, in which case it parses that as a
comma-separated list. `pricing_schedules.py` calls `resolve_regions()` instead of hardcoding a region.

**Rationale**: Matches the override pattern the codebase already uses for
`DATA_DIRECTORY_ROOT`/`resolve_output_dir()` in `aws_pricing_api.py`, so it's consistent with existing
conventions rather than introducing a new configuration mechanism (e.g. a YAML file or a Dagster
resource) for a single list of strings. Satisfies FR-001, FR-002, FR-009.

**Addendum (post-implementation)**: `resolve_regions()`/`DEFAULT_PRICING_REGIONS` alone left the region
list reachable only via a Python-level env var read inside the schedule module — not visible or
settable through Dagster's own config system (`Definitions`, per-deployment resource config). Added
`PricingRegionsResource` (`src/dagster_app/resources.py`), a `ConfigurableResource` wrapping
`resolve_regions()` as its default, injected into `pricing_weekly_schedule` as a named parameter and
registered in `Definitions(resources={"pricing_regions": PricingRegionsResource()})`. This keeps
`PRICING_REGIONS` working as the default source while also making the region list overridable directly
through Dagster's resource config — the schedule-level analog of "config," since a schedule's own
`RunRequest`s (not the schedule itself) are what carry op config, and each `RunRequest` is intentionally
single-region (Decision 1) so a `regions: List[str]` field cannot live on `PricingDownloadConfig`
without abandoning per-region run isolation.

## Decision 3: Weekly cron expression

**Decision**: Change `cron_schedule="0 13 * * *"` to `"0 13 * * 1"` (Monday 13:00 UTC).

**Rationale**: Keeps the existing 13:00 UTC trigger time (no reason to change it) and reduces
frequency from daily to weekly by restricting to one weekday, per FR-008. Monday is a conventional
start-of-week refresh slot; per the spec's Assumptions, the exact weekday is an operational detail,
not a functional requirement, and remains a one-line change if a different day is preferred later.

**Alternatives considered**: None materially different — any single fixed weekday satisfies FR-008
equally; Monday was chosen only as a readable default.

## Decision 4: Bounding concurrent regions (SC-006)

**Decision**: Tag each region's `RunRequest` with a Dagster run concurrency-pool key (e.g.
`tags={"dagster/concurrency_key": "pricing-region-download"}`). By default, no explicit pool limit is
configured, so concurrency is naturally capped at the number of configured regions (`len(resolve_regions())`)
— it can never exceed that regardless of the tag. An operator can additionally set a lower Dagster
instance-level pool limit at any time, without a code change, if tighter bounding is ever needed.

**Rationale**: Dagster's built-in run-concurrency-pools feature is the natural fit for "bounded and
configurable" (SC-006) applied to *separate runs* (the mechanism chosen in Decision 1) — it needs no
custom code, and changing the limit is a config change, not a code change, which also reinforces FR-009
(concurrency limit "defined in a single, clearly identifiable place").

**Alternatives considered**: A custom semaphore/queue inside the schedule function — rejected as
unnecessary custom concurrency-control code duplicating a feature the orchestrator already provides.

## Decision 5: Async I/O for per-service downloads within a region

**Decision**: In `aws_pricing_api.py`, replace `PricingDataManager.process_service_codes`'s
`ThreadPoolExecutor` + `PricingDataManager.download_pricing_data`'s synchronous `requests.get` with
`asyncio` + `httpx.AsyncClient` for the actual price-list file downloads (the highest-fan-out,
genuinely I/O-bound step — up to ~100+ services per region). The boto3-based calls
(`describe_services`, `get_price_list_arn`/`list_price_lists`, `get_price_list_file_url`) stay
synchronous boto3 calls, invoked via `asyncio.to_thread(...)` from the async code path.

**Rationale**: Directly answers point (2) of the request ("use async methods where it will improve
efficiency") on the part of the pipeline where it actually matters — dozens of concurrent HTTP
downloads have materially lower overhead as `asyncio` tasks than as a fixed-size thread pool. boto3 has
no first-party asyncio support, so wrapping its calls in `asyncio.to_thread` (rather than adding a
second AWS SDK surface) is the standard, low-risk way to keep the exact same AWS calls/credentials
behavior while still participating in the same event loop as the async downloads. This satisfies
FR-012/SC-002/SC-006 without changing FR-001–FR-011 observable behavior: `run_pricing_job()` keeps its
existing synchronous signature (it internally does `asyncio.run(...)` once), so both the Dagster op
(`download_pricing`) and the existing CLI (`aws_pricing_cli.py`) call it exactly as before (FR-011).

**Alternatives considered**:
- *`aioboto3`*: rejected — a second AWS SDK dependency/maintenance surface for calls
  (`list_price_lists`, `get_price_list_file_url`) that are lightweight metadata lookups, not the
  actual bottleneck (the file transfers are); `requests`→`httpx` already covers the real bottleneck.
- *Keep `ThreadPoolExecutor`, just raise `max_raw_download_workers`*: rejected — doesn't reduce
  per-connection overhead, just shifts the tuning knob; doesn't address the request to adopt async
  where appropriate.
- *Rewrite everything (including boto3 calls) as fully async*: rejected — no async AWS SDK is used
  elsewhere in this codebase, and forcing it here without `aioboto3` would mean reimplementing
  request signing, which is out of scope and riskier than `asyncio.to_thread`.

## Decision 6: Consolidation/truncation (pandas) stay synchronous

**Decision**: `consolidate_files`/`truncate_data` (both pandas/CPU-bound, file-local) are left
synchronous, called after the async download phase completes (`asyncio.run` boundary), not converted
to async.

**Rationale**: These are CPU-bound, not I/O-bound — `asyncio` only helps I/O-bound waiting. Converting
them would add complexity (needing `asyncio.to_thread` again) with no measurable benefit, and they run
after all downloads for a region finish, so they're not on the parallel-region critical path.

## Decision 7: On-demand multi-region trigger (post-implementation addendum)

**Decision**: Added `trigger_configured_regions_sensor` — a `@sensor`,
`default_status=DefaultSensorStatus.STOPPED`, that yields the exact same per-region `RunRequest`s as
`pricing_weekly_schedule` via a shared `build_region_run_requests()` helper — alongside the existing
single job (`pricing_pipeline_single_region`, still just the one job; see Revision below) and the
existing schedule. Evaluating the sensor via the Dagster UI's "Test Sensor" launches all configured
regions immediately, without waiting for the Monday cron tick or touching the production schedule.

**Rationale**: Raised directly by the user: there was no way, from the Dagster UI, to trigger a
multi-region run on demand — only the weekly schedule could do it, and manually launching the job from
the Launchpad only ever covers one region per run (op config, not a per-run list). A `@sensor` evaluated
via "Test Sensor" is Dagster's idiomatic on-demand-trigger mechanism (works regardless of running
status, unlike a schedule which is inherently cron-tied). Factoring the fan-out into
`build_region_run_requests()` (shared by both the schedule and the sensor) means the two trigger paths
cannot drift apart in behavior — a single tested implementation, two ways to invoke it.

**Revision**: Initially also split the job into two names (`pricing_pipeline_single_region` +
`pricing_pipeline_configured_regions`) over the identical op graph, purely to make the Dagster UI Jobs
list self-documenting about which name the schedule/sensor targeted. The user pushed back — two jobs
doing the exact same thing reads as more confusing, not less. Reverted to one job
(`pricing_pipeline_single_region`), targeted directly by both the schedule and the sensor; the
distinction between "a manual single-region run" and "one of N region runs launched by the
schedule/sensor" is carried by *how* the job gets launched, not by its name.

**Alternatives considered**:
- *A single job with `DynamicOut` fan-out, processing all regions within one run*: rejected —
  the user explicitly chose the separate-runs approach after weighing this tradeoff: it would require
  new ops taking `region` as an input rather than `Config` (since `PricingDownloadConfig`/
  `TransformConfig` still need to work for `pricing_pipeline_single_region`'s existing single-region
  use), a new config class, and — without also configuring a multiprocess executor — would run regions
  *sequentially* within that one run by default, working against the very efficiency goal (FR-012)
  this feature exists for.
- *A second schedule with a rarely-firing cron instead of a sensor*: rejected — schedules are
  fundamentally cron-triggered; a `STOPPED` sensor is Dagster's purpose-built construct for
  "on-demand only, evaluable at will," and doesn't imply (or require reasoning about) a fake cadence.
