provider "aws" {
  region = var.aws_region

  # Account singletons (constitution V, v1.1.1): tagged environment=shared.
  default_tags {
    tags = {
      project     = "cloud-pricing"
      component   = "bootstrap"
      environment = "shared"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  account_id        = data.aws_caller_identity.current.account_id
  state_bucket_name = var.state_bucket_name
}
