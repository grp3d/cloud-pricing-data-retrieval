terraform {
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # tofu init -backend-config=../envs/<env>.backend.hcl -backend-config="key=<env>/data.tfstate"
  backend "s3" {}
}
