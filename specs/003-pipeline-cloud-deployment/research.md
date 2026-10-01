# Research: Scheduled Cloud Pricing Pipeline

**Feature**: 003-pipeline-cloud-deployment | **Date**: 2026-09-28

This document resolves the open technical questions from the spec (including the input's "compute size" question) and records each design decision with its rationale and the alternatives rejected.

---

## R1. Compute sizing and task shape

**Measurements (2026-09-28, laptop, snapshot `20260928_100347`)**:

| Measure | Value |
|---|---|
| Full 7-region local run (download + transform, Dagster) | ~3.5 min wall clock (10:03:47 → 10:07:08) |
| Raw per snapshot | 3.3 GB uncompressed JSON, ~1,600 files; largest 481 MB (`AmazonEC2-us-east-1`) |
| Transform, 1 region (us-east-1, 235 files, 559 MB) | 5.9 s, **peak RSS 3.9 GB** |
| Parquet output, 1 region | 47 MB |

**Decision**:
- **Shape**: one ECS Fargate task per run covers all regions.
- **Downloads**: run concurrently across regions (network-bound, low memory).
- **Transforms**: run **one region at a time** (`TRANSFORM_CONCURRENCY=1`), each starting as soon as that region's download finishes.
- **Size**: **ARM64, 1 vCPU / 8 GB, 30 GiB ephemeral storage**.
- **Timeouts**: the in-process run timeout is 120 min, and the run claim's TTL is 180 min (R5).

**Rationale**:
- **Memory**: 3.9 GB peak per transform plus the interpreter, download buffers and pandas overhead fits comfortably in 8 GB. Two parallel transforms would not fit.
- **Disk**: the largest region's raw data (~0.6 GB) plus the compressed copy plus the Parquet staging area is under 2 GB at any time. Uncompressed raw is deleted after each region's transform, and 30 GiB leaves headroom for new services.
- **Runtime**: the estimated 10–15 min on Fargate is well within limits.
- **Cost**: about $0.06/run × 4.3 runs ≈ **$0.30/month** at Fargate ARM prices.
- **Coordination**: one task per run keeps the manifest and claim logic in a single process, with no cross-task coordination.

**Alternatives considered**:
- **One task per region**: it needs cross-task aggregation to write one manifest (a Step Functions or "last one finishes" pattern, the complexity feature 002 introduced) and 7× cold starts, for no cost benefit.
- **Lambda**: the 15-minute hard limit is too close to the expected runtime, and 10 GB of ephemeral storage is tight for new services.
- **AWS Batch**: it adds a compute environment and job queue for no benefit at one job per week.
- **Fargate Spot**: it saves about $0.20/month but risks interrupting a run whose data can't be recovered. Rejected.

## R2. Scheduling and on-demand triggering

**Decision**: **EventBridge Scheduler** runs a cron schedule (`cron(0 13 ? * MON *)`, UTC, configurable) with an ECS `RunTask` target. The target overrides the command to `run --trigger scheduled`. The retry policy allows 2 attempts within 1 hour for `RunTask` API or capacity failures, and failures go to an **SQS dead-letter queue** with an alarm on it.

On-demand runs use a GitHub Actions `workflow_dispatch` workflow (`run-pipeline.yml`) that calls `aws ecs run-task` with a command override. Equivalent documented `aws ecs run-task` commands are available for use from a laptop.

**Rationale**:
- Scheduler has no idle cost, supports time zones and has a native ECS target.
- The workflow lets the owner trigger runs without local AWS credentials, and records who triggered each run.

**Alternatives considered**: EventBridge rules (older, no DLQ per target without extra wiring) and Step Functions (unnecessary without multi-step orchestration).

## R3. Networking

**Decision**: The `pipeline` stack creates a dedicated VPC with 2 **public** subnets, an internet gateway and **no NAT gateway**. Tasks run with `assignPublicIp=ENABLED` and a security group that allows egress only. A free **S3 gateway endpoint** keeps S3 traffic off the internet.

**Rationale**: The task needs outbound HTTPS (pricing API, price-list file URLs, ECR, SNS, CloudWatch Logs). A NAT gateway would cost about $32/month, more than the whole project budget. The task accepts no inbound traffic, and the public IPv4 charge ($0.005/h) applies only while a task runs.

**Alternatives considered**:
- **Default VPC**: it may not exist in a fresh account, and it isn't namespaced by environment.
- **Private subnets with VPC interface endpoints**: about $7/month per endpoint, for ECR, Logs and SNS.

## R4. Storage abstraction (FR-008)

**Decision**: A small `Storage` protocol in `src/pipeline/storage.py` with two implementations, `LocalStorage` (the `os` module) and `S3Storage` (boto3). It is selected by `PIPELINE_STORAGE_URI` (`file:///data` or `s3://bucket/prefix`). Operations:

- `put_file` and `put_bytes`
- `put_if_absent`: `If-None-Match: *`, or `O_CREAT|O_EXCL` locally
- `put_if_match`: `If-Match: <etag>`, or compare-and-replace under `flock` locally
- `get_bytes` and `download_file`
- `head` (size, etag, last_modified)
- `list(prefix)` (recursive)
- `delete`

Keys are always `/`-separated and relative to the root.

Data files are written to a local staging directory first, then uploaded. Size, sha256 and row count are computed from the local file before upload.

**Rationale**:
- Conditional writes are required for the run claim (R5) and for `latest.json` (R7). Neither fsspec/s3fs nor `pyarrow.fs` exposes them uniformly.
- A thin interface is easy to fake and to test with moto.
- Local staging keeps pyarrow writes simple and makes checksums exact.

**Alternatives considered**:
- **fsspec/s3fs**: an extra dependency, and no conditional-write API.
- **`pyarrow.fs`**: no conditional writes.
- **Writing Parquet directly to S3**: it complicates checksum calculation and partial-failure cleanup.

**Local default**: If `PIPELINE_STORAGE_URI` is unset, the root is `file://$DATA_DIRECTORY_ROOT/pipeline` (or `./pipeline`). The new layout therefore never collides with the legacy `pricing_aws/` folders.

## R5. Per-date run claim (FR-005)

**Decision**:
- **Claim object**: `<provider>/claims/<snapshot_date>.json`, containing `{run_id, trigger, acquired_at, expires_at}`.
- **Acquire**: `put_if_absent`. If the claim exists and has not expired, the run is **refused immediately** (exit 0, reason "run already in progress", with an alert if `--trigger scheduled`). If it has expired, it is taken over with `put_if_match` on the old claim's etag, so only one of several racing runs wins.
- **TTL**: `RUN_CLAIM_TTL_MINUTES` (default 180). This is always greater than `RUN_TIMEOUT_MINUTES` (default 120), which the runner enforces with an in-process deadline, and settings validation rejects a TTL ≤ timeout. The timeout is enforced by a watchdog timer, not only by checks between steps: if work blocks (a hung download), the watchdog stops the process with exit 124 (after a best-effort alert), so a run can never outlive its claim and let another run in (PR review, 2026-10-01).
- **Release**: read the claim and its etag, verify our `run_id`, then delete it with `delete_if_match` on that etag (S3 `DeleteObject` with `If-Match`; a version check under `flock` locally). If another run took over the expired claim in between, the delete fails and its claim survives. This closes a race found in PR review (2026-09-30); the earlier read-then-delete could remove another run's claim.
- **Retention scope**: retention on *other* dates tries to take that date's claim and skips the date if it's busy.

**Clock skew**: claim expiry and grace periods mix the runner's clock with S3 `LastModified`, so the runner must have a correct clock. At start-up it compares its clock with the `Date` header of an S3 response. If they differ by more than `MAX_CLOCK_SKEW_SECONDS` (default 300), it refuses to run with exit 2. This matters for off-AWS writers (R22); Fargate clocks are NTP-synced.

**Rationale**:
- S3 conditional writes (`If-None-Match` since Aug 2024, `If-Match` since Nov 2024) give a correct mutex without an extra service.
- The TTL covers crashed or OOM-killed tasks.

**Alternatives considered**:
- **DynamoDB lock table**: an extra resource and IAM, and not portable to a local directory.
- **`flock`** (the current approach): local only, and doesn't work on S3.
- **ECS-level concurrency check (list running tasks)**: races, and doesn't cover local runs.

## R6. Revisions, immutable files and snapshot status (FR-013–FR-016, FR-043)

**Decision**:
- **File naming**: Parquet files are named `part-<run_id>.parquet` inside the Hive-style directory `<table>/snapshot_date=<D>/region=<R>/`. Raw files live under `raw/<D>/<R>/<run_id>/`.
- **Revision files**: each revision is written to `manifests/<D>/revisions/<NNNN>.json` (immutable), and then copied to `manifests/<D>/manifest.json`, the **current** manifest.
- **Carry-forward**: a new revision starts from the current manifest's per-region entries and replaces **only the regions that succeeded in this run**. Regions not re-run, or re-run but failed, keep their previous data entries. The run's own per-region outcomes are recorded under `run.region_results`.
- **Snapshot status** comes from data completeness across `regions.requested`:
  - `succeeded`: every requested region has data.
  - `partial`: at least one requested region has data.
  - `failed`: no requested region has data.
- **Monotonic status**: because data never regresses, a snapshot's status can only improve across revisions. `purged` is the only exception.
- **Requested regions**: the union of the previous revision's requested regions and this run's regions. The first run uses the configured list.

**Rationale**:
- This answers the clarification choice (A): readers of an old revision are never disturbed.
- A failed re-run of a good region can never drop good data, which would otherwise turn it into "superseded" files and delete them.
- Consumers get a simple status meaning: "is the snapshot complete?".

**Alternatives considered**:
- **Status taken from the run's outcome alone**: a failed transform-only re-run would downgrade a good snapshot.
- **A `rev=` directory level**: it pollutes Hive partition discovery with an extra column.

## R7. `latest.json` update (FR-012, FR-014)

**Decision**: After the current manifest is written with status `succeeded`:
1. Read `latest.json` and its etag.
2. If our `snapshot_date` is later than or equal to the one it points to (for an equal date, our revision must be higher), write the new pointer with `put_if_match`. If `latest.json` is missing, write it with `put_if_absent`.
3. On a precondition failure, re-read and retry, up to 5 times.

Locally, the same logic runs under `flock`.

**Rationale**: Runs for different dates hold different claims and can race on `latest.json`. Compare-and-swap guarantees it always names the newest `succeeded` snapshot date, and never moves backwards.

## R8. Raw compression (FR-009)

**Decision**: Use **zstd level 3** via the Python 3.14 stdlib `compression.zstd`, stored as `*.json.zst`. `raw download --decompress` gives plain JSON.

**Rationale**:
- Its ratio is about the same as gzip for this data (roughly 20×).
- It compresses 3.3 GB in seconds, where gzip -6 takes over a minute.
- It adds no dependency.

**Alternatives considered**: gzip (slower, and not much more convenient given the download command) and parquet-only (raw data is required for transform-only runs).

**Fallback**: If the base image has to use Python 3.13 (see R17), use the `zstandard` package with the same file format.

## R9. Download retries (FR-047)

**Decision**:
- **Level**: retries happen at the **per-file level** inside a region, which is finer-grained than re-downloading the whole region and matches "retry that region's download".
- **Policy**: `RetryPolicy` (`src/pipeline/retry.py`) builds its settings from `PRICING_DOWNLOAD_RETRY_MAX_RETRIES` (default 3), `PRICING_DOWNLOAD_RETRY_STRATEGY` (`fixed` | `linear_backoff` | `exponential_backoff`, default `exponential_backoff`) and `PRICING_DOWNLOAD_RETRY_BASE_DELAY_SECONDS` (default 30).
- **Delay before retry n (n ≥ 1)**:
  - `fixed`: `base`
  - `linear_backoff`: `base × n`
  - `exponential_backoff`: `base × 2^(n−1)`
- **Retryable errors**: `httpx.TimeoutException`, connection errors, HTTP 429/5xx, a truncated or empty download, and botocore throttling on `get_price_list_file_url`. Truncated or empty downloads are detected cheaply at download time: a Content-Length mismatch, an empty body, or a last non-whitespace byte other than `}`. A file that passes this check but then fails to parse at transform time fails the region **without** a download retry, because parsing about 480 MB twice to validate is too costly. The region's reason records the parse error, and the owner re-runs the region (FR-004).
- **Permanent errors (never retried)**: HTTP 4xx other than 429, AccessDenied, an invalid region, and `ValidationException`.
- **Region outcome**: a region **fails** if any service that has a price list still fails after its retries. "No price list available in region" is **not** a failure; it is counted in the manifest as `services_without_price_list`.
- **Reporting**: the region result records `download_attempts_max` and its last error.
- **Throttling of the Price List API** (`ListPriceLists`, `GetPriceListFileUrl`) is part of downloading a service's price list, so it is retried per the same policy once botocore's own retries give up. Found in the first real run (2026-09-28): with regions downloading in parallel, `ListPriceLists` throttled 17 of 272 services, which the old code had silently skipped.
- **SDK retries**: boto3 clients use botocore's `adaptive` retry mode (client-side rate limiting, 5 attempts). This is SDK configuration, not a setting, so the `pricing_download_retry_*` names stay specific to pricing downloads, as the clarification required.

**Rationale**:
- Today one timed-out service file is silently dropped while the region still counts as "success", so the snapshot data may be incomplete.
- Retrying at the file level and failing the region otherwise makes completeness trustworthy.

## R10. Retention (FR-020–FR-024, FR-045, FR-046, FR-048–FR-050)

**Decision**: The retention step runs as the last phase of `runner.run_snapshot` (and via `retention` on demand). It works in this order:

1. **Build the active set**: list the current `manifest.json` of every date and collect the paths of files listed by manifests whose status isn't `purged`.
2. **Superseded and orphaned files for the run's own date** (the claim is already held): Parquet keys under that date's prefix that aren't in the active set. A superseded file (listed by an earlier revision) is eligible when `now ≥ current_manifest.created_at + grace`; the superseding revision is always written after the file, so the file's own timestamp adds nothing. An orphan (never listed by any revision, e.g. left by a failed run) is eligible when `now ≥ file.last_modified + grace`.
   - If the grace period is ≤ `SUPERSEDED_FILE_INLINE_WAIT_MAX_MINUTES`, the step sleeps for the remaining time first.
   - Otherwise it skips those files, and a later run's retention step deletes them.
3. **Other dates**: try to take each date's claim (skip the date if it's busy), then apply the same superseded rule, without waiting.
4. **Thinning**: among snapshots older than `PARQUET_WEEKLY_RETENTION_MONTHS` (default 12), group by the snapshot date's UTC calendar month. Keep the earliest snapshot with status `succeeded` and purge the rest. Never purge the date `latest.json` names.
5. **Purge a date**: rewrite `manifest.json` as a new revision with `status=purged`, `purged_at` and an empty `tables`, and append it to `revisions/`. Only then delete the date's Parquet files.
6. **Guard (FR-046)**: immediately before each delete, check the key against a *freshly re-read* active set for that date (re-read `manifest.json`). If the key is now referenced, skip it and log.
7. **Raw data**:
   - **S3**: an **S3 lifecycle rule** expires `<provider>/raw/` after `RAW_RETENTION_DAYS` days, one rule per provider prefix, generated from the `pricing_providers` variable (`providers` is a reserved variable name in Terraform/OpenTofu).
   - **Local**: the retention step deletes raw run folders older than `RAW_RETENTION_DAYS`, based on the `raw_stored_at` recorded in manifests or the folder mtime. Raw files are grouped by snapshot date and deleted under that date's claim, so dates another run is using are skipped (PR review, 2026-09-30).
