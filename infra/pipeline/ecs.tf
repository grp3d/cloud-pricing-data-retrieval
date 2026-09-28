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
  task_family = "cloud-pricing-pipeline-${local.env}"

  # Container settings (contracts/configuration.md).
  task_environment = {
    PIPELINE_STORAGE_URI                      = "s3://${data.aws_s3_bucket.data.bucket}/"
    PIPELINE_ENVIRONMENT                      = local.env
    PIPELINE_HOST_LABEL                       = "aws-ecs"
    PRICING_REGIONS                           = join(",", var.pricing_regions)
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
}

resource "aws_ecs_task_definition" "pipeline" {
  family                   = local.task_family
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  ephemeral_storage {
    size_in_gib = var.ephemeral_storage_gib
  }

  container_definitions = jsonencode([{
    name      = "pipeline"
    image     = "${aws_ecr_repository.pipeline.repository_url}:${var.image_tag}"
    essential = true
    command   = ["run", "--trigger", "scheduled"]
    environment = [
      for name, value in local.task_environment : { name = name, value = value }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.pipeline.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "run"
      }
    }
  }])
}
