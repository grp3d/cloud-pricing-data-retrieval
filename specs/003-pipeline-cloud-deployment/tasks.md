---

description: "Task list for feature 003: Scheduled Cloud Pricing Pipeline with Snapshot Manifest and Retention"
---

# Tasks: Scheduled Cloud Pricing Pipeline with Snapshot Manifest and Retention

**Input**: Design documents from `specs/003-pipeline-cloud-deployment/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: REQUIRED. Constitution v1.1.0 Principle IV makes tests mandatory for every behavior change. It also makes them **test-first** for:
- price transformation
- manifest and revision building, and status derivation
- the `latest.json` compare-and-swap
- retention decisions
- backfill manifest generation

For those, the test task comes first and MUST fail before the implementation task starts. Tests for wiring, the CLI, Dagster, Docker, OpenTofu and workflows MAY be written afterwards.

**Organization**: Tasks are grouped by user story, US1–US9 from spec.md. P1 stories come first, and within P1 the order is US2 → US1 → US3. US2 (a local run that publishes the manifest) is the minimal end-to-end slice the other stories build on.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on incomplete tasks)
- **[Story]**: the user story this task belongs to (US1–US9)
- Paths are relative to the repository root. References such as "R9" point to `research.md`, "FR-0xx" to `spec.md`, and `contracts/…` to files under `specs/003-pipeline-cloud-deployment/contracts/`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: dependency split, package skeleton and test scaffolding.

- [X] T001 Split dependencies into three files:
  - `requirements.txt`: runtime only. Remove `dagster`, `dagster-webserver` and `dagster-dg-cli`; keep `boto3`, `click`, `pandas`, `openpyxl`, `pyarrow` and `httpx`.
  - New `requirements-dagster.txt`: the three dagster packages plus `-r requirements.txt`.
  - New `requirements-dev.txt`: `-r requirements.txt`, `pytest`, `pytest-asyncio`, `moto[s3,sns,sqs,events,cloudwatch]>=5`, `jsonschema>=4`.

  Update the Setup section of `README.md` to match.
- [X] T002 Create the package skeleton:
  - `src/pipeline/__init__.py`
  - `src/pipeline/__main__.py`: a click group named `cli` with no commands yet, plus `if __name__ == "__main__": cli()`
  - `src/pipeline/schemas/manifest.schema.json` and `src/pipeline/schemas/latest.schema.json`: byte-for-byte copies of `specs/003-pipeline-cloud-deployment/contracts/*.schema.json`
  - `src/watchdog/__init__.py`
- [X] T003 [P] Create small, deterministic AWS pricing JSON fixtures under `tests/fixtures/pricing/`. Use the same shape as the real price-list files: `offerCode`, `publicationDate`, `products` (with `attributes.regionCode`) and `terms.OnDemand…priceDimensions.pricePerUnit`. Include:
  - `AmazonS3` and `AmazonEC2` for `us-east-1` and `eu-west-1`
  - one product whose `regionCode` belongs to another region (it must be filtered out)
  - one price whose `pricePerUnit` value isn't numeric (e.g., `"N/A"`)
- [X] T004 [P] Create `tests/helpers/fake_pricing.py` with `FakePricingSource`. It implements the downloader interface the runner uses (see T031 in US2). For each region you configure a behavior:
  - `ok`: copy that region's fixtures
  - `permanent_error`: raise a non-retryable error
  - `fail_times(n)`: raise a retryable error n times, then succeed
  - `no_price_list(service)`

  It records every call so tests can assert that no download happened.
- [X] T005 [P] Add shared fixtures to `tests/conftest.py`:
  - `local_store` (a `file://` root under `tmp_path`)
  - `s3_store` (a moto `mock_aws` bucket `test-data` in `us-east-1`, with an `s3://test-data/` root)
  - `store` (parametrized over both)
  - `frozen_clock` (an injectable `now()` function)
  - `no_sleep` (records sleep durations instead of sleeping)
- [X] T006 [P] Update `.gitignore`: add `.localdata/`, `infra/**/.terraform/`, `*.tfstate*`, `*.tfplan`, `plan.json` and `result.json`. Create `.dockerignore` excluding `venv/`, `.venv/`, `specs/`, `tests/`, `infra/`, `.git/`, `.localdata/`, `**/__pycache__/`, `dagster.log` and `.tmp_dagster_home_*/`.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: settings, storage, layout, retries and run claims. Every story depends on these.

**⚠️ CRITICAL**: no user-story work starts until this phase is complete.

- [X] T007 [P] Write `tests/unit/test_settings.py` for `PipelineSettings.from_env()`. Cover:
  - every default in `contracts/configuration.md` (container table), except `MAX_CLOCK_SKEW_SECONDS`, which is added in US9
  - env overrides
  - `PIPELINE_STORAGE_URI` defaulting to `file://$DATA_DIRECTORY_ROOT/pipeline`, or `file://./pipeline` when `DATA_DIRECTORY_ROOT` is unset
  - `PRICING_REGIONS` delegating to `src/aws_regions.resolve_regions()`
  - `PIPELINE_HOST_LABEL` defaulting to `local` and validated against `[a-z0-9-]{1,63}`

  Validation errors (raising `SettingsError`) for:
  - an unknown `PRICING_DOWNLOAD_RETRY_STRATEGY`
  - negative retries, delays or grace values
  - `RUN_CLAIM_TTL_MINUTES <= RUN_TIMEOUT_MINUTES`
  - `RAW_RETENTION_DAYS < 1` and `PARQUET_WEEKLY_RETENTION_MONTHS < 1`
  - a URI scheme other than `file`/`s3`
  - a bad `PIPELINE_PROVIDER`
- [X] T008 Implement `src/pipeline/config.py`:
  - a frozen dataclass `PipelineSettings` holding every setting from `contracts/configuration.md`, except `MAX_CLOCK_SKEW_SECONDS` (US9)
  - `from_env(environ=os.environ)`
  - `SettingsError`
  - `redacted()`, a dict for the `settings` command

  The retry fields must be named `pricing_download_retry_max_retries`, `pricing_download_retry_strategy` and `pricing_download_retry_base_delay_seconds` (FR-047, Principle III).
- [X] T009 [P] Write `tests/unit/test_storage_local_s3.py`, parametrized over `local_store` and `s3_store`. Cover:
  - `put_bytes`/`get_bytes`, `put_file`/`download_file` and `head` (size, etag, last_modified)
  - recursive `list(prefix)` returning root-relative `/` keys
  - `delete`, and `delete` of a missing key as a no-op
  - `put_if_absent` raising `PreconditionFailed` when the key exists
  - `put_if_match` succeeding on the current etag and raising `PreconditionFailed` on a stale etag
  - `open_storage()` parsing `file:///abs/path` and `s3://bucket/optional/prefix`, with the prefix applied transparently

  If moto doesn't honor `IfNoneMatch`/`IfMatch` on `put_object`, add a `ConditionalS3Stub` in `tests/helpers/` and mark those cases accordingly (R21).
- [X] T010 Implement `src/pipeline/storage.py` (R4). It contains:
  - the `Storage` Protocol
  - `ObjectInfo(key, size, etag, last_modified)`
  - `PreconditionFailed`
  - `LocalStorage`: `put_if_absent` via `os.open(O_CREAT|O_EXCL)` with a temp-file write then `os.link`/`os.replace`; `put_if_match` under `fcntl.flock` on `<root>/.storage.lock`, with the etag being sha256 of the content; atomic writes via temp file plus `os.replace`
  - `S3Storage`: boto3, `IfNoneMatch="*"` / `IfMatch=etag`, mapping HTTP 412 to `PreconditionFailed`, paginated `list_objects_v2`, and `upload_file` for large files
  - `open_storage(uri)`
- [X] T011 [P] Write `tests/unit/test_layout.py`. It asserts the exact key strings from `contracts/storage-layout.md` for:
  - raw run prefixes and raw file keys (`.json.zst`)
  - Parquet data keys (`part-<run_id>.parquet`, with the optional `-<n>` suffix)
  - the current manifest, revision keys (zero-padded 4 digits), `latest.json` and claim keys

  It also covers `new_run_id(now)` (format `^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$`, sortable) and the rejection of a malformed `snapshot_date`, region or provider.