8. **Dry run**: compute the same plan, log it, emit it as JSON with `--dry-run --output plan.json`, and skip every write and delete.

**Failure handling**: A retention failure is caught. It doesn't change the snapshot status or `latest.json`, is added to the run report as `retention_error`, and sends an alert (FR-050).

**Rationale**:
- Lifecycle rules handle raw data for free and without code.
- Everything that depends on manifest state (superseded files, thinning) has to be code.
- Doing the manifest-first purge and re-checking the active set before each delete guarantees SC-006.

**Alternatives considered**:
- **S3 lifecycle for Parquet**: it can't express "earliest succeeded per month".
- **Deleting by file age only**: it would delete shared, carried-forward files.

## R11. Missed-run detection ("dead man's switch", FR-027)

**Decision**: A small **Lambda** (`src/watchdog/freshness_check.py`, Python 3.13, stdlib + boto3) is invoked **daily at 14:00 UTC** by EventBridge Scheduler.
- It reads `aws/manifests/latest.json`. If it's missing, or `today − snapshot_date > MAX_SNAPSHOT_AGE_DAYS` (default 8), it publishes a missed-run alert to SNS.
- A CloudWatch alarm on the Lambda's `Errors` metric also alerts, so a broken watchdog is noticed.

**Rationale**:
- It runs independently of the pipeline image, so it still works when the image is broken.
- A daily check meets SC-003 (the window + ≤ 1 day).
- It costs effectively $0 (free tier).

