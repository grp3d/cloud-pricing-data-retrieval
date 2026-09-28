terraform {
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # First apply uses local state; then migrate into the bucket this stack creates:
  #   tofu init -migrate-state -backend-config=../envs/shared.backend.hcl
  # (see README.md). Uncomment after the first apply:
  backend "s3" {}
}