- [X] T012 Implement `src/pipeline/layout.py`: pure functions for every key in `contracts/storage-layout.md`, `new_run_id()`, `parse_revision_key()`, and `TABLES = ("service_dim","product_dim","product_attribute","region_dim","price_fact")`.
- [X] T013 [P] Write `tests/unit/test_retry_policy.py`. For base 30 and retries n = 1, 2 and 3, the delays must be:
  - `fixed`: 30, 30, 30
  - `linear_backoff`: 30, 60, 90
  - `exponential_backoff`: 30, 60, 120

  Also cover: `max_retries=0` never retries; `RetryPolicy.from_settings()`; and `run_with_retry(fn, is_retryable, sleep)`, which retries only retryable errors, returns the attempt count, and re-raises the last error with its attempt count.
- [X] T014 Implement `src/pipeline/retry.py` (R9): `RetryPolicy(max_retries, strategy, base_delay_seconds)`, `delay_for(retry_number)`, and the async and sync `run_with_retry` helpers with an injectable sleep.
- [X] T015 Extend `tests/unit/test_aws_pricing_api_async.py` (FR-047, R9). Cover:
  - The per-file download uses the injected `RetryPolicy` and sleep.
  - Retryable errors are retried: timeouts, connection errors, HTTP 429/5xx, a Content-Length mismatch, an empty body, and a body whose last non-whitespace byte isn't `}`.
  - Permanent errors are not retried: HTTP 4xx other than 429, and AccessDenied/ValidationException from the pricing API.
  - "No price list for region" is counted in `services_without_price_list` and is not a failure.
  - The new result fields hold the correct values: `failed_downloads` (a list of `{service, reason, attempts}`) and `max_attempts`.
  - The existing hard-coded 3-attempt loop is gone.
- [X] T016 Modify `src/aws_pricing_api.py` to make T015 pass:
  - Add `retry_policy: Optional[RetryPolicy]` to `PricingJobRequest` (default from `PipelineSettings`) and an injectable sleep.
  - Classify errors as retryable or permanent.
  - Add a cheap integrity check (Content-Length match, non-empty, trailing `}`).
  - Add `failed_downloads`, `services_without_price_list` and `max_attempts` to `PricingJobResult`.
  - Configure boto3 clients with `botocore.config.Config(retries={"mode": "standard"})`.

  Keep `run_pricing_job` backward compatible for `src/aws_pricing_cli.py` (FR-037).
- [X] T017 [P] Write `tests/unit/test_run_claims.py`, parametrized over `store` with `frozen_clock`. Cover:
  - Acquiring on an absent claim writes `{run_id, trigger, acquired_at, expires_at}`.
  - A second acquire raises `ClaimHeld(holder_run_id)` while the claim is unexpired.
  - An expired claim is taken over, and of two concurrent takeovers exactly one wins (simulate with a stale etag).
  - `release()` deletes only when `run_id` matches.
  - A context manager releases on exception.
  - `try_acquire()` returns None instead of raising (used by retention for other dates).
- [X] T018 Implement `src/pipeline/claims.py` (R5): `RunClaim`, `ClaimHeld`, `acquire(store, provider, date, run_id, trigger, ttl, now)`, `try_acquire(...)`, `release(...)` and a `held_claim(...)` context manager, using `put_if_absent` / `put_if_match`.

**Checkpoint**: settings, storage (local and S3), layout, retries and claims are tested and ready.

---

## Phase 3: User Story 2 — Each snapshot is described by a published manifest (Priority: P1) 🎯 MVP

**Goal**: a local run (`file://`) downloads, transforms and publishes a snapshot with a schema-valid manifest and `latest.json`. The `_SUCCESS`, `_REGION_COMPLETE` and `.locks` markers are gone. Dagster calls the same entry point.

**Independent Test**: `python -m src.pipeline run --regions us-east-1` against `file://.localdata`. Using only `latest.json` → manifest, every file is found and verified (`verify` exits 0). The manifest validates against `contracts/manifest.schema.json`, and no marker files exist (quickstart A1).

### Tests for User Story 2 (write first; they MUST fail) ⚠️

- [X] T019 [P] [US2] Rename `tests/unit/test_pricing_parquet_transformations.py` to `tests/unit/test_aws_pricing_transformations.py` with `git mv`, and rewrite it for the new behavior (C1, C3). Cover:
  - Output is written into a given `staging_dir` as `<table>/snapshot_date=<D>/region=<R>/part-<run_id>.parquet`.
  - Each written file gets a `TableFileResult(path, bytes, sha256, row_count)` matching the file.
  - A table with zero rows produces no file and a region entry with `row_count=0`.
  - The cross-region product filter still applies.
  - The non-numeric price fixture yields `price=None` and `unparseable_prices == 1` for that region, and is never estimated.
  - No `_SUCCESS`, `_REGION_COMPLETE` or `.locks` files are created.
  - `TABLE_SCHEMA_VERSIONS` covers all five tables with value 1.
  - A parse failure in an input file is reported, not swallowed.
- [X] T020 [P] [US2] Write `tests/unit/test_manifest.py` (data-model.md, R6). Cover:
  - `build_revision()` for revision 1 from region results (succeeded, partial, failed).
  - Status derivation over `regions.requested`.
  - Carry-forward: a later revision starts from the previous revision's per-region entries, replaces only regions that succeeded in this run, and keeps regions whose re-run failed.
  - `requested` is the union of the previous and current regions.
  - Status never regresses.
  - `previous_revision` and revision numbering.
  - `regions.failed` reasons are truncated to 500 characters.
  - `raw` entries include `purge_after = stored_at date + RAW_RETENTION_DAYS + 1`.
  - `run.host` comes from settings.
  - `run.region_results` includes `unparseable_prices`.
  - JSON serialization is deterministic (sorted keys, `Z` timestamps).
  - `active_file_paths(manifest)` returns every data file path.
- [X] T021 [P] [US2] Write `tests/unit/test_latest_pointer.py`, parametrized over `store`. Cover:
  - `update_latest()` writes only for a `succeeded` manifest.
  - It creates `latest.json` when missing.
  - It never moves to an older `snapshot_date`.
  - For the same date it moves only to a higher revision.
  - It retries on `PreconditionFailed` (simulate a concurrent writer) up to 5 times, then raises.
  - The written document matches `contracts/latest.schema.json`.
- [X] T022 [P] [US2] Write `tests/unit/test_contract_schemas.py` (C2). Cover:
  - `src/pipeline/schemas/*.json` are byte-identical to `specs/003-pipeline-cloud-deployment/contracts/*.schema.json`.
  - Manifests from `build_revision()` (succeeded, partial, failed) and documents from `update_latest()` validate against the schemas with `jsonschema` (Draft 2020-12, with a format checker).

  Leave placeholders (skipped tests) for the purged manifest (added in US5) and the backfill manifest (added in US8).

  Also create `tests/helpers/contracts.py` with `assert_valid_contract(store, provider="aws")`, which validates every JSON document under `<provider>/manifests/` in a storage root against `src/pipeline/schemas/*.schema.json`.
- [X] T023 [P] [US2] Write `tests/integration/test_run_snapshot_local.py` using `FakePricingSource` and `local_store`. Cover:
  - A 2-region full run produces the exact layout in `contracts/storage-layout.md`: raw `.json.zst` files, five tables, `revisions/0001.json`, `manifest.json` identical to the revision, and `latest.json`.
  - The manifest is `succeeded`.
  - `verify_snapshot()` reports no mismatches.
  - No marker or lock files exist anywhere.
  - A run with one `permanent_error` region produces status `partial`, lists `regions.failed` with a reason, and leaves `latest.json` unchanged.
  - A run where every region fails produces status `failed`, empty `tables`, and no `latest.json`.
  - The uncompressed staging files are deleted after the run.
  - After each scenario, call `assert_valid_contract(store)`, so the stored `manifest.json`, each `revisions/NNNN.json` and `latest.json` are validated against the schemas. This checks what the code actually writes (constitution IV).
