# The pipeline runs as a Fargate task per run: no always-on compute (FR-002, FR-006, R1).

resource "aws_ecs_cluster" "pipeline" {
  name = "cloud-pricing-${local.env}"
}

resource "aws_cloudwatch_log_group" "pipeline" {
  name              = "/cloud-pricing/pipeline-${local.env}"
  retention_in_days = var.log_retention_days
}

# --- IAM ------------------------------------------------------------------------------

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "cloud-pricing-task-execution-${local.env}"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# GCP only: a separate execution role is the only principal that may read the GCP key, so
# the AWS and Azure tasks can't (FR-007, FR-008). The parameter itself is created and
# rotated by the operator, never by OpenTofu, so its value never enters state (R12).
locals {
  gcp_key_parameter_arn = try(
    "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${local.account_id}:parameter${var.provider_runs["gcp"].api_key_parameter}",
    null,
  )
}

resource "aws_iam_role" "execution_gcp" {
  count              = contains(keys(var.provider_runs), "gcp") ? 1 : 0
  name               = "cloud-pricing-task-execution-gcp-${local.env}"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_gcp" {
  count      = length(aws_iam_role.execution_gcp)
  role       = aws_iam_role.execution_gcp[0].name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_gcp_key" {
  count = length(aws_iam_role.execution_gcp)
  statement {
    sid       = "ReadGcpPricingKey"
    actions   = ["ssm:GetParameters"]
    resources = [local.gcp_key_parameter_arn]
  }
}

resource "aws_iam_role_policy" "execution_gcp_key" {
  count  = length(aws_iam_role.execution_gcp)
  name   = "cloud-pricing-gcp-pricing-key-${local.env}"
  role   = aws_iam_role.execution_gcp[0].id
  policy = data.aws_iam_policy_document.execution_gcp_key[0].json
}

data "aws_iam_policy_document" "task" {
  statement {
    sid     = "DataReadWrite"
    actions = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = [
      for p in var.pricing_providers : "${data.aws_s3_bucket.data.arn}/${p}/*"
    ]
  }

  statement {
    sid       = "DataList"
    actions   = ["s3:ListBucket"]
    resources = [data.aws_s3_bucket.data.arn]
  }

  statement {
    sid = "PricingApi"
    actions = [
      "pricing:DescribeServices",
      "pricing:GetProducts",
      "pricing:ListPriceLists",
      "pricing:GetPriceListFileUrl",
      "ec2:DescribeRegions",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role" "task" {
  name               = "cloud-pricing-task-${local.env}"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy" "task" {
  name   = "cloud-pricing-task-${local.env}"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# --- task definition --------------------------------------------------------------------

locals {
  # The AWS family keeps its feature-003 name, so its task definition isn't replaced.
  task_families = {
    for p in keys(var.provider_runs) : p => p == "aws" ? "cloud-pricing-pipeline-${local.env}" : "cloud-pricing-pipeline-${p}-${local.env}"
  }

  # Container settings shared by every provider (contracts/configuration.md).
  task_environment = {
    PIPELINE_STORAGE_URI                      = "s3://${data.aws_s3_bucket.data.bucket}/"
    PIPELINE_ENVIRONMENT                      = local.env
    PIPELINE_HOST_LABEL                       = "aws-ecs"
    RUN_TIMEOUT_MINUTES                       = tostring(var.run_timeout_minutes)
    RUN_CLAIM_TTL_MINUTES                     = tostring(var.run_claim_ttl_minutes)
    PRICING_DOWNLOAD_RETRY_MAX_RETRIES        = tostring(var.pricing_download_retry_max_retries)
    PRICING_DOWNLOAD_RETRY_STRATEGY           = var.pricing_download_retry_strategy
    PRICING_DOWNLOAD_RETRY_BASE_DELAY_SECONDS = tostring(var.pricing_download_retry_base_delay_seconds)
    RAW_RETENTION_DAYS                        = tostring(var.raw_retention_days)
    PARQUET_WEEKLY_RETENTION_MONTHS           = tostring(var.parquet_weekly_retention_months)
    SUPERSEDED_FILE_GRACE_MINUTES             = tostring(var.superseded_file_grace_minutes)
    SUPERSEDED_FILE_INLINE_WAIT_MAX_MINUTES   = tostring(var.superseded_file_inline_wait_max_minutes)
    SUCCESS_SUMMARY_ENABLED                   = tostring(var.success_summary_enabled)
    ALERT_TOPIC_ARN                           = aws_sns_topic.alerts.arn
  }

  # Per provider: its locations, plus PIPELINE_PROVIDER for the new providers (AWS is the
  # default, and its environment stays as it was so its task definition isn't replaced).
  provider_environment = {
    for p, c in var.provider_runs : p => merge(
      local.task_environment,
      { PRICING_REGIONS = join(",", c.locations) },
      p == "aws" ? {} : { PIPELINE_PROVIDER = p },
    )
  }
}

resource "aws_ecs_task_definition" "pipeline" {
  for_each                 = var.provider_runs
  family                   = local.task_families[each.key]
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.task_cpu
  memory                   = each.value.task_memory
  execution_role_arn       = each.key == "gcp" ? aws_iam_role.execution_gcp[0].arn : aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  lifecycle {
    precondition {
      condition     = contains(var.pricing_providers, each.key)
      error_message = "Every provider in provider_runs must also be in pricing_providers (the data stack's prefixes)."
    }
  }

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  ephemeral_storage {
    size_in_gib = var.ephemeral_storage_gib
  }

  container_definitions = jsonencode([merge(
    {
      name      = "pipeline"
      image     = "${aws_ecr_repository.pipeline.repository_url}:${var.image_tag}"
      essential = true
      command   = ["run", "--trigger", "scheduled"]
      environment = [
        for name, value in local.provider_environment[each.key] : { name = name, value = value }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.pipeline.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "run"
        }
      }
    },
    # The key is injected by ECS from SSM at task start; only its ARN is in the definition.
    each.key == "gcp" ? {
      secrets = [{ name = "GCP_PRICING_API_KEY", valueFrom = local.gcp_key_parameter_arn }]
    } : {},
  )])
}

moved {
  from = aws_ecs_task_definition.pipeline
  to   = aws_ecs_task_definition.pipeline["aws"]
}
