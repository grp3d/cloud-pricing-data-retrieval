terraform {
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }

  # tofu init -backend-config=../envs/<env>.backend.hcl -backend-config="key=<env>/pipeline.tfstate"
  backend "s3" {}
}