**Alternatives considered**:
- **CloudWatch alarm on a custom "snapshot succeeded" metric with `treat_missing_data=breaching`**: alarm evaluation windows are limited to 7 days for 1-hour-or-longer periods (verify during implementation), so an 8-day window can't be expressed directly.
- **Daily Fargate check task**: it depends on the same image and costs more.

## R12. Alerting and exit codes (FR-026, FR-028, FR-050)

**Decision**: There is a single SNS topic, `pricing-pipeline-alerts-<env>`, with an email subscription. The address comes from the `alert_email` variable, supplied through a GitHub Actions secret, so it's never committed. Alerts come from four sources:

| Source | Trigger | Message |
|---|---|---|
| Container (`notify.py`) | a run ends with any region failure, snapshot `partial`/`failed`, a refused *scheduled* run, a retention error, or an optional success summary | structured subject and body (see `contracts/configuration.md`) |
| Container top-level handler (`__main__`) | an unhandled exception on any host, best effort before exiting non-zero (FR-057) | `RUN CRASHED` with traceback summary |
| EventBridge rule on `ECS Task State Change` | `lastStatus=STOPPED` and (`containers[].exitCode != 0` **or** `stopCode=TaskFailedToStart`) for the pipeline task family | crash, OOM (exit 137) or start failure |
| CloudWatch alarm | the Scheduler DLQ has messages | the schedule couldn't launch the task |
| Lambda watchdog, plus an alarm on its errors | stale `latest.json` | missed run |

