# Phase 1 Contracts: Multi-Region Parallel Pricing Downloads

This project has no external/public API — its interfaces are the Python module boundaries consumed by
`aws_pricing_cli.py` (CLI) and `dagster_app/*` (scheduled pipeline). This document freezes the
contract of the modules this feature adds or changes, so CLI and Dagster call sites, and tests, can be
written against a stable shape.

## `src/aws_regions.py` (new)

```python
DEFAULT_PRICING_REGIONS: List[str]
# = ["us-east-1", "us-east-2", "us-west-1", "us-west-2",
#    "eu-west-1", "eu-west-2", "ap-northeast-1"]   # FR-002

def resolve_regions() -> List[str]:
    """Return the effective region list: DEFAULT_PRICING_REGIONS, unless the
    PRICING_REGIONS env var is set to a comma-separated list of region codes,
    in which case that list is returned instead. Never returns an empty list —
    an explicitly empty PRICING_REGIONS ("" or all-whitespace) falls back to
    DEFAULT_PRICING_REGIONS rather than yielding zero regions (see Edge Cases:
    empty region list). Order is preserved from the source list."""
```

**Consumers**: `src/dagster_app/resources.py::PricingRegionsResource` (its default value), for this
feature. (`aws_pricing_cli.py` is explicitly out of scope per FR-011 and does not call this — it keeps
taking `--region` directly.)

## `src/dagster_app/resources.py` (new, post-implementation addendum)

```python
class PricingRegionsResource(ConfigurableResource):
    """Dagster-native config surface for the region list (FR-001, FR-009) — makes it settable
    via Definitions(resources=...) / per-deployment resource config, not only PRICING_REGIONS."""

    regions: List[str]  # defaults to aws_regions.resolve_regions() at construction time

    def get_regions(self) -> List[str]: ...
```

**Consumers**: `dagster_app/schedules/pricing_schedules.py::pricing_weekly_schedule` (injected as a
named parameter, per Dagster's resource-as-parameter convention for `@schedule`) and
`dagster_app/definitions.py` (registers the default instance under the `"pricing_regions"` key).

## `src/aws_pricing_api.py` — `run_pricing_job` (signature unchanged)

```python
def run_pricing_job(request: PricingJobRequest) -> PricingJobResult:
    """Unchanged public signature and unchanged synchronous call contract.
    Internally now runs its per-service download phase on an asyncio event
    loop (asyncio.run(...) at this function's boundary — see research.md
    Decision 5) but callers (CLI, Dagster op) observe no difference: same
    inputs, same PricingJobResult shape, same exceptions
    (CredentialsError, ValueError, RuntimeError)."""
```

**Consumers**: `aws_pricing_cli.py` (unchanged call site, FR-011) and
`dagster_app/assets/pricing_assets.py::download_pricing` (unchanged call site).

### New internal (non-public) async functions backing it

```python
async def _download_pricing_data_async(
    self, price_list_arn: str, service_code: str, file_format: str
) -> Optional[str]: ...

async def _process_service_codes_async(
    self, service_codes: List[str], output_format: str,
    max_raw_download_workers: int = 2,
) -> tuple[List[str], List[str]]: ...
```

These replace the `ThreadPoolExecutor`-based bodies of `download_pricing_data`/
`process_service_codes` (research.md Decision 5). `max_raw_download_workers` is kept as the
concurrency-limit parameter name and semantics (now bounding concurrent `asyncio` tasks via a
`asyncio.Semaphore` instead of thread-pool workers), so `PricingDownloadConfig.max_raw_download_workers`
in `pricing_assets.py` and the CLI's equivalent flag need no change.

## `src/dagster_app/jobs/pricing_jobs.py` (renamed, post-implementation addendum)

```python
@job
def pricing_pipeline_single_region(): ...  # was: pricing_pipeline. Still the only job — targeted
                                            # both for manual single-region Launchpad launches and
                                            # by pricing_weekly_schedule / trigger_configured_regions_sensor
```

Every run of this job still processes exactly one region, whatever region its `PricingDownloadConfig`
carries. Multi-region coverage comes from N runs of it (one per configured region), launched by the
schedule or the sensor below — not from a distinct job (research.md Decision 7 Revision: an earlier
version split this into two identically-behaving jobs; reverted as needless duplication).

## `src/dagster_app/schedules/pricing_schedules.py`

```python
def build_region_run_requests(
    pricing_regions: PricingRegionsResource,
) -> Iterator[RunRequest]:
    """One RunRequest per region in pricing_regions.get_regions() (FR-001, FR-003, FR-004, FR-009),
    each with run_key=f"{timestamp}-{region}" and tags={"dagster/concurrency_key":
    "pricing-region-download"} (research.md Decision 4), targeting
    pricing_pipeline_single_region. Shared by pricing_weekly_schedule and
    trigger_configured_regions_sensor (research.md Decision 7) — the single source of truth for
    the region fan-out, so the two trigger paths cannot drift apart."""


@schedule(job=pricing_pipeline_single_region, cron_schedule="0 13 * * 1")  # weekly, Monday 13:00 UTC
def pricing_weekly_schedule(
    _context: ScheduleEvaluationContext, pricing_regions: PricingRegionsResource
) -> Iterator[RunRequest]:
    """yield from build_region_run_requests(pricing_regions)"""
```

**Consumers**: `dagster_app/definitions.py` (registration) and the Dagster daemon/scheduler, which now
dispatches N concurrent runs per tick instead of 1.

## `src/dagster_app/sensors/pricing_sensors.py` (new, post-implementation addendum)

```python
@sensor(
    job=pricing_pipeline_single_region,
    default_status=DefaultSensorStatus.STOPPED,
    minimum_interval_seconds=3600,
)
def trigger_configured_regions_sensor(
    context: SensorEvaluationContext, pricing_regions: PricingRegionsResource
) -> Iterator[RunRequest]:
    """On-demand companion to pricing_weekly_schedule: yield from build_region_run_requests(...).
    STOPPED by default — never fires on its own; evaluate manually via the Dagster UI's
    'Test Sensor' to launch all configured regions immediately, independent of the weekly cron."""
```

**Consumers**: `dagster_app/definitions.py` (registration under `sensors=[...]`).
