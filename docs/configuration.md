# Configuration reference

Nothing is hard-coded: the pipeline reads **environment variables**, validated at start-up (an
invalid value exits 2). In the cloud, the `pipeline` stack sets them in each provider's ECS task
definition from **OpenTofu variables**; locally, you export them or use a `.env` file.

## Pipeline (container / CLI)

### General

| Variable | Default | Notes |
|---|---|---|
| `PIPELINE_PROVIDER` | `aws` | `aws`, `azure` or `gcp`. |
| `PIPELINE_STORAGE_URI` | `file://$DATA_DIRECTORY_ROOT/pipeline` (else `file://./pipeline`) | `file://…` or `s3://bucket[/prefix]`. |
| `PIPELINE_ENVIRONMENT` | `local` | `[a-z0-9-]{1,32}`; used in alert subjects. The cloud sets e.g. `prod`. |
| `PIPELINE_HOST_LABEL` | `local` | Recorded as `run.host`. The cloud sets `aws-ecs`. |
| `PRICING_REGIONS` | the provider's default locations (below) | Comma-separated locations. For Azure and GCP, `global` is always added and may not be listed. |
| `DATA_DIRECTORY_ROOT` | unset | Default local root, and the default `upload-history --source`. |
| `GIT_SHA`, `IMAGE_TAG` | `local` | Baked into the image at build time; recorded in manifests. |

Default locations:

| Provider | Locations |
|---|---|
| `aws` | `us-east-1`, `us-east-2`, `us-west-1`, `us-west-2`, `eu-west-1`, `eu-west-2`, `ap-northeast-1` |
| `azure` | `eastus`, `eastus2`, `westus`, `westus2`, `westeurope`, `uksouth`, `japaneast`, `north-america`, `europe`, `asia`, `intercontinental` (+ `global`) |
| `gcp` | `us-east1`, `us-east4`, `us-west1`, `us-west2`, `europe-west1`, `europe-west2`, `asia-northeast1` (+ `global`) |

### Downloads, retries and timeouts

| Variable | Default | Notes |
|---|---|---|
| `PRICING_DOWNLOAD_RETRY_MAX_RETRIES` | `3` | `0` disables retries. Azure and GCP resume at the failed page. |
| `PRICING_DOWNLOAD_RETRY_STRATEGY` | `exponential_backoff` | `fixed`, `linear_backoff` or `exponential_backoff`. |
| `PRICING_DOWNLOAD_RETRY_BASE_DELAY_SECONDS` | `30` | |
| `PRICING_DOWNLOAD_RATE_LIMIT_PER_SECOND` | Azure `0.5`, GCP `1.0`, AWS unset | Maximum request rate to the source. |
| `PRICING_DOWNLOAD_MAX_CONCURRENT_REQUESTS` | `1` | Azure and GCP. |
| `MAX_RAW_DOWNLOAD_WORKERS` | `2` | AWS: concurrent file downloads per region. |
| `TRANSFORM_CONCURRENCY` | `1` | Parallel location transforms (memory-bound). |
| `RUN_TIMEOUT_MINUTES` | `120` | Hard limit; the process exits 124. |
| `RUN_CLAIM_TTL_MINUTES` | `180` | Must be greater than `RUN_TIMEOUT_MINUTES`. |
| `MAX_CLOCK_SKEW_SECONDS` | `300` | `s3://` roots only: refuse to start if the local clock is off by more. |

### Retention

| Variable | Default | Notes |
|---|---|---|
| `RAW_RETENTION_DAYS` | `30` | Must match the S3 lifecycle rule (the same OpenTofu variable sets both). |
| `PARQUET_WEEKLY_RETENTION_MONTHS` | `12` | After this, one snapshot per month is kept. |
| `SUPERSEDED_FILE_GRACE_MINUTES` | `5` | How long superseded files stay for readers holding an old manifest. |
| `SUPERSEDED_FILE_INLINE_WAIT_MAX_MINUTES` | `5` | How long a run waits for that grace period before leaving cleanup to the next run. |

### Alerts

| Variable | Default | Notes |
|---|---|---|
| `ALERT_TOPIC_ARN` | unset (alerts are only logged) | SNS topic ARN. |
| `SUCCESS_SUMMARY_ENABLED` | `false` | Also send `RUN SUCCEEDED`. |

### Azure

| Variable | Default | Notes |
|---|---|---|
| `AZURE_PRICES_API_URL` | `https://prices.azure.com/api/retail/prices` | Public API, no credential. |
| `AZURE_PRICES_API_VERSION` | `2023-01-01-preview` | Savings-plan prices need this version or later. |
| `INCLUDE_AZURE_DEVTEST_PRICES` | `true` | `false` skips `DevTestConsumption` prices at the source (about 18% fewer requests) and records it in the manifest's `excluded_price_types`. |
| `PRICING_CURRENCY` | `USD` | Azure and GCP. Prices in other currencies are stored as null. |

### GCP