**Exit codes**: `0` means the run completed and reported its outcome, whatever the status. A partial run is still exit 0: it is reported by the container, so it isn't alerted twice. `2` means a configuration or usage error. Any other non-zero code is an unhandled crash, which the ECS rule catches.

**Local mode**: if `ALERT_TOPIC_ARN` is unset, alerts are only logged.

**Rationale**: The container alerts with rich context. The platform-level rules catch everything the container can't report itself (crash, never started). Together they meet SC-002's 15-minute target with a large margin.

**One-time manual step**: SNS email subscriptions need the recipient to click a confirmation link. This is the only non-code step, and the quickstart documents it.

## R13. Infrastructure layout and state (FR-031–FR-033, SC-008)

**Decision**: There are three OpenTofu root modules, each with separate state in an S3 backend with **native S3 locking** (`use_lockfile = true`, OpenTofu ≥ 1.10, no DynamoDB):

- **`infra/bootstrap`**: applied once from a laptop, with local state migrated into the bucket it creates. It creates:
  - **Account singletons** (the constitution V exception), named `cloud-pricing-shared-*` and tagged `environment=shared`:
    - The state bucket (owner-chosen name via `state_bucket_name`), versioned and encrypted.
    - The GitHub OIDC provider.
    - An **AWS Budget** (`cloud-pricing-shared-budget`) covering the whole account: a warning at $15 and an alert at $25 of actual spend per month, plus a forecasted alert at $25.
  - **Per-environment** CI roles, one set per entry in the `environments` variable (default `["prod"]`):
    - `cloud-pricing-gha-plan-<env>`: read-only, plus state lock files under `<env>/`, for PRs.
    - `cloud-pricing-gha-apply-<env>`: trusted only for `repo:<repo>:environment:<env>`, and scoped to `cloud-pricing-*-<env>` resources.
    - `cloud-pricing-gha-run-<env>`: `ecs:RunTask` / `iam:PassRole` on that environment's pipeline task only.
