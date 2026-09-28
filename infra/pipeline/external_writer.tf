# Lets the off-AWS writer role (data stack, US9) publish run alerts and pull the pipeline
# image. The alert topic and repository live in this stack, so the grant lives here too.
# Nothing is created while off-AWS access is disabled (writer_role_name is null).

data "terraform_remote_state" "data" {
  backend = "s3"
  config = {
    bucket = var.state_bucket_name
    key    = "${var.environment}/data.tfstate"
    region = var.aws_region
  }
}

locals {
  writer_role_name = try(data.terraform_remote_state.data.outputs.writer_role_name, null)
}

data "aws_iam_policy_document" "external_writer_pipeline" {
  statement {
    sid       = "PublishAlerts"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
  }

  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid       = "PullPipelineImage"
    actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
    resources = [aws_ecr_repository.pipeline.arn]
  }
}

resource "aws_iam_role_policy" "external_writer_pipeline" {
  count  = local.writer_role_name == null ? 0 : 1
  name   = "cloud-pricing-writer-pipeline-${local.env}"
  role   = local.writer_role_name
  policy = data.aws_iam_policy_document.external_writer_pipeline.json
}
