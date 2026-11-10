# Pipeline stack behavior that depends on inputs (tasks T092, SC-008, FR-051; feature 004 T045).
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
    condition     = aws_scheduler_schedule.weekly["aws"].state == "ENABLED"
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
    provider_runs = {
      aws = {
        locations           = ["us-east-1"]
        schedule_expression = "cron(0 13 ? * MON *)"
        schedule_enabled    = false
        task_cpu            = 1024
        task_memory         = 8192
      }
    }
  }

  assert {
    condition     = aws_scheduler_schedule.weekly["aws"].state == "DISABLED"
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
    condition     = aws_ecs_task_definition.pipeline["aws"].family == "cloud-pricing-pipeline-prod"
    error_message = "Task family must follow constitution V naming."
  }
  assert {
    condition     = aws_ecs_task_definition.pipeline["aws"].runtime_platform[0].cpu_architecture == "ARM64"
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

# --- feature 004: one task definition and schedule per provider (T045) ------------------

run "three_providers" {
  command = plan

  override_data {
    target = data.terraform_remote_state.data
    values = { outputs = { writer_role_name = null } }
  }

  variables {
    pricing_providers = ["aws", "azure", "gcp"]
    provider_runs = {
      aws = {
        locations           = ["us-east-1", "us-east-2"]
        schedule_expression = "cron(0 13 ? * MON *)"
        schedule_enabled    = true
        task_cpu            = 1024
        task_memory         = 8192
      }
      azure = {
        locations           = ["eastus", "north-america"]
        schedule_expression = "cron(0 13 ? * MON *)"
        schedule_enabled    = false
        task_cpu            = 1024
        task_memory         = 2048
      }
      gcp = {
        locations           = ["us-east1"]
        schedule_expression = "cron(0 13 ? * MON *)"
        schedule_enabled    = false
        task_cpu            = 1024
        task_memory         = 2048
        api_key_parameter   = "/cloud-pricing-data-retrieval/prod/gcp-pricing-api-key"
      }
    }
  }

  assert {
    condition     = length(aws_ecs_task_definition.pipeline) == 3 && length(aws_scheduler_schedule.weekly) == 3
    error_message = "One task definition and one schedule per provider."
  }
  assert {
    condition = (
      aws_ecs_task_definition.pipeline["aws"].family == "cloud-pricing-pipeline-prod" &&
      aws_ecs_task_definition.pipeline["azure"].family == "cloud-pricing-pipeline-azure-prod" &&
      aws_ecs_task_definition.pipeline["gcp"].family == "cloud-pricing-pipeline-gcp-prod" &&
      aws_scheduler_schedule.weekly["aws"].name == "cloud-pricing-pipeline-prod"
    )
    error_message = "AWS keeps its feature-003 names; new providers add theirs."
  }
  assert {
    condition = (
      aws_scheduler_schedule.weekly["aws"].state == "ENABLED" &&
      aws_scheduler_schedule.weekly["azure"].state == "DISABLED" &&
      aws_scheduler_schedule.weekly["gcp"].state == "DISABLED"
    )
    error_message = "Azure and GCP ship with their schedules disabled (FR-033)."
  }
  assert {
    condition = (
      local.provider_environment["azure"]["PIPELINE_PROVIDER"] == "azure" &&
      local.provider_environment["azure"]["PRICING_REGIONS"] == "eastus,north-america" &&
      local.provider_environment["gcp"]["PIPELINE_PROVIDER"] == "gcp" &&
      !contains(keys(local.provider_environment["aws"]), "PIPELINE_PROVIDER") &&
      local.provider_environment["aws"]["PRICING_REGIONS"] == "us-east-1,us-east-2"
    )
    error_message = "Each task gets its provider and locations; the AWS environment is unchanged."
  }
  assert {
    condition = alltrue([
      for p, env in local.provider_environment : !anytrue([for k, v in env : strcontains(k, "API_KEY")])
    ])
    error_message = "No key is ever a plain environment variable."
  }
  assert {
    condition     = strcontains(aws_ecs_task_definition.pipeline["gcp"].container_definitions, "\"secrets\":[{\"name\":\"GCP_PRICING_API_KEY\",\"valueFrom\":\"arn:aws:ssm:us-east-1:123456789012:parameter/cloud-pricing-data-retrieval/prod/gcp-pricing-api-key\"}]")
    error_message = "The GCP task gets the key from the SSM parameter by ARN."
  }
  assert {
    condition = (
      !strcontains(aws_ecs_task_definition.pipeline["aws"].container_definitions, "secrets") &&
      !strcontains(aws_ecs_task_definition.pipeline["azure"].container_definitions, "secrets")
    )
    error_message = "Only the GCP task definition has secrets (FR-008)."
  }
  assert {
    condition = (
      toset(keys(jsondecode(aws_ecs_task_definition.pipeline["aws"].container_definitions)[0])) ==
      toset(["name", "image", "essential", "command", "environment", "logConfiguration"])
    )
    error_message = "The AWS container definition keeps its feature-003 shape, so moving it to [\"aws\"] replaces nothing (SC-008)."
  }
  assert {
    condition = (
      length(aws_iam_role.execution_gcp) == 1 &&
      aws_iam_role.execution_gcp[0].name == "cloud-pricing-task-execution-gcp-prod"
    )
    error_message = "GCP gets its own execution role."
  }
  assert {
    condition = (
      data.aws_iam_policy_document.execution_gcp_key[0].statement[0].actions == toset(["ssm:GetParameters"]) &&
      data.aws_iam_policy_document.execution_gcp_key[0].statement[0].resources == toset(["arn:aws:ssm:us-east-1:123456789012:parameter/cloud-pricing-data-retrieval/prod/gcp-pricing-api-key"])
    )
    error_message = "The GCP execution role may read that one parameter and nothing else."
  }
  assert {
    condition     = length(aws_iam_role_policy_attachment.execution_gcp) == 1
    error_message = "The GCP execution role still has the standard execution policy."
  }
  assert {
    condition = length([
      for g in jsondecode(aws_cloudwatch_event_rule.task_crashed.event_pattern).detail.group : g
      if contains(["family:cloud-pricing-pipeline-prod", "family:cloud-pricing-pipeline-azure-prod", "family:cloud-pricing-pipeline-gcp-prod"], g)
    ]) == 3
    error_message = "TASK CRASHED covers every provider's task family (FR-026)."
  }
  assert {
    condition     = aws_lambda_function.watchdog.environment[0].variables["PROVIDERS"] == "aws"
    error_message = "The watchdog checks only providers that are scheduled."
  }
}

run "aws_only_has_no_gcp_role" {
  command = plan

  override_data {
    target = data.terraform_remote_state.data
    values = { outputs = { writer_role_name = null } }
  }

  assert {
    condition     = length(aws_iam_role.execution_gcp) == 0 && length(aws_ecs_task_definition.pipeline) == 1
    error_message = "Without gcp in provider_runs there is no GCP role or task."
  }
}

run "api_key_parameter_only_for_gcp" {
  command = plan

  override_data {
    target = data.terraform_remote_state.data
    values = { outputs = { writer_role_name = null } }
  }

  variables {
    provider_runs = {
      azure = {
        locations           = ["eastus"]
        schedule_expression = "cron(0 13 ? * MON *)"
        schedule_enabled    = false
        task_cpu            = 1024
        task_memory         = 2048
        api_key_parameter   = "/cloud-pricing-data-retrieval/prod/azure-key"
      }
    }
  }

  expect_failures = [var.provider_runs]
}

run "gcp_requires_api_key_parameter" {
  command = plan

  override_data {
    target = data.terraform_remote_state.data
    values = { outputs = { writer_role_name = null } }
  }

  variables {
    provider_runs = {
      gcp = {
        locations           = ["us-east1"]
        schedule_expression = "cron(0 13 ? * MON *)"
        schedule_enabled    = false
        task_cpu            = 1024
        task_memory         = 2048
      }
    }
  }

  expect_failures = [var.provider_runs]
}
