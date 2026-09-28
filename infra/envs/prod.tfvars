# Non-secret settings for prod (contracts/configuration.md). Secrets such as alert_email
# are supplied as TF_VAR_* environment variables, never committed.

environment       = "prod"
aws_region        = "us-east-1"
pricing_providers = ["aws"]

# Bucket names are chosen by the owner and never contain the AWS account ID.
data_bucket_name  = "cloud-pricing-data-prod-g08a9i"
state_bucket_name = "cloud-pricing-shared-tfstate-g08a9i" # same value as bucket in prod.backend.hcl

# data stack
raw_retention_days                = 30
noncurrent_version_retention_days = 7

# pipeline stack
pricing_regions = [
  "us-east-1",
  "us-east-2",
  "us-west-1",
  "us-west-2",
  "eu-west-1",
  "eu-west-2",
  "ap-northeast-1",
]
schedule_expression     = "cron(0 13 ? * MON *)"
schedule_enabled        = true
task_cpu                = 1024
task_memory             = 8192
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
