# Non-secret settings for prod (contracts/configuration.md). Secrets such as alert_email
# are supplied as TF_VAR_* environment variables, never committed.

environment       = "prod"
aws_region        = "us-east-1"
pricing_providers = ["aws", "azure", "gcp"]

# Bucket names are chosen by the owner and never contain the AWS account ID.
data_bucket_name  = "cloud-pricing-data-prod-g08a9i"
state_bucket_name = "cloud-pricing-shared-tfstate-g08a9i" # same value as bucket in prod.backend.hcl

# data stack
raw_retention_days                = 30
noncurrent_version_retention_days = 7

# pipeline stack: one entry per provider (feature 004). Azure and GCP ship with their
# schedules disabled and are enabled together after validation (FR-033, quickstart C6).
provider_runs = {
  aws = {
    locations           = ["us-east-1", "us-east-2", "us-west-1", "us-west-2", "eu-west-1", "eu-west-2", "ap-northeast-1"]
    schedule_expression = "cron(0 13 ? * MON *)"
    schedule_enabled    = true
    task_cpu            = 1024
    task_memory         = 8192
  }
  azure = {
    locations = ["eastus", "eastus2", "westus", "westus2", "westeurope", "uksouth", "japaneast",
    "north-america", "europe", "asia", "intercontinental"]
    schedule_expression = "cron(0 13 ? * MON *)"
    schedule_enabled    = true
    task_cpu            = 1024
    task_memory         = 2048
  }
  gcp = {
    locations           = ["us-east1", "us-east4", "us-west1", "us-west2", "europe-west1", "europe-west2", "asia-northeast1"]
    schedule_expression = "cron(0 13 ? * MON *)"
    schedule_enabled    = true
    task_cpu            = 1024
    task_memory         = 2048                                                     # full-catalog transform peaks at ~0.9 GiB (measured 2026-10-30)
    api_key_parameter   = "/cloud-pricing-data-retrieval/prod/gcp-pricing-api-key" # created by the operator (quickstart C1)
  }
}
ephemeral_storage_gib   = 30
run_timeout_minutes     = 120
run_claim_ttl_minutes   = 180
log_retention_days      = 30
max_snapshot_age_days   = 8
success_summary_enabled = false

pricing_download_retry_max_retries        = 3
pricing_download_retry_strategy           = "exponential_backoff"
pricing_download_retry_base_delay_seconds = 30
parquet_weekly_retention_months           = 12
superseded_file_grace_minutes             = 5
superseded_file_inline_wait_max_minutes   = 5