| Variable | Default | Notes |
|---|---|---|
| `GCP_PRICING_API_KEY` | none | **Secret, required for `gcp`.** See [The GCP API key](#the-gcp-api-key). |
| `GCP_BILLING_API_URL` | `https://cloudbilling.googleapis.com` | |
| `GCP_BILLING_API_VERSION` | `v2beta` | SKU and price listings. The services list always uses `v1`. |
| `GCP_BILLING_PAGE_SIZE` | `5000` | 1–5000. |
| `GCP_BILLING_BUSINESS_ENTITIES` | `businessEntities/GCP` | Comma-separated; which catalog services are in scope. |

## The GCP API key

The key is never committed or stored in GitHub. It's sent only in the `X-goog-api-key` header,
shown as `***` by `settings`, and scrubbed from logs, errors and alerts. A missing key exits 2
before anything is written; a rejected key fails the snapshot without retries. Restrict the key
to the Cloud Billing API in the Google Cloud console.

- **Locally**: put `GCP_PRICING_API_KEY=...` (no quotes) in a git-ignored `.env` at the repo
  root. Pass it to Docker with `--env-file .env`, or load it in a shell with
  `set -a; source .env; set +a`.
- **In AWS**: the SSM `SecureString` `/cloud-pricing-data-retrieval/<env>/gcp-pricing-api-key`.
  The operator creates and rotates it (OpenTofu never does, so the value never enters state).
  Only the execution role `cloud-pricing-task-execution-gcp-<env>` can read it, and ECS injects
  it into the GCP task. Rotation needs no redeploy.

  ```bash
  read -rs K && aws ssm put-parameter --name /cloud-pricing-data-retrieval/prod/gcp-pricing-api-key \
    --type SecureString --value "$K" && unset K     # add --overwrite to rotate
  ```

## Watchdog Lambda

| Variable | Default | Notes |
|---|---|---|
| `DATA_BUCKET`, `DATA_PREFIX` | from stack, `""` | Where to find `latest.json`. |
| `PROVIDERS` | `aws` | Set by the stack to every provider whose schedule is enabled. |
| `MAX_SNAPSHOT_AGE_DAYS` | `8` | `MISSED RUN` if `latest.json` is older. |
| `ALERT_TOPIC_ARN`, `ENVIRONMENT` | from stack | |

## OpenTofu variables

Shared settings for the `data` and `pipeline` stacks are in `infra/envs/<env>.tfvars` (each
stack warns about the variables it doesn't declare; that's expected).

### `infra/bootstrap`

| Variable | Default | Notes |
|---|---|---|
| `aws_region` | `us-east-1` | |
| `github_repository` | required | `owner/cloud-pricing-data-retrieval`; scopes the OIDC trust. |
| `github_immutable_subject` | `true` | GitHub's immutable OIDC subject format (repos created after 2026-07-15). |
| `github_owner_id`, `github_repo_id` | required | Numeric IDs used in that subject. |
| `state_bucket_name` | required | Account singleton. |
| `environments` | `["prod"]` | One set of CI roles per environment: `cloud-pricing-gha-{plan,apply,run}-<env>`. |
| `budget_warning_usd` / `budget_alert_usd` | `10` / `20` | |
| `budget_email` | required (secret) | |

### `infra/data`

| Variable | Default | Notes |
|---|---|---|
| `environment` | required | `dev`, `qa` or `prod`; in every resource name. |
| `data_bucket_name` | required | Keep the `cloud-pricing-data-<env>-` prefix (the deploy role matches it). |
| `pricing_providers` | `["aws"]` (prod: all three) | One prefix and raw lifecycle rule per provider. |
| `raw_retention_days` | `30` | |
| `noncurrent_version_retention_days` | `7` | Safety net for accidental deletes. |
| `roles_anywhere_ca_bundle_pem` | `""` | Public CA certificate(s) for off-AWS access; empty means none. |
| `external_writer_subjects` / `external_reader_subjects` | `[]` | Certificate CNs allowed to assume the writer / reader role. |
| `external_session_duration_seconds` | `3600` | |

### `infra/pipeline`

| Variable | Default | Notes |
|---|---|---|
| `environment`, `aws_region`, `data_bucket_name`, `state_bucket_name` | required | |
| `image_tag` | required | The release tag, passed by CI. |
| `provider_runs` | required | One entry per provider (below). |
| `pricing_providers`, `raw_retention_days` | as in `infra/data` | Shared with the data stack through the same tfvars. |
| `ephemeral_storage_gib` | `30` | |
| `run_timeout_minutes` / `run_claim_ttl_minutes` | `120` / `180` | |
| `pricing_download_retry_max_retries` / `_strategy` / `_base_delay_seconds` | `3` / `exponential_backoff` / `30` | |
| `parquet_weekly_retention_months` | `12` | |
| `superseded_file_grace_minutes` / `superseded_file_inline_wait_max_minutes` | `5` / `5` | |
| `success_summary_enabled` | `false` | |
| `alert_email` | required (GitHub secret `TF_VAR_ALERT_EMAIL`) | Needs a one-time click on the SNS confirmation email. |
| `max_snapshot_age_days` | `8` | Watchdog window. |
| `log_retention_days` | `30` | |

`provider_runs` creates one task definition and one weekly schedule per provider. Every key must
also be in the data stack's `pricing_providers`.

```hcl
provider_runs = {
  aws = {
    locations           = ["us-east-1", "us-east-2", ...]
    schedule_expression = "cron(0 13 ? * MON *)"   # UTC
    schedule_enabled    = true                     # false pauses this provider only
    task_cpu            = 1024
    task_memory         = 8192
  }
  azure = { ... task_memory = 2048 }
  gcp = {
    ...
    api_key_parameter   = "/cloud-pricing-data-retrieval/prod/gcp-pricing-api-key"
  }
}
```

**Stack outputs used elsewhere**:
- `data`: `data_bucket_name` and `data_read_policy_arn` (for `cloud-pricing-app`), plus the
  Roles Anywhere trust anchor and writer/reader role and profile ARNs (for off-AWS machines).
- `pipeline`: `ecs_cluster_arn`, `task_definition_families`, `subnet_ids`, `security_group_id`,
  `log_group_name` (for `run-pipeline.yml` and manual `aws ecs run-task`).
