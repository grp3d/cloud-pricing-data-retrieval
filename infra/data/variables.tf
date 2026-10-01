variable "environment" {
  description = "Environment name; namespaces every resource (FR-032)."
  type        = string

  validation {
    condition     = contains(["dev", "qa", "prod"], var.environment)
    error_message = "environment must be dev, qa or prod."
  }
}

variable "data_bucket_name" {
  description = "Name of the pricing data bucket, chosen by the owner (e.g. cloud-pricing-data-prod-g08a9i). Globally unique; never contains the account ID."
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
  description = "Cloud pricing providers stored in this bucket; one raw-data lifecycle rule each."
  type        = list(string)
  default     = ["aws"]
}

variable "raw_retention_days" {
  description = "Days raw pricing files are kept before S3 expires them (FR-020)."
  type        = number
  default     = 30

  validation {
    condition     = var.raw_retention_days >= 1
    error_message = "raw_retention_days must be at least 1."
  }
}

variable "noncurrent_version_retention_days" {
  description = "Days deleted/overwritten object versions are kept as a safety net (R15)."
  type        = number
  default     = 7
}

# --- off-AWS access (US9, research R22) ---------------------------------------------------

variable "roles_anywhere_ca_bundle_pem" {
  description = "PUBLIC certificate(s) of the owner's CA (PEM). Empty disables off-AWS access."
  type        = string
  default     = ""
}

variable "external_writer_subjects" {
  description = "Client-certificate CNs allowed to publish snapshots from outside AWS, e.g. [\"home-server\"]."
  type        = list(string)
  default     = []
}

variable "external_reader_subjects" {
  description = "Client-certificate CNs allowed read-only access from outside AWS, e.g. [\"dev-laptop\"]."
  type        = list(string)
  default     = []
}

variable "external_session_duration_seconds" {
  description = "Lifetime of off-AWS credentials; the signing helper refreshes them automatically."
  type        = number
  default     = 3600

  validation {
    condition     = var.external_session_duration_seconds >= 900 && var.external_session_duration_seconds <= 43200
    error_message = "external_session_duration_seconds must be between 900 and 43200."
  }
}
