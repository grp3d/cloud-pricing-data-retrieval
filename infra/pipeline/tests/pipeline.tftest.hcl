# Pipeline stack behavior that depends on inputs (tasks T092, SC-008, FR-051).
# Runs offline against a mocked AWS provider: `tofu test` in infra/pipeline.

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-1a", "us-east-1b"] }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  mock_data "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::cloud-pricing-data-prod-test01" }
  }
  # Computed ARNs must be well-formed, or the provider's own validators reject them.
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock" }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:us-east-1:123456789012:mock" }
  }
  mock_resource "aws_sqs_queue" {
    defaults = { arn = "arn:aws:sqs:us-east-1:123456789012:mock" }
  }
  mock_resource "aws_ecr_repository" {
    defaults = {
      arn            = "arn:aws:ecr:us-east-1:123456789012:repository/mock"
      repository_url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/mock"
    }
  }
  mock_resource "aws_ecs_cluster" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:cluster/mock" }
  }
  mock_resource "aws_ecs_task_definition" {
    defaults = {
      arn                  = "arn:aws:ecs:us-east-1:123456789012:task-definition/mock:1"
      arn_without_revision = "arn:aws:ecs:us-east-1:123456789012:task-definition/mock"
    }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:mock" }
  }
  mock_resource "aws_lambda_function" {
    defaults = { arn = "arn:aws:lambda:us-east-1:123456789012:function:mock" }
  }
}

mock_provider "archive" {}

variables {
  environment       = "prod"
  image_tag         = "abc123def456"
  data_bucket_name  = "cloud-pricing-data-prod-test01"
  state_bucket_name = "cloud-pricing-shared-tfstate-test01"
}

run "off_aws_writer_disabled" {
  command = plan

  override_data {
    target = data.terraform_remote_state.data
    values = { outputs = { writer_role_name = null } }
  }

  assert {
    condition     = length(aws_iam_role_policy.external_writer_pipeline) == 0
    error_message = "No grant for the writer role while off-AWS access is disabled."
  }
  assert {
    condition     = aws_scheduler_schedule.weekly.state == "ENABLED"
    error_message = "The schedule is enabled by default."
  }
}

run "off_aws_writer_enabled" {
  command = plan

  override_data {
    target = data.terraform_remote_state.data
    values = { outputs = { writer_role_name = "cloud-pricing-pipeline-writer-prod" } }
  }

  assert {
    condition     = aws_iam_role_policy.external_writer_pipeline[0].role == "cloud-pricing-pipeline-writer-prod"
    error_message = "The writer role gets alert publishing and image pulls from this stack."
  }
}

run "schedule_can_be_paused" {
  command = plan

  override_data {
    target = data.terraform_remote_state.data
    values = { outputs = { writer_role_name = null } }
  }

  variables {
    schedule_enabled = false
  }

  assert {
    condition     = aws_scheduler_schedule.weekly.state == "DISABLED"
    error_message = "schedule_enabled=false must pause cloud runs (FR-051)."
  }
}

run "task_definition_settings" {
  command = plan

  override_data {
    target = data.terraform_remote_state.data
    values = { outputs = { writer_role_name = null } }
  }

  assert {
    condition     = aws_ecs_task_definition.pipeline.family == "cloud-pricing-pipeline-prod"
    error_message = "Task family must follow constitution V naming."
  }
  assert {
    condition     = aws_ecs_task_definition.pipeline.runtime_platform[0].cpu_architecture == "ARM64"
    error_message = "The task runs on Graviton (research R1)."
  }
  assert {
    condition     = local.task_environment["PIPELINE_HOST_LABEL"] == "aws-ecs"
    error_message = "Cloud runs record run.host = aws-ecs."
  }
  assert {
    condition     = local.task_environment["PIPELINE_STORAGE_URI"] == "s3://cloud-pricing-data-prod-test01/"
    error_message = "The task writes to the protected data bucket."
  }
}

run "claim_ttl_must_exceed_timeout" {
  command = plan

  override_data {
    target = data.terraform_remote_state.data
    values = { outputs = { writer_role_name = null } }
  }

  variables {
    run_claim_ttl_minutes = 60
  }

  expect_failures = [var.run_claim_ttl_minutes]
}
