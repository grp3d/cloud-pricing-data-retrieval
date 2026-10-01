variable "environment" {
  description = "Environment name; namespaces every resource (FR-032)."
  type        = string

  validation {
    condition     = contains(["dev", "qa", "prod"], var.environment)
    error_message = "environment must be dev, qa or prod."
  }
}

variable "data_bucket_name" {
  description = "Name of the pricing data bucket created by the data stack (same value as there)."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.data_bucket_name))
    error_message = "Must be a valid S3 bucket name: 3-63 characters, lowercase letters, digits and hyphens."
  }
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "pricing_providers" {
  description = "Providers whose latest.json the watchdog checks, and whose prefixes the task may write."
  type        = list(string)
  default     = ["aws"]
}

variable "image_tag" {
  description = "Pipeline image tag (the git SHA), passed in by CI."
  type        = string
}

variable "pricing_regions" {
  description = "AWS regions to download pricing for."
  type        = list(string)
  default     = ["us-east-1", "us-east-2", "us-west-1", "us-west-2", "eu-west-1", "eu-west-2", "ap-northeast-1"]

  validation {
    condition     = length(var.pricing_regions) > 0
    error_message = "pricing_regions must not be empty."
  }
}

variable "schedule_expression" {
  description = "EventBridge Scheduler expression, evaluated in UTC (FR-002)."
  type        = string
  default     = "cron(0 13 ? * MON *)"
}

variable "schedule_enabled" {
  description = "false pauses cloud runs while keeping all other infrastructure (FR-051)."
  type        = bool
  default     = true
}

variable "task_cpu" {
  type    = number
  default = 1024
}

variable "task_memory" {
  description = "MiB. A 7-region run peaks at ~3.9 GB per transform (research R1)."
  type        = number
  default     = 8192
}

variable "ephemeral_storage_gib" {
  type    = number
  default = 30

  validation {
    condition     = var.ephemeral_storage_gib >= 21 && var.ephemeral_storage_gib <= 200
    error_message = "ephemeral_storage_gib must be between 21 and 200."
  }
}

variable "run_timeout_minutes" {
  type    = number
  default = 120
}

variable "run_claim_ttl_minutes" {
  type    = number
  default = 180

  validation {
    condition     = var.run_claim_ttl_minutes > var.run_timeout_minutes
    error_message = "run_claim_ttl_minutes must be greater than run_timeout_minutes."
  }
}

variable "pricing_download_retry_max_retries" {
  type    = number
  default = 3
}

variable "pricing_download_retry_strategy" {
  type    = string
  default = "exponential_backoff"

  validation {
    condition     = contains(["fixed", "linear_backoff", "exponential_backoff"], var.pricing_download_retry_strategy)
    error_message = "pricing_download_retry_strategy must be fixed, linear_backoff or exponential_backoff."
  }
}

variable "pricing_download_retry_base_delay_seconds" {
  type    = number
  default = 30
}

variable "raw_retention_days" {
  description = "Must match the data stack's lifecycle rule (same value in envs/<env>.tfvars)."
  type        = number
  default     = 30
}

variable "parquet_weekly_retention_months" {
  type    = number
  default = 12
}

variable "superseded_file_grace_minutes" {
  type    = number
  default = 5
}

variable "superseded_file_inline_wait_max_minutes" {
  type    = number
  default = 5
}

variable "success_summary_enabled" {
  type    = bool
  default = false
}

variable "alert_email" {
  description = "Where alerts are emailed (supply via TF_VAR_alert_email; never committed)."
  type        = string
  default     = null
  sensitive   = true
}

variable "max_snapshot_age_days" {
  description = "Missed-run watchdog window (FR-027)."
  type        = number
  default     = 8
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for run logs (FR-029)."
  type        = number
  default     = 30
}

variable "state_bucket_name" {
  description = "IaC state bucket (the bucket in envs/<env>.backend.hcl), used to read the data stack's outputs."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.state_bucket_name))
    error_message = "Must be a valid S3 bucket name: 3-63 characters, lowercase letters, digits and hyphens."
  }
}
