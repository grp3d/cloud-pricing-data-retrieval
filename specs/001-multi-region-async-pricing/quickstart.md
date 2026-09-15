# Quickstart: Validate Multi-Region Parallel Pricing Downloads

Validates the feature end-to-end once implemented, against the contracts in `contracts/` and the
requirements in `spec.md`. Assumes AWS credentials configured per `CREDENTIAL_HELP` in
`src/aws_pricing_api.py` for the live-run steps; the automated tests use mocked `boto3`/`httpx`.

## Prerequisites

```bash
cd cloud-pricing-data-retrieval
python -m venv venv && source venv/bin/activate   # if not already set up
pip install -r requirements.txt
pip install pytest pytest-asyncio httpx            # new dev/runtime deps for this feature
```

## 1. Region list resolves correctly (FR-001, FR-002, FR-009)

```bash
python -c "from src.aws_regions import resolve_regions; print(resolve_regions())"
# Expect: ['us-east-1', 'us-east-2', 'us-west-1', 'us-west-2', 'eu-west-1', 'eu-west-2', 'ap-northeast-1']

PRICING_REGIONS="us-east-1,eu-west-1" python -c "from src.aws_regions import resolve_regions; print(resolve_regions())"
# Expect: ['us-east-1', 'eu-west-1']
```

## 2. Schedule fans out one RunRequest per region, weekly (FR-003, FR-004, FR-008)

```bash
pytest tests/unit/test_pricing_schedule.py -v
```

Expected: a test that builds a `ScheduleEvaluationContext` (via Dagster's `build_schedule_context`),
invokes `pricing_weekly_schedule`, and asserts:
- exactly one `RunRequest` per region returned by `resolve_regions()`, each with a distinct `run_key`
  and `config.ops.download_pricing.region` / `.transform_to_parquet.region` matching that region
- `pricing_weekly_schedule.cron_schedule == "0 13 * * 1"`

## 3. Per-region failure isolation (FR-005, FR-006, FR-007)

```bash
pytest tests/integration/test_multi_region_run.py -v
```

Expected: with `boto3`/`httpx` mocked so one region (e.g. `eu-west-2`) raises during download and the
others succeed, the test asserts the other regions' `run_pricing_job(...)` results still have
`success=True` with their files present, and only the mocked-failing region reports an error —
demonstrating one region's failure doesn't block the others (this exercises the same code path each
region's Dagster run takes, without needing a live Dagster instance).

## 4. Async download behavior + CLI/API compatibility (FR-011, FR-012)

```bash
pytest tests/unit/test_aws_pricing_api_async.py -v
```

Expected: with `httpx.AsyncClient` and `boto3` mocked, calling `run_pricing_job(request)` still returns
a `PricingJobResult` with the same shape as before this feature (downloaded_files, failed_services,
success, errors), confirming the async rewrite of the per-service download phase is behavior-preserving
at the `run_pricing_job` boundary.

```bash
python -m src.aws_pricing_cli --region us-east-1 --all-services   # (adjust to actual CLI flags)
```

Expected: unchanged CLI behavior — this manually confirms FR-011 (existing ad hoc single-region usage
still works, unaffected by the schedule/region-list changes).

## 5. End-to-end local Dagster check (manual, optional)

```bash
dagster dev -m src.dagster_app.definitions
```

In the Dagster UI, open `pricing_weekly_schedule`, use "Test Schedule" / evaluate a tick, and confirm
7 separate runs are launched (one per default region) rather than 1, and that they execute
concurrently (visible in the Runs view) rather than strictly one after another.

## Success criteria checked by this quickstart

| Spec ID | Verified by |
|---|---|
| SC-001 | Step 5 (7 regions produce output) |
| SC-002 | Step 5 (runs launch and execute concurrently, not sequentially) |
| SC-003 | Step 3 |
| SC-004 | Step 2 (cron assertion) |
| SC-005 | Step 1 (env var override needs no code change) |
| SC-006 | Step 2 / research.md Decision 4 (concurrency-pool tag present on each RunRequest) |
