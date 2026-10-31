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
  description = "Providers whose prefixes the task may write (the data stack's list). Every key of `provider_runs` must be in it."
  type        = list(string)
  default     = ["aws"]
}

variable "image_tag" {
  description = "Pipeline image tag (the git SHA), passed in by CI."
  type        = string
}

variable "provider_runs" {
  description = <<-EOT
    One entry per pricing provider run in the cloud (feature 004, contracts/configuration.md):
    its locations, weekly schedule (UTC; schedule_enabled = false pauses it, FR-051), task size
    (MiB; an AWS 7-region run peaks at ~3.9 GB per transform, research R1) and, for gcp only,
    the operator-created SSM parameter holding its API key (research R12).
  EOT
  type = map(object({
    locations           = list(string)
    schedule_expression = string
    schedule_enabled    = bool
    task_cpu            = number
    task_memory         = number
    api_key_parameter   = optional(string)
  }))
  default = {
    aws = {
      locations           = ["us-east-1", "us-east-2", "us-west-1", "us-west-2", "eu-west-1", "eu-west-2", "ap-northeast-1"]
      schedule_expression = "cron(0 13 ? * MON *)"
      schedule_enabled    = true
      task_cpu            = 1024
      task_memory         = 8192
    }
  }

  validation {
    condition     = alltrue([for p in keys(var.provider_runs) : contains(["aws", "azure", "gcp"], p)])
    error_message = "provider_runs keys must be aws, azure or gcp."
  }
  validation {
    condition     = alltrue([for p, c in var.provider_runs : length(c.locations) > 0])
    error_message = "Every provider needs at least one location."
  }
  validation {
    condition = alltrue([
      for p, c in var.provider_runs : (p == "gcp") == (c.api_key_parameter != null)
    ])
    error_message = "api_key_parameter is required for gcp and allowed only for gcp."
  }
  validation {
    condition = alltrue([
      for p, c in var.provider_runs : c.api_key_parameter == null || can(regex("^/cloud-pricing/[a-z0-9-]+/[a-z0-9-]+$", c.api_key_parameter))
    ])
    error_message = "api_key_parameter must look like /cloud-pricing/<env>/<name>."
  }
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
