terraform {
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # State lives in the bucket this stack created (shared/bootstrap.tfstate):
  #   tofu init -backend-config=../envs/shared.backend.hcl
  # Only when building a brand-new account does this block start commented out; see
  # README.md "Building a new account".
  backend "s3" {}
}