- [X] T024 [P] [US2] Write `tests/integration/test_run_snapshot_s3.py`, running the same scenarios as T023 against `s3_store` (moto), including the `assert_valid_contract` checks.

### Implementation for User Story 2

- [X] T025 [US2] Rename `src/pricing_parquet_transformations.py` to `src/aws_pricing_transformations.py` with `git mv` (C3). Update every import: `src/dagster_app/assets/pricing_assets.py` and the tests.
- [X] T026 [US2] Modify `src/aws_pricing_transformations.py` to make T019 pass:
  - Remove every use of `partition_markers` (the invalidate/mark/finalize/lock calls and the `marker_*` result fields).
  - `TransformRequest` gains `staging_dir` and `run_id`, and drops `expected_regions`.
  - Write `part-<run_id>.parquet` (snappy).
  - Compute the sha256, bytes and row count (from the pyarrow metadata) of each written file.
  - Count `unparseable_prices` in `_parse_terms`.
  - Export `TABLE_SCHEMA_VERSIONS`.
  - `TransformResult` returns `files: list[TableFileResult]`, `row_counts`, `unparseable_prices`, `parse_failed_files` and `errors`.
- [X] T027 [US2] Delete `src/partition_markers.py`, `tests/unit/test_partition_markers.py`, `tests/unit/test_region_run_lock.py` and `tests/integration/test_success_markers_multi_region.py` (FR-019).
- [X] T028 [US2] Implement `src/pipeline/manifest.py` to make T020 and T022 pass:
  - dataclasses mirroring data-model.md (`Manifest`, `RunInfo`, `RegionResult`, `TableEntry`, `RegionTableEntry`, `DataFile`, `RawEntry`)
  - `build_revision(previous, run_info, region_outcomes, settings, now)`
  - `derive_status()`
  - `to_json()` / `from_json()`
  - `active_file_paths()`
  - `write_revision(store, manifest)`: writes `revisions/<NNNN>.json` via `put_if_absent`, then overwrites `manifest.json`
  - `read_current(store, provider, date)`
  - `next_revision_number(store, provider, date)`
  - `MANIFEST_VERSION = "1.0"`
- [X] T029 [P] [US2] Implement `src/pipeline/latest.py` (R7): `update_latest(store, manifest, now)` with compare-and-swap and bounded retries, and `read_latest(store, provider)`.
- [X] T030 [P] [US2] Implement `src/pipeline/verify.py`: `verify_snapshot(store, provider, date=None)`. It iterates over active manifests, and for each file checks existence, `bytes` and `sha256`, and `row_count` from the Parquet footer. It returns a list of mismatches (SC-006).
- [X] T031 [US2] Implement `src/pipeline/runner.py`: `run_snapshot(settings, request, *, store=None, downloader=None, now=utcnow, sleep=time.sleep) -> RunReport`. `RunRequest` has `snapshot_date`, `regions`, `trigger` and `mode`. `RunReport` follows data-model.md, with `retention` and `alerts_sent` left empty for now. The flow follows data-model.md "Run flow":
  1. Validate the settings.
  2. Hold the claim via `claims.held_claim` with a TTL (refusal comes in US4).
  3. Enforce a run deadline of `RUN_TIMEOUT_MINUTES`.
  4. Download concurrently per region into a temp dir, using the default downloader wrapping `run_pricing_job` with `RetryPolicy.from_settings`.
  5. Compress each raw file with zstd level 3 and upload it to the raw prefix (`compression.zstd`, R8).
  6. Transform one region at a time as its download completes, bounded by a `TRANSFORM_CONCURRENCY` semaphore.
  7. Upload the Parquet files, then delete that region's temp files.
  8. Build and write the revision, then call `update_latest` if the status is `succeeded`.
  9. Emit the RunReport as one JSON log line.

  A region fails if its download has any `failed_downloads` or `parse_failed_files`.
- [X] T032 [US2] Add the `run`, `verify` and `settings` commands to `src/pipeline/__main__.py` per `contracts/cli.md`:
  - `run` options: `--snapshot-date`, `--regions`, `--trigger scheduled|manual` and `--skip-retention`. `--transform-only` is added in US4.
  - Logging is JSON lines to stdout.
  - Exit codes: 0 on completion, 2 on a `SettingsError` or a bad argument, 1 from `verify` on a mismatch.
- [X] T033 [US2] Rewrite the Dagster app as an optional local runner over `run_snapshot` (FR-007, R16):
  - `src/dagster_app/assets/pricing_assets.py`: a single op, `run_pricing_snapshot`, with config `snapshot_date`, `regions` and `transform_only`, that builds `PipelineSettings.from_env()` and calls `run_snapshot`. It raises if the report is `refused`/`error`, or if the snapshot status is `failed`.
  - `src/dagster_app/jobs/pricing_jobs.py`: job `pricing_snapshot`.
  - `src/dagster_app/schedules/pricing_schedules.py`: a weekly schedule producing one RunRequest with trigger `scheduled`.
  - Delete `src/dagster_app/sensors/` and remove it from `src/dagster_app/definitions.py`.
- [X] T034 [US2] Update the Dagster tests: rewrite `tests/unit/test_pricing_schedule.py` to expect one run per tick with the cron `0 13 * * 1` kept, adjust `tests/unit/test_pricing_resources.py`, delete `tests/unit/test_pricing_sensors.py`, and delete `tests/integration/test_multi_region_run.py`, which is superseded by `test_run_snapshot_local.py`. They use `pytest.importorskip("dagster")` for developers without the extra installed. CI installs `requirements-dagster.txt`, so these tests must run there, not be skipped (T074).
- [X] T035 [US2] Update `README.md`: replace the "Partition success markers" section with a "Snapshot manifest" section linking `specs/003-pipeline-cloud-deployment/contracts/`, and document `python -m src.pipeline run|verify|settings` and `PIPELINE_STORAGE_URI`.

**Checkpoint**: local end-to-end MVP. Snapshots are published with a manifest and `latest.json`, and are verifiable (quickstart A1).

---

## Phase 4: User Story 1 — Weekly snapshot runs in the cloud without the owner (Priority: P1)

**Goal**: the same pipeline runs weekly on ECS Fargate from a schedule, storing data in a protected S3 bucket. Nothing stays running afterwards.

**Independent Test**: after applying `bootstrap` (state), `data` and `pipeline` for `prod` and pushing an image, trigger the task (or wait for the schedule). All configured regions' raw files and tables, the manifest and `latest.json` appear in S3, and no task keeps running (quickstart B3).

### Implementation for User Story 1

- [X] T036 [P] [US1] Create the `Dockerfile` (R17):
  - Base `python:3.14-slim`.
  - `ARG GIT_SHA=local IMAGE_TAG=local`, exported as environment variables.
  - Install `requirements.txt` only.
  - Copy `src/`.
  - A non-root user `pipeline`.
  - `ENTRYPOINT ["python","-m","src.pipeline"]` and `CMD ["run","--trigger","scheduled"]`.

  Verify `pyarrow`/`pandas` wheels install for `linux/arm64` and `linux/amd64`. If not, switch to `python:3.13-slim`, add `zstandard`, and make `runner.py` import zstd through a small compatibility shim (R8, R17).
- [X] T037 [P] [US1] Create `infra/bootstrap/`:
  - `versions.tf`: OpenTofu ≥ 1.10, `hashicorp/aws ~> 6.0`.
  - `providers.tf`: `default_tags` `project=cloud-pricing`, `component=bootstrap`, `environment=shared` (account singletons, constitution V v1.1.1).
  - `variables.tf`: `aws_region`, `state_bucket_name` (required, owner-chosen), `environments` (default `["prod"]`), `github_repository`, `budget_email`, `budget_warning_usd`, `budget_alert_usd`.
  - `state.tf`: the state bucket with versioning, SSE-S3, Block Public Access and a TLS-only policy.
  - `outputs.tf`.
  - `README.md`: first apply with local state, then `tofu init -migrate-state` into the new bucket (R13).
