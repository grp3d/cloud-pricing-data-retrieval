# Contract: Configuration (feature 004 changes)

This extends `specs/003-pipeline-cloud-deployment/contracts/configuration.md`. Every existing setting keeps its meaning and applies to every provider. An invalid value exits with code 2, as before.

## Pipeline settings (environment variables)

| Variable | Default | Valid | Notes |
|---|---|---|---|
| `PIPELINE_PROVIDER` | `aws` | `aws`, `azure`, `gcp` | Previously any `[a-z0-9]+`. It now must name a known provider. |
| `PRICING_REGIONS` | the provider's default locations (data-model.md) | comma-separated locations | It accepts multi-region locations for Azure and GCP. `global` is always added for Azure and GCP and may not be listed. |
| `PRICING_DOWNLOAD_RATE_LIMIT_PER_SECOND` | Azure `0.5`, GCP `1.0`, AWS unset (current behavior) | > 0 | Maximum request rate to the pricing source (FR-032). |
| `PRICING_DOWNLOAD_MAX_CONCURRENT_REQUESTS` | `1` (Azure, GCP) | ≥ 1 | Maximum requests in flight at once. |
| `PRICING_CURRENCY` | `USD` | ISO 4217 code | Azure and GCP only. |
| `AZURE_PRICES_API_URL` | `https://prices.azure.com/api/retail/prices` | https URL | |
| `AZURE_PRICES_API_VERSION` | `2023-01-01-preview` | | Savings plan prices need this version or later. |
| `INCLUDE_AZURE_DEVTEST_PRICES` | `true` | `true`, `false` | `false` adds `and type ne 'DevTestConsumption'` to every Azure request filter, so Dev/Test prices are never requested (about 18% fewer Azure requests), and the manifest lists `DevTestConsumption` in `excluded_price_types` (FR-035). Azure publishes Dev/Test as one price type, so there is no separate dev or test setting. |
| `GCP_BILLING_API_URL` | `https://cloudbilling.googleapis.com` | https URL | |
| `GCP_BILLING_API_VERSION` | `v2beta` | | Version for the SKU and price listings. The services list always uses `v1`. |
| `GCP_BILLING_PAGE_SIZE` | `5000` | 1–5000 | The maximum; fewer requests (FR-034). |
| `GCP_BILLING_BUSINESS_ENTITIES` | `businessEntities/GCP` | comma-separated | Which catalog services are in scope. |
| `GCP_PRICING_API_KEY` | none | non-empty | **Secret.** Required when `PIPELINE_PROVIDER=gcp`: if it's missing, the CLI exits 2 before claiming the date. Sent only in the `X-goog-api-key` header. Shown as `***` by `settings`. Never written to logs, alerts, manifests or data. |

The `pricing_download_retry_*` settings (feature 003, FR-047) apply to Azure and GCP pages; a retry resumes at the failed page (FR-034), and restarts the listing from its first page once if the source rejects the resume point.

The variable names above are the contract. Where they are read is an implementation detail: `AZURE_*` and `GCP_*` variables are read by the provider's own settings class (constitution VI, code organization), the others by the shared settings.

## Local use

```bash
# .env at the repo root (git-ignored by `.env*`)
PIPELINE_PROVIDER=gcp
GCP_PRICING_API_KEY=<your key>

docker run --rm --env-file .env -v "$PWD/.localdata:/data" \
  -e PIPELINE_STORAGE_URI=file:///data pricing-pipeline:local run --regions us-east1
```

## Infrastructure variables (`infra/pipeline`, `infra/envs/<env>.tfvars`)

`pricing_regions`, `schedule_expression`, `schedule_enabled`, `task_cpu` and `task_memory` are replaced by one map:

```hcl
providers = {
  aws = {
    locations           = ["us-east-1", "us-east-2", "us-west-1", "us-west-2", "eu-west-1", "eu-west-2", "ap-northeast-1"]
    schedule_expression = "cron(0 13 ? * MON *)"
    schedule_enabled    = true
    task_cpu            = 1024
    task_memory         = 8192
  }
  azure = {
    locations           = ["eastus", "eastus2", "westus", "westus2", "westeurope", "uksouth", "japaneast",
                           "north-america", "europe", "asia", "intercontinental"]
    schedule_expression = "cron(0 13 ? * MON *)"
    schedule_enabled    = false   # enabled together with gcp after validation (FR-033)
    task_cpu            = 1024
    task_memory         = 2048
  }
  gcp = {
    locations           = ["us-east1", "us-east4", "us-west1", "us-west2", "europe-west1", "europe-west2",
                           "asia-northeast1"]   # multi-region SKUs list their member regions (research R6)
    schedule_expression = "cron(0 13 ? * MON *)"
    schedule_enabled    = false
    task_cpu            = 1024
    task_memory         = 2048
    api_key_parameter   = "/cloud-pricing/prod/gcp-pricing-api-key"   # created by the operator (quickstart C1)
  }
}
```

`pricing_providers` (data stack) becomes `["aws", "azure", "gcp"]`. The watchdog checks only providers whose `schedule_enabled` is `true`.

## SSM parameter (GCP key)

| Name | Type | Key | Managed by |
|---|---|---|---|
| `/cloud-pricing/<env>/gcp-pricing-api-key` | `SecureString`, standard tier | `aws/ssm` | The operator, via a documented CLI command. **Not** OpenTofu, which would store the decrypted value in state (research R12). |

Only the execution role `cloud-pricing-task-execution-gcp-<env>` may read it. ECS injects it into the GCP task as `GCP_PRICING_API_KEY`.

## GitHub

`run-pipeline` gains a `provider` input (`aws` default, `azure`, `gcp`) that selects the provider's task definition. No new GitHub secrets: the GCP key isn't stored in GitHub.
