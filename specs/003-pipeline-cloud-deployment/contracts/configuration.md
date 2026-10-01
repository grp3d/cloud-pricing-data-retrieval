# Contract: Configuration

All settings come from configuration and are never hard-coded (FR-035). The container reads **environment variables**, which the `pipeline` stack sets from **OpenTofu variables** in the ECS task definition. Locally, you export the same variables. Settings are validated at start-up, and an invalid value exits with code 2.

## Container / CLI environment variables

| Variable | Default | Validation | Spec |
|---|---|---|---|
| `PIPELINE_STORAGE_URI` | `file://$DATA_DIRECTORY_ROOT/pipeline` (else `file://./pipeline`) | Must be `file://` or `s3://`. | FR-008 |
| `PIPELINE_PROVIDER` | `aws` | `[a-z0-9]+` | FR-010 |
| `PIPELINE_ENVIRONMENT` | `local` (the task definition sets the stack's environment, e.g. `prod`) | `[a-z0-9-]{1,32}`. Used in alert subjects. | FR-026 |
| `PRICING_REGIONS` | the 7 defaults in `src/aws_regions.py` | A comma-separated list of region codes. | FR-003 |
| `MAX_RAW_DOWNLOAD_WORKERS` | `2` | ≥ 1. Concurrent file downloads per region. | |
| `TRANSFORM_CONCURRENCY` | `1` | ≥ 1. Parallel region transforms (memory-bound, see research R1). | |
| `PRICING_DOWNLOAD_RETRY_MAX_RETRIES` | `3` | ≥ 0 (0 disables retries). | FR-047 |
| `PRICING_DOWNLOAD_RETRY_STRATEGY` | `exponential_backoff` | `fixed` \| `linear_backoff` \| `exponential_backoff` | FR-047 |
| `PRICING_DOWNLOAD_RETRY_BASE_DELAY_SECONDS` | `30` | ≥ 0 | FR-047 |
| `RUN_TIMEOUT_MINUTES` | `120` | ≥ 1. A hard limit: a watchdog stops the process (exit 124) even if work is blocked. | FR-005 |
| `RUN_CLAIM_TTL_MINUTES` | `180` | Must be greater than `RUN_TIMEOUT_MINUTES`. | FR-005 |
| `RAW_RETENTION_DAYS` | `30` | ≥ 1. Must match the lifecycle rule in S3 (the same OpenTofu variable feeds both). | FR-020 |
| `PARQUET_WEEKLY_RETENTION_MONTHS` | `12` | ≥ 1 | FR-021 |
| `SUPERSEDED_FILE_GRACE_MINUTES` | `5` | ≥ 0 | FR-045 |
| `SUPERSEDED_FILE_INLINE_WAIT_MAX_MINUTES` | `5` | ≥ 0 | FR-049 |
| `ALERT_TOPIC_ARN` | unset → alerts are only logged | An SNS ARN. | FR-026 |
| `SUCCESS_SUMMARY_ENABLED` | `false` | Boolean. | FR-028 |
| `PIPELINE_HOST_LABEL` | `local` (the task definition sets `aws-ecs`) | `[a-z0-9-]{1,63}`. Recorded as `run.host`. | FR-052 |
| `MAX_CLOCK_SKEW_SECONDS` | `300` | ≥ 0. The run refuses to start if the local clock differs from the storage service's clock by more than this. Only applies to `s3://` roots. | Spec edge cases |
| `GIT_SHA`, `IMAGE_TAG` | `local` | Baked into the image at build time. | Manifest provenance |
| `DATA_DIRECTORY_ROOT` | unset | Existing variable. Sets the default local root and the `upload-history --source`. | |

The ad hoc CLI (`src.aws_pricing_cli`) keeps using `DATA_DIRECTORY_ROOT` only.

## Watchdog Lambda environment variables

| Variable | Default | Notes |
|---|---|---|
| `DATA_BUCKET` | from stack | |
| `DATA_PREFIX` | `""` | The root prefix inside the bucket. |
| `PROVIDERS` | `aws` | Comma-separated. One `latest.json` is checked per provider. |
| `MAX_SNAPSHOT_AGE_DAYS` | `8` | FR-027 |
| `ALERT_TOPIC_ARN` | from stack | |
| `ENVIRONMENT` | from stack | Used in the alert subject. |

## OpenTofu variables

### `infra/bootstrap`

| Variable | Default                              | Notes |
|---|--------------------------------------|---|
| `aws_region` | `us-east-1`                          | |
| `github_repository` | required                             | `owner/cloud-pricing-data-retrieval`. Scopes the OIDC trust. |
| `state_bucket_name` | required                             | An account singleton (constitution V), tagged `environment=shared`. Owner-chosen; never contains the account ID. |
| `environments` | `["prod"]`                           | Creates one set of CI roles per environment: `cloud-pricing-gha-{plan,apply,run}-<env>`. |
| `budget_warning_usd` / `budget_alert_usd` | `10` / `20`                          | FR-030 |
| `budget_email` | required (a secret, never committed) | |

### `infra/data` and `infra/pipeline` (shared via `infra/envs/<env>.tfvars`)

| Variable | Default | Stack | Notes |
|---|---|---|---|
| `environment` | required (`dev`\|`qa`\|`prod`) | both | Namespaces every resource: `cloud-pricing-<component>-<env>` (FR-032). |
| `aws_region` | `us-east-1` | both | |
| `data_bucket_name` | required (prod: `cloud-pricing-data-prod-g08a9i`) | both | Owner-chosen, never contains the account ID. Keep the `cloud-pricing-data-<env>-` prefix, which the CI deploy role's permissions match. |
| `pricing_providers` | `["aws"]` | both | One raw lifecycle rule per provider. Also passed to the watchdog. |
| `raw_retention_days` | `30` | both | Feeds the S3 lifecycle rule and `RAW_RETENTION_DAYS`. |
| `noncurrent_version_retention_days` | `7` | data | A safety net for accidental deletes. |
| `roles_anywhere_ca_bundle_pem` | `""` | data | The **public** certificate(s) of the owner's CA, PEM. If empty, no Roles Anywhere resources are created. |
| `external_writer_subjects` | `[]` | data | Certificate subject CNs allowed to assume `cloud-pricing-pipeline-writer-<env>` (e.g., `["home-server"]`). |
| `external_reader_subjects` | `[]` | data | CNs allowed to assume `cloud-pricing-data-reader-<env>`. |
| `external_session_duration_seconds` | `3600` | data | The credential lifetime. The signing helper refreshes credentials automatically for longer runs. |
| `image_tag` | required | pipeline | The git SHA, passed in by CI. |
| `pricing_regions` | the 7 defaults | pipeline | |
| `schedule_expression` | `cron(0 13 ? * MON *)` | pipeline | Evaluated in UTC (FR-002). |
| `schedule_enabled` | `true` | pipeline | `false` pauses cloud runs while keeping all other infrastructure (FR-051), e.g., while a home server is the writer, or to test the missed-run alert (SC-003). |
| `task_cpu` / `task_memory` / `ephemeral_storage_gib` | `1024` / `8192` / `30` | pipeline | Research R1. |
| `run_timeout_minutes` / `run_claim_ttl_minutes` | `120` / `180` | pipeline | |
| `pricing_download_retry_max_retries` / `_strategy` / `_base_delay_seconds` | `3` / `exponential_backoff` / `30` | pipeline | |
| `parquet_weekly_retention_months` | `12` | pipeline | |
| `superseded_file_grace_minutes` / `superseded_file_inline_wait_max_minutes` | `5` / `5` | pipeline | |
| `success_summary_enabled` | `false` | pipeline | |
| `alert_email` | required (the GitHub secret `TF_VAR_alert_email`) | pipeline | Needs a one-time click on the SNS confirmation link. |
| `max_snapshot_age_days` | `8` | pipeline | Watchdog window. |
| `log_retention_days` | `30` | pipeline | FR-029 |
| `state_bucket_name` | required | pipeline | The bucket in `envs/<env>.backend.hcl`. Used to read the data stack's outputs (the off-AWS writer role). |

**Outputs used by other repos and workflows**:
- **`data`**: `data_bucket_name` and `data_read_policy_arn`, for the web app. It also outputs `roles_anywhere_trust_anchor_arn`, plus `writer_role_arn` / `writer_profile_arn` and `reader_role_arn` / `reader_profile_arn`, for off-AWS machines.
- **`pipeline`**: `ecs_cluster_arn`, `task_definition_family`, `subnet_ids` and `security_group_id`, used by `run-pipeline.yml` and the documented `aws ecs run-task` commands.

## Alert messages (SNS, email)

The subject format is `[cloud-pricing <env>] <KIND>: <provider> <snapshot_date>`. The body is plain text with the RunReport fields, followed by the RunReport as JSON.

| KIND | Sent by | When |
|---|---|---|
| `RUN FAILED` | container | No requested region has data after the run (`status=failed`). |
| `RUN PARTIAL` | container | The snapshot is `partial`, or any region in this run failed. |
| `RUN REFUSED` | container | A scheduled run was refused because another run holds the claim. |
| `RETENTION ERROR` | container | The retention step raised an error (FR-050). |
| `RUN SUCCEEDED` | container | Only if `SUCCESS_SUMMARY_ENABLED`. Includes regions, row counts per table and duration. |
| `RUN CRASHED` | container (best effort) | An unhandled exception, on any host (FR-057). |
| `TASK CRASHED` | EventBridge rule | The ECS task stopped with a non-zero exit, an OOM, or `TaskFailedToStart`. |
| `SCHEDULE FAILED` | CloudWatch alarm | The Scheduler DLQ has one or more messages. |
| `MISSED RUN` | watchdog Lambda | `latest.json` is missing or older than `MAX_SNAPSHOT_AGE_DAYS`. |
| `WATCHDOG ERROR` | CloudWatch alarm | The watchdog Lambda has errors. |