- [X] T038 [P] [US1] Create `infra/envs/prod.tfvars` with the non-secret values from `contracts/configuration.md` (environment `prod`, regions, schedule, sizes, retention and retry defaults). Create `infra/envs/prod.backend.hcl` with the state bucket, `region`, `use_lockfile = true`, and a per-stack `key` passed as `-backend-config="key=prod/<stack>.tfstate"`.
- [X] T039 [P] [US1] Create `infra/data/` (R15):
  - `versions.tf`, a `backend.tf` with an empty `backend "s3" {}` block, and `providers.tf` with `default_tags` `project=cloud-pricing`, `component=data`, `environment=<var.environment>`.
  - `variables.tf`, with `environment` validated to `dev|qa|prod`, `pricing_providers`, `raw_retention_days` and `noncurrent_version_retention_days`.
  - `main.tf`:
    - bucket named by the required `data_bucket_name` variable (prod: `cloud-pricing-data-prod-g08a9i`) with `lifecycle { prevent_destroy = true }`
    - Block Public Access (all four) and `BucketOwnerEnforced`
    - SSE-S3
    - versioning, plus a lifecycle rule expiring noncurrent versions after `noncurrent_version_retention_days`
    - a bucket policy denying `aws:SecureTransport=false`
  - `read_policy.tf`: the managed policy `cloud-pricing-data-read-<env>` (`s3:GetObject` on `*/manifests/*` and `*/parquet/*`, `s3:ListBucket`).
  - `outputs.tf`: `data_bucket_name`, `data_bucket_arn`, `data_read_policy_arn`.
- [X] T040 [P] [US1] Create `infra/pipeline/network.tf` (R3): a VPC `10.42.0.0/24` with 2 public subnets in different AZs, an internet gateway, a route table, a free S3 gateway endpoint, and a security group allowing all egress and no ingress, all named `cloud-pricing-*-<env>`.
- [X] T041 [P] [US1] Create `infra/pipeline/ecr.tf`: repository `cloud-pricing-pipeline-<env>` with immutable tags, scan-on-push, and a lifecycle policy keeping the last 10 images.
- [X] T042 [US1] Create `infra/pipeline/ecs.tf` (R1):
  - cluster `cloud-pricing-<env>`
  - log group `/cloud-pricing/pipeline-<env>` with `log_retention_days` (FR-029)
  - task execution role (ECR pull, logs)
  - task role: S3 get/put/delete/list on the data bucket for `${provider}/*` for each provider, `pricing:DescribeServices`, `GetProducts`, `ListPriceLists`, `GetPriceListFileUrl`, and `ec2:DescribeRegions`
  - Fargate task definition `cloud-pricing-pipeline-<env>`: `ARM64`, `task_cpu`/`task_memory`/`ephemeral_storage_gib`, image `<ecr>:<image_tag>`, the awslogs driver, and environment variables from `contracts/configuration.md` (`PIPELINE_STORAGE_URI=s3://<bucket>/`, `PIPELINE_HOST_LABEL=aws-ecs`, and every retention, retry and timeout value from variables)

  Look up the data bucket via `data "aws_s3_bucket"` by name.
- [X] T043 [US1] Create `infra/pipeline/scheduler.tf` (R2): an EventBridge Scheduler schedule `cloud-pricing-pipeline-<env>` with `schedule_expression` in time zone `UTC` and `state = schedule_enabled ? "ENABLED" : "DISABLED"` (FR-051). Its ECS RunTask target has launch type FARGATE, public-IP network config and an `overrides` command of `run --trigger scheduled`. It uses a scheduler IAM role with `ecs:RunTask` on the task-definition family and `iam:PassRole` on the two task roles.
- [X] T044 [US1] Create the remaining `infra/pipeline` files:
  - `versions.tf`, `backend.tf` and `providers.tf` (`default_tags` `project=cloud-pricing`, `component=pipeline`, `environment=<var.environment>`)
  - `variables.tf`, covering every pipeline variable in `contracts/configuration.md`, with validations for `environment`, the retry strategy enum and `run_claim_ttl_minutes > run_timeout_minutes`
  - `outputs.tf`: `ecs_cluster_arn`, `task_definition_family`, `subnet_ids`, `security_group_id`, `ecr_repository_url`
- [X] T045 [US1] Write `infra/README.md`. It covers:
  - the apply order: bootstrap → data → pipeline
  - the exact `tofu init -backend-config=../envs/prod.backend.hcl -backend-config="key=prod/<stack>.tfstate"` and `tofu apply -var-file=../envs/prod.tfvars` commands
  - a manual image build and push (`docker buildx build --platform linux/arm64 --push -t <ecr>:<sha> --build-arg GIT_SHA=<sha> .`), used until CI exists (US6)
  - the documented `aws ecs run-task` command for an on-demand run, using the stack outputs
- [X] T046 [US1] Run `tofu fmt -check -recursive infra/` and `tofu init -backend=false && tofu validate` in `infra/bootstrap`, `infra/data` and `infra/pipeline`, and fix any issues. Build the image locally with `docker buildx build --platform linux/arm64 .` and run `docker run --rm <img> settings` to confirm it starts. Confirm every resource type that supports tags inherits the default tags (e.g., `tofu plan` shows `tags_all`). Confirm every named resource in `data` and `pipeline` ends in `-<env>` (see Notes → Resource naming). T056 and T092 repeat both checks for the resources they add.

**Checkpoint**: an unattended weekly cloud run is possible (quickstart B3 using the manual push).

---

## Phase 5: User Story 3 — Failures and missed runs reach the owner quickly (Priority: P1)

**Goal**: partial, failed, refused and crashed runs, schedule-launch failures, and missed weeks all reach the owner by email. Success summaries are optional.

**Independent Test**: a run with a failing region produces a `partial` manifest and a `RUN PARTIAL` email within 15 minutes. Disabling the schedule (or setting the window to 0) produces a `MISSED RUN` email (quickstart B4, B6).

### Tests for User Story 3 ⚠️

- [X] T047 [P] [US3] Write `tests/unit/test_notify.py`. Cover:
  - `Notifier.from_settings()` is log-only when `ALERT_TOPIC_ARN` is unset and uses SNS when set (moto).
  - For each kind in `contracts/configuration.md` (`RUN FAILED`, `RUN PARTIAL`, `RUN REFUSED`, `RETENTION ERROR`, `RUN SUCCEEDED`, `RUN CRASHED`), the subject is `[cloud-pricing <env>] <KIND>: <provider> <date>`, truncated to SNS's 100-character limit.
  - The body contains readable fields followed by the RunReport JSON.
  - `RUN SUCCEEDED` is sent only when `SUCCESS_SUMMARY_ENABLED`.
  - An SNS publish failure is logged and never raised.
- [X] T048 [P] [US3] Write `tests/integration/test_run_alerts.py` using `FakePricingSource` and a recording notifier. Cover:
  - A partial run sends `RUN PARTIAL`.
  - A run where every region fails sends `RUN FAILED`.
  - A run that succeeds but had a failed region attempt that was carried forward sends `RUN PARTIAL`.
  - Success with summaries enabled sends `RUN SUCCEEDED` with row counts and duration, and sends nothing when disabled.
  - A CLI invocation (click `CliRunner`) where the runner raises an unexpected exception sends `RUN CRASHED` and exits non-zero (FR-057).
- [X] T049 [P] [US3] Write `tests/unit/test_freshness_check.py` for `src/watchdog/freshness_check.handler` with moto S3 and SNS. Cover:
  - A missing `latest.json` sends `MISSED RUN`.
  - `today - snapshot_date > MAX_SNAPSHOT_AGE_DAYS` sends `MISSED RUN`.
  - A fresh snapshot sends nothing.
  - Each provider in `PROVIDERS` is checked independently.
  - `DATA_PREFIX` is honored.

### Implementation for User Story 3