- **`infra/data`**: the data bucket, its lifecycle rules, bucket policy and a read-only IAM policy for the web app. The **bucket** has `lifecycle { prevent_destroy = true }`. The off-AWS access resources (R22) also live here but aren't protected, so they can be added or removed through variables.
- **`infra/pipeline`**: everything else. It looks up the data bucket by name and can be destroyed and re-created freely (SC-008).

**Environments**:
- Resource names follow `cloud-pricing-<component>-<env>`.
- Each stack is applied with `-var-file=../envs/<env>.tfvars -backend-config=../envs/<env>.backend.hcl`, and the state key is `<env>/<stack>.tfstate`.
- Only `prod.*` files are provided now. `dev`/`qa` need only new tfvars and backend files.

**Tagging**: every stack sets provider `default_tags` of `project=cloud-pricing` and `component=bootstrap|data|pipeline`, plus `environment=<env>` for `data` and `pipeline`, or `environment=shared` for `bootstrap` (account singletons, constitution V v1.1.1), so pipeline cost can be isolated in Cost Explorer (SC-007). This requires activating these as cost allocation tags once.

**Rationale**:
- It separates lifecycles cleanly.
- Standard `-backend-config` works in both OpenTofu and Terraform, avoiding OpenTofu-only early variable evaluation.

