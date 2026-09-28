variable "aws_region" {
  description = "Region for the state bucket and account-level resources."
  type        = string
  default     = "us-east-1"
}

variable "state_bucket_name" {
  description = "IaC state bucket name, chosen by the owner (e.g. cloud-pricing-shared-tfstate-<suffix>). Never the account ID."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.state_bucket_name))
    error_message = "Must be a valid S3 bucket name: 3-63 characters, lowercase letters, digits and hyphens."
  }
}

variable "environments" {
  description = "Environments that get their own set of GitHub Actions roles."
  type        = list(string)
  default     = ["prod"]

  validation {
    condition     = alltrue([for e in var.environments : contains(["dev", "qa", "prod"], e)])
    error_message = "environments must only contain dev, qa or prod."
  }
}

variable "github_repository" {
  description = "GitHub repository allowed to assume the CI roles, as owner/name."
  type        = string
  default     = null
}

variable "budget_email" {
  description = "Email address for AWS Budget notifications (supply via TF_VAR_budget_email)."
  type        = string
  default     = null
  sensitive   = true
}

variable "budget_warning_usd" {
  description = "Monthly actual spend that triggers a warning notification."
  type        = number
  default     = 15
}

variable "budget_alert_usd" {
  description = "Monthly actual (and forecasted) spend that triggers an alert notification."
  type        = number
  default     = 25
}