- [X] T050 [P] [US3] Implement `src/pipeline/notify.py` (R12): `Notifier` with `send(kind, report)`, subject and body formatting, the SNS client, and a log-only fallback.
- [X] T051 [US3] Wire notifications:
  - In `src/pipeline/runner.py`, decide the alert kinds from the RunReport (the snapshot status and this run's `region_results`) and record `alerts_sent`.
  - In `src/pipeline/__main__.py`, add a top-level exception handler that sends a best-effort `RUN CRASHED` alert (with a truncated traceback) and exits 1.
- [X] T052 [P] [US3] Implement `src/watchdog/freshness_check.py` (R11): `handler(event, context)`, stdlib plus boto3 only, compatible with Python 3.13. It reads `<prefix><provider>/manifests/latest.json` for each provider and publishes `MISSED RUN` or returns quietly.
- [X] T053 [US3] Create `infra/pipeline/alerts.tf`:
  - SNS topic `cloud-pricing-pipeline-alerts-<env>` with an email subscription to `var.alert_email` and a topic policy allowing EventBridge and CloudWatch to publish
  - `sns:Publish` added to the task role
  - `ALERT_TOPIC_ARN` and `SUCCESS_SUMMARY_ENABLED` added to the task definition environment (edit `infra/pipeline/ecs.tf`)
  - an EventBridge rule on `aws.ecs` `ECS Task State Change` for the pipeline task-definition family, with `lastStatus=STOPPED` and either a non-zero `containers[].exitCode` or `stopCode=TaskFailedToStart`. It targets SNS with an input transformer producing the `TASK CRASHED` subject and body.
- [X] T054 [US3] Edit `infra/pipeline/scheduler.tf` (R2): add an SQS dead-letter queue `cloud-pricing-scheduler-dlq-<env>`, a retry policy (`maximum_retry_attempts=2`, `maximum_event_age_in_seconds=3600`) and a DLQ target. Create the CloudWatch alarm `SCHEDULE FAILED` on `ApproximateNumberOfMessagesVisible > 0`, sending to SNS.
- [X] T055 [US3] Create `infra/pipeline/watchdog.tf`:
  - `archive_file` of `src/watchdog/`
  - Lambda `cloud-pricing-watchdog-<env>` (python3.13, 128 MB, 30 s timeout), with the environment variables `DATA_BUCKET`, `DATA_PREFIX`, `PROVIDERS`, `MAX_SNAPSHOT_AGE_DAYS` and `ALERT_TOPIC_ARN`
  - an IAM role allowing `s3:GetObject` on `*/manifests/latest.json`, `sns:Publish` and logs
  - a log group with retention
  - a daily EventBridge Scheduler schedule at `cron(0 14 * * ? *)`
  - a CloudWatch alarm `WATCHDOG ERROR` on the Lambda's `Errors` metric (≥ 1 over 1 day), sending to SNS
- [X] T056 [US3] Re-run `tofu fmt`/`validate` for `infra/pipeline` and add the alert and watchdog sections to `infra/README.md`, including the one-time SNS email confirmation. Also repeat T046's tag-inheritance and `-<env>` naming checks for the resources this story adds.

**Checkpoint**: all P1 stories are done. The pipeline runs unattended in the cloud, publishes its contract, and alerts on every failure mode.

---

## Phase 6: User Story 4 — Owner repairs a snapshot without a full re-run (Priority: P2)

**Goal**: re-run chosen regions, rebuild tables from stored raw data without downloading, refuse concurrent runs for the same date, and get distinguishable revisions.

**Independent Test**: after a `partial` run, re-running just the failed region produces a `succeeded` revision and moves `latest.json`. A transform-only run rebuilds the tables with no downloads. A second concurrent run is refused (quickstart A3, A5).

### Tests for User Story 4 ⚠️

- [X] T057 [P] [US4] Extend `tests/integration/test_run_snapshot_local.py` with the following tests:
  - `test_partial_then_region_rerun_succeeds`: region B uses `fail_times(99)` then `ok`. Revision 2 downloads only B, carries forward A's files unchanged (same paths, `written_by_run` = run 1), ends `succeeded`, and `latest.json` points to it.
  - `test_transform_only_rebuilds_without_download`: `FakePricingSource` records zero calls, new `part-<run_id2>` files are written, the old files still exist, and the revision is incremented.
  - `test_transform_only_with_purged_raw_exits_3`: with the raw prefix deleted, the CLI exits 3 and no objects change.
  - `test_rerun_failure_keeps_good_data`: re-running a succeeded region with `permanent_error` keeps its previous files, the status stays `succeeded`, and `region_results` records the failure.
  - `test_run_mode_recorded`: `run.mode` is `regions` or `transform-only`.
- [X] T058 [P] [US4] Write `tests/integration/test_concurrent_runs.py`, parametrized over `store`. Cover:
  - With a claim held by another run_id, `run_snapshot` returns outcome `refused`, writes no objects outside `claims/`, and the CLI exits 0.
  - A scheduled trigger records a `RUN REFUSED` alert, and a manual trigger does not.
  - An expired claim is taken over and the run proceeds.
  - Two threads starting simultaneously: exactly one runs and one is refused.

### Implementation for User Story 4

- [X] T059 [US4] Extend `src/pipeline/runner.py`:
  - Catch `ClaimHeld` and return a `refused` RunReport, alerting on scheduled runs.
  - Mode `regions` when a subset of regions is requested.
  - Mode `transform-only`:
    - Read the current manifest (exit 3 if there is none).
    - For each requested region, list its `raw.location`. Exit 3 with "raw data purged" if any region is missing, before any write.
    - Download and decompress the `.json.zst` files into a temp dir.
    - Transform, then publish a new revision.
  - `requested` is the union of the previous and current regions (via `build_revision`).
- [X] T060 [US4] Add `--transform-only` to `run` in `src/pipeline/__main__.py`, and map precondition errors to exit 3 (`contracts/cli.md`).

**Checkpoint**: targeted repair and safe concurrency work locally and on S3.

---

## Phase 7: User Story 5 — Storage cost stays bounded by rule (Priority: P2)

**Goal**: raw data expires after 30 days, and Parquet is thinned to one snapshot per month after 12 months. Superseded and orphaned files are cleaned up after the grace period, inline at the end of each run. The guard ensures no active file is ever deleted. There is a dry run, and a raw listing and download for owners.

**Independent Test**: with 15 months of seeded history, the retention dry-run plan equals what the real run deletes. Purged manifests are marked. `latest.json`'s date survives. `verify` still passes (quickstart A6; `tests/unit/test_retention.py`).

### Tests for User Story 5 (write first; they MUST fail) ⚠️

- [X] T061 [P] [US5] Write `tests/unit/test_retention.py`, parametrized over `store` with `frozen_clock` and `no_sleep` (R10, FR-020–FR-024, FR-045, FR-046, FR-048–FR-050). Seed builder: snapshots across 15 months, a mix of succeeded, partial and failed, with multiple revisions. Cover:
  - **Thinning**: for months older than `PARQUET_WEEKLY_RETENTION_MONTHS`, the earliest `succeeded` snapshot is kept and the rest are purged. A month with no `succeeded` snapshot purges all of them. The `latest.json` date is never purged.
  - **Purge order**: a new `purged` revision (empty tables, `purged_at`) is written before any delete.
  - **Superseded files**: eligible when `now ≥ manifest.created_at + grace` (the superseding revision's time). **Orphans** (never listed by any revision) are eligible when `now ≥ last_modified + grace` (research R10).
  - **Guard**: a file referenced by a freshly re-read active manifest is skipped. Simulate this by making a concurrent revision list the file between planning and deleting, using a hook.
  - **Busy dates**: another date with a held claim is skipped and reported in `skipped_busy_dates`.
  - **Dry-run parity**: a dry run writes and deletes nothing, and its plan equals the real run's actions.
  - **Local raw**: raw run folders older than `RAW_RETENTION_DAYS` are deleted for `file://` only.
  - **Inline wait**: with grace ≤ `SUPERSEDED_FILE_INLINE_WAIT_MAX_MINUTES`, the step sleeps only for the remaining grace time (e.g., 180 s when the manifest was published 2 minutes earlier with grace 5). With grace above the limit, there is no sleep and the files are left for later.
- [X] T062 [P] [US5] Enable the purged-manifest case in `tests/unit/test_contract_schemas.py`: a manifest from `build_purged_revision()` validates.
- [X] T063 [P] [US5] Add retention tests to `tests/integration/test_run_snapshot_local.py`:
  - `test_inline_retention_cleans_superseded_after_rerun` (grace 5 with injected sleep): after a re-run, the old files are deleted in the same run and `RunReport.retention.waited_seconds ≈ 300`.
  - `test_retention_error_does_not_change_status` (FR-050): retention is patched to raise. The manifest stays `succeeded`, `latest.json` is unchanged, a `RETENTION ERROR` alert is recorded, and the exit code is 0.
  - Both tests end with `assert_valid_contract(store)`.
- [X] T064 [P] [US5] Write `tests/unit/test_raw_access.py`. Cover:
  - `list_raw()` returns one row per (date, region, run_id) with `file_count`, `bytes`, `stored_at` and `purge_after` from manifests (falling back to object metadata).
  - `download_raw(date, dest, regions, decompress)` writes `.json.zst`, or `.json` with `decompress`, and only for the requested regions.
  - A missing snapshot raises the precondition error that maps to exit 3.

### Implementation for User Story 5

- [X] T065 [US5] Add `build_purged_revision(current, now)` to `src/pipeline/manifest.py`: `status=purged`, `tables={}`, `purged={purged_at, reason:"retention-thinning"}`, `run.mode=retention-purge`, and the next revision number.
- [X] T066 [US5] Implement `src/pipeline/retention.py`:
  - `plan_retention(store, settings, now, own_date=None) -> RetentionPlan`
  - `apply_retention(store, settings, plan, now, sleep, own_date, dry_run)`
  - Superseded and orphan cleanup with the inline wait for `own_date`.
  - Other dates handled via `claims.try_acquire`.
  - Monthly thinning.
  - Purge writes the manifest first, then deletes.
  - Before each delete, re-read the current manifest for that date and skip the key if it's now referenced (FR-046).
  - Local raw expiry.
  - The plan and result serialize to the JSON shape in `contracts/cli.md`.
- [X] T067 [US5] Wire inline retention into `src/pipeline/runner.py` (FR-048–FR-050): after the manifest and `latest.json`, and while still holding the claim, call `apply_retention(own_date=snapshot_date)` unless `--skip-retention`. Catch every exception into `RunReport.retention.error` and send a `RETENTION ERROR` alert. Never change the snapshot status.
- [X] T068 [P] [US5] Implement `src/pipeline/raw_access.py`: `list_raw()` and `download_raw()`, decompressing with `compression.zstd`.
- [X] T069 [US5] Add `retention [--dry-run] [--output PATH]` and `raw list [--json]` / `raw download --snapshot-date --dest [--regions] [--decompress]` to `src/pipeline/__main__.py` per `contracts/cli.md`.
- [X] T070 [P] [US5] Create `infra/data/lifecycle.tf` (FR-020): one lifecycle rule per entry in `var.pricing_providers`, expiring the prefix `<provider>/raw/` after `raw_retention_days` days and aborting incomplete multipart uploads after 1 day. Merge it with the existing noncurrent-version rule in a single `aws_s3_bucket_lifecycle_configuration`, moving that rule out of `infra/data/main.tf`.

**Checkpoint**: storage is bounded by rule, both locally and in S3, and the dry run is trustworthy.

---

## Phase 8: User Story 6 — Environment is reproducible from code and deployed from CI (Priority: P2)

**Goal**: federated CI access with no keys, an account budget alert, CI tests and plans on PRs, and image publishing plus an approval-gated apply on `main`. On-demand runs start from a workflow. All of it is parameterized by environment.

**Independent Test**: a PR shows tests and a `tofu plan`. Merging publishes an image and waits for approval, and the approved apply updates `prod`. Destroying and re-applying the pipeline stack restores a working schedule, with the data intact (quickstart B2, B8).

### Implementation for User Story 6

- [X] T071 [P] [US6] Create `infra/bootstrap/github_oidc.tf` (R13, R14): the IAM OIDC provider `token.actions.githubusercontent.com` (an account singleton, tagged `environment=shared`), and for each `env` in `var.environments`:
  - **`cloud-pricing-gha-plan-<env>`**: trusted for `repo:<repo>:pull_request`. `ReadOnlyAccess` plus read/write on `*.tflock` in the state bucket under `<env>/`.
  - **`cloud-pricing-gha-apply-<env>`**: trusted only for `repo:<repo>:environment:<env>`. Permissions scoped to `cloud-pricing-*-<env>` resources that the `data` and `pipeline` stacks manage (S3, ECR, ECS, Scheduler, SNS, SQS, Lambda, EventBridge, CloudWatch, Logs, IAM roles and policies, EC2 VPC, Roles Anywhere), plus the state bucket under `<env>/`.
  - **`cloud-pricing-gha-run-<env>`**: trusted for `repo:<repo>:ref:refs/heads/main`. `ecs:RunTask` on `cloud-pricing-pipeline-<env>` task definitions, `iam:PassRole` on its task roles, and `ecs:DescribeTasks`.

  Output a map of role ARNs keyed by environment (`gha_role_arns = { prod = { plan, apply, run } }`).
- [X] T072 [P] [US6] Create `infra/bootstrap/budget.tf` (FR-030): an AWS Budget covering the account, with actual-spend notifications at `budget_warning_usd` and `budget_alert_usd`, and a forecasted notification at `budget_alert_usd`, all to `budget_email`. Name it `cloud-pricing-shared-budget`, tagged `environment=shared` (account singleton).
- [X] T073 [P] [US6] Create `infra/envs/dev.tfvars.example` and `infra/envs/qa.tfvars.example`, documented as not provisioned, to show how environments are parameterized (FR-032). Add a CI check that `tofu validate` passes with `-var environment=dev`.
- [X] T074 [P] [US6] Create `.github/workflows/ci.yml` (on `pull_request`) with these jobs:
  - `test`: Python 3.14, `pip install -r requirements-dev.txt -r requirements-dagster.txt`, `pytest`. The Dagster tests must run in CI, not be skipped (constitution IV).
  - `docker`: buildx build for `linux/arm64,linux/amd64` with no push.
  - `tofu`: `fmt -check`, and `init -backend=false` plus `validate` for the three stacks.
  - `plan`: `data` and `pipeline` with `-var-file=infra/envs/prod.tfvars -var image_tag=${{ github.sha }}` via `aws-actions/configure-aws-credentials` assuming the repo variable `AWS_ROLE_PLAN_PROD` (`cloud-pricing-gha-plan-prod`). The plan summary is posted as a PR comment.
- [X] T075 [US6] Create `.github/workflows/deploy.yml` (on `push` to `main`):
  - `test`.
  - `build`: multi-arch `linux/arm64,linux/amd64`, with native runners where available (R14). It pushes `<ecr>:<git-sha>` with `--build-arg GIT_SHA`/`IMAGE_TAG`, using `AWS_ROLE_APPLY_PROD` (`cloud-pricing-gha-apply-prod`).
  - `apply`: `environment: prod`, so it needs the required reviewer. It runs `tofu apply` for `data`, then `pipeline`, with `-var image_tag=<sha>` and `TF_VAR_alert_email` from secrets.
- [X] T076 [P] [US6] Create `.github/workflows/run-pipeline.yml` (`workflow_dispatch`, FR-004). Inputs are `mode` (`run`|`transform-only`|`retention`), `snapshot_date`, `regions` and `dry_run`. It assumes `AWS_ROLE_RUN_PROD` (`cloud-pricing-gha-run-prod`), builds the container command override per `contracts/cli.md`, calls `aws ecs run-task` using the pipeline stack outputs (from repo variables), and prints the task ARN and a CloudWatch Logs link.
- [X] T077 [US6] Extend `infra/README.md` with:
  - the GitHub setup: the environment `prod` with a required reviewer, the secrets `TF_VAR_alert_email` and `TF_VAR_budget_email`, and the variables for the role ARNs, the AWS region and the pipeline outputs
  - the destroy and re-apply procedure for the pipeline stack (SC-008)
  - activating the `project`/`component`/`environment` cost allocation tags

**Checkpoint**: everything is deployable from CI with approval, and reproducible from code.

---

## Phase 9: User Story 7 — Same pipeline runs locally for development (Priority: P3)

**Goal**: the packaged pipeline and the existing ad hoc CLI keep working on a laptop against a local directory. No AWS storage is needed.

**Independent Test**: `docker run` with a mounted local directory produces the same layout and manifest as a cloud run. The ad hoc CLI behaves as before (quickstart A1, A8).

- [X] T078 [P] [US7] Write `tests/unit/test_aws_pricing_cli.py` (FR-037), using click `CliRunner` with `run_pricing_job` monkeypatched. It checks that `--region` and `--all-services`/`--service-code`, `--output-dir`, `--format`, `--consolidate` and `--truncate` build the same `PricingJobRequest` as before, and that the output messages and exit codes are unchanged.
- [X] T079 [P] [US7] Write `tests/integration/test_local_mode_no_cloud.py` (FR-036, SC-009). With `PIPELINE_STORAGE_URI=file://…`, and with `boto3.client("s3")` / `boto3.client("sns")` patched to raise if called, `run_snapshot` with `FakePricingSource` succeeds. Its manifest equals a moto-S3 run's manifest, except for `run_id`, timestamps and `run.host`.
- [X] T080 [US7] Update `README.md`:
  - a "Run locally" section covering `python -m src.pipeline run --regions …`, the default local root, `docker build` and a `docker run -v "$PWD/.localdata:/data" -e PIPELINE_STORAGE_URI=file:///data …` example
  - a Dagster section with the new `pricing_snapshot` job and `pip install -r requirements-dagster.txt`
  - a note that `src.aws_pricing_cli` is unchanged

**Checkpoint**: parity between local and cloud is proven by tests.

---

## Phase 10: User Story 8 — Operator uploads existing local history one snapshot at a time (Priority: P3)

**Goal**: `upload-history` publishes one local table snapshot to the store with a proper manifest. It aborts if data already exists unless `--overwrite` is given, and never uploads raw data.

**Independent Test**: uploading a legacy snapshot creates a `succeeded` backfill manifest and files that pass `verify`. A second upload without overwrite exits 3 with no writes. `--overwrite` creates revision 2. No raw objects appear (quickstart A7).

### Tests for User Story 8 (write first; they MUST fail) ⚠️

- [X] T081 [P] [US8] Write `tests/unit/test_backfill.py`, with fixtures built by writing Parquet files into a legacy tree `<tmp>/pricing_aws/parquet/<table>/snapshot_date=<D>/region=<R>/part-0.parquet` for 2 regions, plus a `pricing_aws/raw/…` folder. Call `assert_valid_contract(store)` after every successful upload. Cover:
  - A generated manifest has `origin=backfill`, `run.trigger=backfill`, `run.mode=backfill`, `raw=null`, `requested`/`succeeded` equal to the regions found, `status=succeeded`, and sha256, bytes and row counts matching the source files.
  - Files are re-keyed to `part-<run_id>.parquet`.
  - A new-layout source with an existing `manifest.json` is used as-is, with only the paths re-keyed if they differ.
  - The command aborts with exit 3 before any write if anything exists under `parquet/*/snapshot_date=<D>/` or `manifests/<D>/` in the target.
  - `--overwrite` publishes revision n+1, and the old files become superseded.
  - No key under `<provider>/raw/` is ever written, even though the source has raw files.
  - A date with no local table data exits 3.
  - `--dry-run` writes nothing and prints the manifest.
  - The claim is taken, and a held claim results in a refusal.
  - `latest.json` moves only if the date is newer.
- [X] T082 [P] [US8] Enable the backfill case in `tests/unit/test_contract_schemas.py`: a manifest from `build_backfill_manifest()` validates against the schema, including the `origin=backfill → raw=null` rule.

### Implementation for User Story 8

- [X] T083 [US8] Implement `src/pipeline/backfill.py` (R18): `find_local_snapshot(source, provider, date)` (new or legacy layout), `build_backfill_manifest(...)`, and `upload_history(store, settings, date, source, overwrite, dry_run, now)`. The upload order is data, then the revision and manifest, then `latest.json`. Hold the claim throughout.
- [X] T084 [US8] Add `upload-history --snapshot-date [--source] [--overwrite] [--dry-run]` to `src/pipeline/__main__.py` per `contracts/cli.md`. Exit 3 on an existing target without `--overwrite`, and on missing local data.

**Checkpoint**: history can be migrated one snapshot at a time, under the operator's control.

---

## Phase 11: User Story 9 — Pipeline or web app runs outside AWS against cloud storage (Priority: P3)

**Goal**: pause the Fargate schedule and run the same image on a home server against S3 with short-lived Roles Anywhere credentials. Off-AWS readers get read-only access. The same guarantees and alerts apply.

**Independent Test**: with `schedule_enabled=false`, a home-server run using only a client certificate publishes to S3 with `run.host=home-server`. A reader certificate can `verify` but not delete. A clock skewed by 10 minutes is refused (quickstart Part C).

### Tests for User Story 9 ⚠️

- [X] T085 [P] [US9] Write `tests/unit/test_clock_skew.py`. Add `MAX_CLOCK_SKEW_SECONDS` (default 300, ≥ 0) to the `tests/unit/test_settings.py` expectations. Cover:
  - For `s3://` roots, `check_clock_skew(store, now)` uses `store.server_time()`, which comes from the S3 response `Date` header (stubbed), and raises `SettingsError` when `|now - server_time| > MAX_CLOCK_SKEW_SECONDS`, so the CLI exits 2.
  - `file://` roots skip the check.
- [X] T086 [P] [US9] Add `test_host_label_recorded` to `tests/integration/test_run_snapshot_s3.py`: with `PIPELINE_HOST_LABEL=home-server`, the manifest's `run.host == "home-server"`, and the output is otherwise identical to an `aws-ecs` run (FR-052, FR-056).

### Implementation for User Story 9

- [X] T087 [US9] Add the setting `max_clock_skew_seconds` to `src/pipeline/config.py`. Add `server_time()` to `src/pipeline/storage.py` (S3: the `Date` header of a `head_bucket` response; local: `None`). Call `check_clock_skew` at the start of `run_snapshot`, `retention` and `upload_history`.
- [X] T088 [US9] Edit the `Dockerfile` (R17, R22): download AWS's `aws_signing_helper` for `$TARGETARCH`, at a pinned version whose sha256 is verified, into `/usr/local/bin/`. Document in the `Dockerfile` comments how to mount the credentials (`-v ~/.pricing:/creds:ro -e AWS_CONFIG_FILE=/creds/aws-config -e AWS_PROFILE=pricing-writer`).
- [X] T089 [P] [US9] Create `infra/data/external_access.tf` (R22). Everything in it is gated by `roles_anywhere_ca_bundle_pem != ""`:
  - An `aws_rolesanywhere_trust_anchor` (`CERTIFICATE_BUNDLE`).
  - The role `cloud-pricing-pipeline-writer-<env>`: S3 get/put/delete/list on `<provider>/*` for each provider, the pricing API actions and `ec2:DescribeRegions`. Its trust policy allows principal `rolesanywhere.amazonaws.com` to `sts:AssumeRole`, `sts:TagSession` and `sts:SetSourceIdentity`, with `aws:SourceArn` = the trust anchor and `aws:PrincipalTag/x509Subject/CN` in `external_writer_subjects`. An empty list gets a deny-all trust policy.
  - The role `cloud-pricing-data-reader-<env>`: attaches `cloud-pricing-data-read-<env>`, with the same trust shape using `external_reader_subjects`.
  - One `aws_rolesanywhere_profile` per role, with `duration_seconds = external_session_duration_seconds`.
  - The new variables in `infra/data/variables.tf` and the outputs in `infra/data/outputs.tf` (per `contracts/configuration.md`), plus `writer_role_name`, which is null when Roles Anywhere is disabled (used by T090).
- [X] T090 [US9] Create `infra/pipeline/external_writer.tf`.
  - Read the `data` stack's outputs with `data "terraform_remote_state" "data"`, using the same backend config as the `data` stack (key `<env>/data.tfstate`).
  - Set `local.writer_role_name = try(data.terraform_remote_state.data.outputs.writer_role_name, null)`.
  - With `count = local.writer_role_name == null ? 0 : 1`, attach an inline policy granting `sns:Publish` on the alert topic and ECR pull (`ecr:GetAuthorizationToken`, `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer`) on the pipeline repository.
- [X] T091 [US9] Write `infra/data/README.md`:
  - creating the owner CA with `openssl` (with the key kept offline)
  - issuing a client certificate for a given CN (valid ≤ 1 year)
  - adding the CN to `external_*_subjects` and applying
  - a `~/.pricing/aws-config` template with `credential_process = aws_signing_helper credential-process …`
  - the home-server `docker run` command and a `systemd` timer (or cron) example with `PIPELINE_HOST_LABEL`
  - pausing Fargate with `schedule_enabled=false`
  - rotating certificates
  - revoking via CN removal or `aws rolesanywhere import-crl`
  - keeping the clock synced (NTP)
  - optionally shortening `max_snapshot_age_days` while running off AWS
- [X] T092 [US9] Run `tofu validate` for `infra/data` and `infra/pipeline` with Roles Anywhere enabled and disabled (a dummy CA PEM generated with `openssl` in a temp dir). Also repeat T046's tag-inheritance and `-<env>` naming checks for the resources this story adds.

**Checkpoint**: off-AWS writers and readers work with no long-lived keys, and consumers can't tell who produced the data.

---

## Phase 12: Polish & Cross-Cutting Concerns

- [X] T093 Search the whole repository for leftovers (`_SUCCESS`, `_REGION_COMPLETE`, `.locks`, `partition_markers`, `pricing_parquet_transformations`, `trigger_configured_regions_sensor`) and remove or update every reference outside `specs/001-*`/`specs/002-*`.
- [X] T094 [P] Finish the `README.md` overview: architecture (local, Fargate and home server), the command reference linking `contracts/cli.md`, the configuration reference linking `contracts/configuration.md`, and the consumer contract linking `contracts/storage-layout.md`.
- [X] T095 [P] Add a `CHANGELOG`-style note to `README.md` on the breaking change for consumers: markers are replaced by the manifest (FR-019).
- [X] T096 Run the full `pytest` suite and fix any failures. Run `tofu fmt -check -recursive infra/` and `validate` on all stacks.
- [X] T097 Run quickstart Part A (A1–A9) locally and record the outcomes in `specs/003-pipeline-cloud-deployment/checklists/validation.md`.
- [X] T098 Go through the constitution v1.1.0 review checklist (Development Workflow → "Review checklist", principles I–VII) for the whole change set, and record the results in `specs/003-pipeline-cloud-deployment/checklists/validation.md`.
- [ ] T099 After deployment, run quickstart Parts B and C (B1–B9, C1–C5) and record them in `specs/003-pipeline-cloud-deployment/checklists/validation.md`. SC-001 (4 weeks) and SC-010 are confirmed after the observation period.

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (Phase 1)**: none.
- **Foundational (Phase 2)**: depends on Setup and blocks every story.
- **US2 (Phase 3)**: depends on Foundational. It is the base for every other story, because it provides the runner, the manifest and `latest.json`.
- **US1 (Phase 4)**: depends on US2 (the runner and CLI in the image).
- **US3 (Phase 5)**: depends on US2 (runner hooks). Its infrastructure tasks depend on US1's `infra/pipeline`.
- **US4 (Phase 6)**: depends on US2. It is independent of US1 and US3 (tested locally and on moto).
- **US5 (Phase 7)**: depends on US2. It uses claims (Foundational) for other dates. `lifecycle.tf` depends on US1's `infra/data`.
- **US6 (Phase 8)**: depends on US1 (the stacks exist). `run-pipeline.yml` is most useful after US4 and US5.
- **US7 (Phase 9)**: depends on US2. The Docker parts depend on US1's `Dockerfile`.
- **US8 (Phase 10)**: depends on US2 (manifest, latest, claims).
- **US9 (Phase 11)**: depends on US1 (the stacks and the `Dockerfile`), US3 (the alert topic) and US6 (the multi-arch image published to ECR).
- **Polish (Phase 12)**: depends on all the stories you choose to deliver.

### Story completion order

```text
Setup → Foundational → US2 ─┬─► US1 ─┬─► US3 ──┐
                            │        ├─► US6 ──┼─► US9
                            │        └─────────┘
                            ├─► US4
                            ├─► US5
                            ├─► US7
                            └─► US8
                                                    → Polish
```

### Within each story

- Test tasks marked ⚠️ come first. For transformation, manifest, `latest.json`, retention and backfill they are **required to fail first** (constitution IV).
- Models and pure logic come before the runner and CLI wiring. Infrastructure follows the code it deploys.
- Stop at each checkpoint and validate the story on its own.

---

## Parallel Opportunities

- **Setup**: the fixtures, the fake source, the conftest and the ignore files can be done in parallel once T001–T002 are done.
- **Foundational**: all `[P]` test files (settings, storage, layout, retry, claims) can be written together. After that, `config.py`, `layout.py` and `retry.py` can be implemented in parallel. `storage.py` comes before `claims.py`.
- **US2**: all six test files can be written in parallel. `latest.py` and `verify.py` can be implemented in parallel after `manifest.py`.
- **US1**: the `Dockerfile`, `bootstrap/state`, the envs, `infra/data`, `network.tf` and `ecr.tf` can all be done in parallel. `ecs.tf` comes before `scheduler.tf`.
- **After US2**: US4, US5, US7 and US8 touch mostly different modules. The conflicts are `runner.py` (US4 and US5) and `__main__.py` (US4, US5 and US8). Serialize those edits, or give each story its own branch and merge carefully.

### Parallel example: US2 tests

```bash
Task: "Rewrite tests/unit/test_aws_pricing_transformations.py (staging output, stats, unparseable prices, no markers)"
Task: "Write tests/unit/test_manifest.py (build_revision, status, carry-forward, serialization)"
Task: "Write tests/unit/test_latest_pointer.py (CAS, never backwards)"
Task: "Write tests/unit/test_contract_schemas.py (schema copies + validation)"
Task: "Write tests/integration/test_run_snapshot_local.py and tests/integration/test_run_snapshot_s3.py"
```

### Parallel example: US1 infrastructure

```bash
Task: "Dockerfile"
Task: "infra/bootstrap state stack"
Task: "infra/envs/prod.tfvars + prod.backend.hcl"
Task: "infra/data stack"
Task: "infra/pipeline/network.tf"
Task: "infra/pipeline/ecr.tf"
```

---

## Implementation Strategy

### MVP (P1: US2 → US1 → US3)

1. Setup and Foundational.
2. **US2**: a local end-to-end run with the manifest. Validate with quickstart A1. The contract becomes available to `cloud-pricing-app` here.
3. **US1**: deploy the stacks and push the image manually. Validate with quickstart B3.
4. **US3**: add alerts and the watchdog. Validate with quickstart B4 and B6.
5. **Stop and operate for a week or two.** Unattended weekly snapshots are being preserved, which is the core value.

### Incremental delivery

6. **US4** (repair) and **US5** (retention) before storage grows noticeably. Retention matters within the first 30 days, for raw data.
7. **US6** (CI/CD and OIDC) to replace the manual pushes and applies.
8. **US7** (local parity documentation and tests), **US8** (history upload) and **US9** (home server), as needed.
9. Polish.

---

## Notes

- `[P]` means different files and no dependency on incomplete tasks. `[USn]` maps a task to spec.md.
- Commit after each task or logical group, on branch `003-pipeline-cloud-deployment`.
- Never merge with failing tests (constitution Development Workflow).
- **Resource naming (constitution V)**: every OpenTofu resource with a name or identifier in the `data` and `pipeline` stacks MUST follow `cloud-pricing-<component>-<env>`. This includes resources whose task doesn't spell out a name: the Roles Anywhere trust anchor and profiles (T089), the EventBridge rule (T053), the Scheduler IAM role (T043), and the watchdog IAM role and log group (T055). Only account singletons in `bootstrap` use `cloud-pricing-shared-*`.
- The schema copies in `src/pipeline/schemas/` must stay byte-identical to the spec contracts; T022 (contract schema test) enforces this.