**Alternatives considered**: a single stack (can't destroy the pipeline without the bucket) and Terragrunt (an extra tool).

## R14. CI/CD (FR-034)

**Decision**:
- **`ci.yml`** (pull requests):
  - `pytest`.
  - `docker build` for arm64 (no push).
  - `tofu fmt -check` and `validate` for all stacks.
  - `tofu plan` for `data` and `pipeline` using the `cloud-pricing-gha-plan-prod` OIDC role, posted as a PR comment.
- **`deploy.yml`** (release-based, decided 2026-09-30): runs on a pushed `vX.Y.Z` tag whose commit is on `main`, or manually from `main` naming an existing tag (redeploy or rollback). Merges to `main` don't deploy; `ci.yml` also runs on pushes to `main`. The GitHub `prod` environment restricts deployments to `main` and `v*` refs. The OIDC subject template includes the ref (`["repo", "context", "ref"]`, immutable format with owner and repo IDs, decided 2026-09-30), so the AWS roles check it too: apply accepts only the `prod` environment with a `v*` tag or `main`, plan only pull requests, run only `main`. The repo is public, so role ARNs are secrets and logins use `mask-aws-account-id`. Steps:
  - Tests.
  - Build and push a **multi-arch** image (`linux/arm64` for Fargate, `linux/amd64` for typical home servers) to ECR, tagged with the release version (`vX.Y.Z`) and `sha-<commit>`. The ECR tags are immutable (a redeploy reuses the existing image), a lifecycle rule keeps the last 10 images, and scan-on-push is enabled.
  - `tofu apply` of `data` then `pipeline` with `-var image_tag=vX.Y.Z`, in a job bound to GitHub Environment **`prod`** with required reviewers (the manual approval).
- **`run-pipeline.yml`** (`workflow_dispatch`): inputs are `mode` (`run` | `transform-only` | `retention`), `snapshot_date`, `regions` and `dry_run`. It assumes `cloud-pricing-gha-run-prod` and calls `aws ecs run-task`.
- **Runner**: the build uses buildx. The arm64 leg runs natively on `ubuntu-24.04-arm` (or under QEMU if that's unavailable), and the amd64 leg on `ubuntu-24.04`. The two are merged into one manifest list.

**Rationale**:
- Immutable version tags make every deploy traceable. The manifest's `pipeline_version` records the version tag and the commit.
- Approval gates both infrastructure changes and new images.

## R15. Data bucket settings (FR-011, FR-012)

**Decision**:
- **Access**: Block Public Access (all 4 settings), a bucket policy that denies non-TLS requests, and `BucketOwnerEnforced` object ownership.
- **Encryption**: SSE-S3 (SSE-KMS adds per-request cost and key cost for no gain here).
- **Versioning**: enabled, with **noncurrent versions expiring after 7 days**. This is a safety net against accidental deletes of irreplaceable history, at negligible cost.
- **Naming**: bucket names are chosen by the owner (`data_bucket_name`, `state_bucket_name`) and never contain the AWS account ID (decided 2026-09-29). Account IDs aren't secret, but there's no need to expose one in S3 hostnames, logs and app configuration. A generated random suffix was rejected as unnecessary automation.
- **Web app access**: the managed IAM policy `cloud-pricing-data-read-<env>` (`s3:GetObject` and `s3:ListBucket` on the data prefixes), output for the app feature to attach.

## R16. Dagster's role (FR-007)

**Decision**: Dagster stays as an optional local runner (`requirements-dagster.txt`, not in the image). It becomes one job, `pricing_snapshot`, with one op that calls `src.pipeline.runner.run_snapshot(...)`. The job's config offers `snapshot_date`, `regions` and `mode`, and `pricing_weekly_schedule` is kept for local use.

The per-region fan-out, the `trigger_configured_regions_sensor` and the marker handling are removed. A per-region fan-out would be refused by the per-date run claim anyway, and the Launchpad now covers on-demand runs.

## R17. Container image

**Decision**: The image is built on `python:3.14-slim` and runs as a non-root user.
- **Dependencies**: `requirements.txt` only.
- **Build args**: `GIT_SHA` and `IMAGE_TAG`, exposed as environment variables for manifest provenance.
- **Command**: `ENTRYPOINT ["python","-m","src.pipeline"]`, with default `CMD ["run","--trigger","scheduled"]`.
- **`.dockerignore`**: excludes `venv/`, `specs/`, `tests/`, `infra/` and data.
- **Off-AWS credentials**: the image includes AWS's `aws_signing_helper` binary (the one for the image's architecture, pinned version, checksum-verified). An off-AWS host only has to mount its certificate, key and an AWS config file that uses `credential_process` (R22).
- **Host label**: `PIPELINE_HOST_LABEL`, which the task definition sets to `aws-ecs`.
- **Local use**: `docker run -v "$DATA:/data" -e PIPELINE_STORAGE_URI=file:///data …`.

**Verify at implementation**: pyarrow and pandas publish `cp314` manylinux aarch64 wheels. The current venv already runs 3.14 on macOS. If they don't, pin `python:3.13-slim` and use `zstandard` (R8).

## R18. History upload script (FR-038–FR-042)

**Decision**: The command is `python -m src.pipeline upload-history --snapshot-date D [--overwrite] [--source <path>]`.

- **Source layouts**:
  - The new local layout (a local-directory run after this feature). Its `manifest.json` is used as-is.
  - The legacy layout `$DATA_DIRECTORY_ROOT/pricing_aws/parquet/<table>/snapshot_date=D/region=R/*.parquet`, which has no manifest. The script builds one:
    - `origin=backfill` and `run.trigger=backfill`
    - `regions.requested` and `regions.succeeded` set to the regions found
    - `status=succeeded`
    - `raw=null`
    - a new run_id
    - sha256, bytes and row count (from Parquet metadata) for each file
    - `pipeline_version` set to the current code version
- **Upload**: files are re-keyed to the standard layout with `part-<run_id>.parquet`.
- **Claim**: the script takes the date's run claim.
- **Existing data**: without `--overwrite`, the script aborts before any write if anything exists under the date's `parquet/…/snapshot_date=D/` or `manifests/D/` prefixes. With `--overwrite`, it publishes a new revision that supersedes everything, so previous files become superseded and are cleaned up by retention after the grace period.
- **Order and pointer**: data files are uploaded, then the revision and manifest, then `latest.json` is updated (R7).
- **Never raw**: raw files are never read or uploaded.

## R19. Snapshot date and run identity

**Decision**:
- **`snapshot_date`**: the UTC date at run start, overridable with `--snapshot-date`.
- **`run_id`**: `<UTC yyyymmddThhmmssZ>-<6 hex>`, which is sortable and unique.
- **`revision`**: the maximum existing revision number for that date plus 1, computed while holding the claim.

## R20. Cost estimate (SC-007)

These are `us-east-1` list prices at the current scale.

| Item | Monthly |
|---|---|
| Fargate ARM 1 vCPU/8 GB, ~15 min × 4.3 runs | ~$0.30 |
| S3 Parquet (~7.5 GB after year 1, then +~1.7 GB/yr) + raw (~0.8 GB rolling) + noncurrent versions | ~$0.25 (year 1 average lower) |
| S3 requests (~10k PUT/LIST per run) | ~$0.25 |
| ECR (≤10 images × ~250 MB) | ~$0.25 |
| CloudWatch Logs (30-day retention, small) | <$0.05 |
| CloudWatch alarms (3 × $0.10) | $0.30 |
| EventBridge Scheduler, SNS email, Lambda, SQS, Budgets (2 free), IAM Roles Anywhere (no charge with your own CA) | ~$0 |
| S3 data transfer out to off-AWS readers and writers (about 0.3 GB/week; 100 GB/month free) | $0 |
| Public IPv4 while a task runs | <$0.01 |
| **Total** | **≈ $1.40 (< $3 target)** |

## R21. Testing approach

**Decision**:
- **Storage**: the same test suite is parametrized over `LocalStorage` (tmp_path) and `S3Storage` on **moto**.
- **Runner integration tests**: they inject a fake downloader that writes fixture JSON per region. Its behavior is configurable per region (succeed, fail permanently, fail N times then succeed), so partial, re-run and retry scenarios run offline.
- **Retention**: tests seed manifests spanning 15 months.
- **Verify at implementation**: moto's support for `IfNoneMatch` and `IfMatch` on `put_object`. If it isn't supported, cover the claim and `latest.json` compare-and-swap with a fake conditional store for S3 semantics, plus a real-S3 smoke test in the quickstart.

## R22. Running outside AWS: IAM Roles Anywhere (FR-051–FR-057)

**Decision**:
- **Certificate authority**: an **owner-managed private CA**. It is a self-signed root created once with `openssl`, and its private key is kept offline, not in the repo or AWS. Its **public** certificate is registered as a Roles Anywhere **trust anchor** (`source_type = CERTIFICATE_BUNDLE`) in the `data` stack, and only when `roles_anywhere_ca_bundle_pem` is set.
- **Two roles**, each with its own Roles Anywhere **profile** (session length `external_session_duration_seconds`, default 1 h):
  - **`cloud-pricing-pipeline-writer-<env>`**:
    - S3 `Get`/`Put`/`Delete`/`List` on `<provider>/*`, including conditional puts.
    - The Pricing API actions (`pricing:DescribeServices`, `GetProducts`, `ListPriceLists`, `GetPriceListFileUrl`) and `ec2:DescribeRegions`.
    - `sns:Publish` and ECR pull. These are attached by the `pipeline` stack, which owns the topic and repository. The pipeline stack looks up the role by name, and skips the attachment if the role doesn't exist.
  - **`cloud-pricing-data-reader-<env>`**: attaches the same `cloud-pricing-data-read-<env>` policy that the web app uses inside AWS.
- **Trust policy**: the principal is `rolesanywhere.amazonaws.com`, with actions `sts:AssumeRole`, `sts:TagSession` and `sts:SetSourceIdentity`. Conditions:
  - `aws:SourceArn` must equal the trust anchor.
  - `aws:PrincipalTag/x509Subject/CN` must be in `external_writer_subjects` or `external_reader_subjects`.
  - If the subject list is empty, the role gets a deny-all trust policy.
- **Revoking a machine**: remove its CN and apply (immediate), or import a CRL with `aws rolesanywhere import-crl`, which the quickstart documents.
- **Certificate lifetime**: client certificates are valid for at most 1 year, with the rotation steps in `infra/data/README.md`.
- **Getting credentials on the machine**: an `~/.aws/config` profile with `credential_process = aws_signing_helper credential-process --certificate … --private-key … --trust-anchor-arn … --profile-arn … --role-arn …`. boto3 refreshes these credentials automatically before they expire, so a 1 h session covers runs of any length.
- **In Docker**: mount the certificate, key and config read-only and set `AWS_CONFIG_FILE` and `AWS_PROFILE`. The helper binary is already in the image (R17).
- **Scheduling the home run**: the Dagster schedule (R16) or a cron/systemd timer that calls `docker run … run --trigger scheduled`. Set `PIPELINE_HOST_LABEL` (e.g., `home-server`) so `run.host` in the manifest records where the run executed.
- **Switching writers**: `schedule_enabled=false` pauses Fargate runs and keeps everything else deployed (FR-051). The run claim protects against overlap if both writers are enabled at once.
- **Alerting parity**: the container's own alerts work anywhere the writer role can publish to SNS, and the top-level crash handler adds `RUN CRASHED` (FR-057). A hard kill (OOM, power loss) off AWS is caught only by the missed-run watchdog. Operators can lower `max_snapshot_age_days` while running off AWS.

**Rationale**:
- Short-lived credentials, per-machine identity and per-machine revocation, with no long-lived keys, and no AWS cost when you run your own CA.
- The consumer contract is untouched, so readers don't care where snapshots come from (FR-056).

**Alternatives considered**:
- **An IAM user with access keys**: long-lived secrets on home machines. Rejected by the clarification.
- **AWS Private CA as the trust anchor**: about $400/month (general-purpose mode) or $50/month (short-lived mode), which breaks the budget.
- **Presigned URLs for readers**: the app would need a broker, and they don't support listing or reading manifests flexibly.
- **SSM hybrid activations (managed-instance credentials)**: heavier agent setup, and meant for fleet management rather than application identity.

