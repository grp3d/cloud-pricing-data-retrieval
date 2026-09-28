provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project     = "cloud-pricing"
      component   = "pipeline"
      environment = var.environment
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  env        = var.environment
  account_id = data.aws_caller_identity.current.account_id

  # The data bucket lives in the separate, protected `data` stack (research R13).
  data_bucket_name = var.data_bucket_name
}

data "aws_s3_bucket" "data" {
  bucket = local.data_bucket_name
}
