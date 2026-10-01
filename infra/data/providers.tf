provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project     = "cloud-pricing"
      component   = "data"
      environment = var.environment
    }
  }
}
