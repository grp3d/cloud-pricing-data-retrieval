# Missed-run watchdog (FR-027, research R11): a small Lambda, independent of the pipeline
# image, checks latest.json daily and alerts if the newest succeeded snapshot is too old.

data "archive_file" "watchdog" {
  type        = "zip"
  source_file = "${path.module}/../../src/watchdog/freshness_check.py"
  output_path = "${path.module}/.build/watchdog.zip"
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "watchdog" {
  name               = "cloud-pricing-watchdog-${local.env}"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "watchdog" {
  name              = "/aws/lambda/cloud-pricing-watchdog-${local.env}"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "watchdog" {
  statement {
    sid       = "ReadLatest"
    actions   = ["s3:GetObject"]
    resources = [for p in var.pricing_providers : "${data.aws_s3_bucket.data.arn}/${p}/manifests/latest.json"]
  }

  # Without ListBucket a missing key reads as AccessDenied instead of NoSuchKey.
  statement {
    sid       = "ListForNotFound"
    actions   = ["s3:ListBucket"]
    resources = [data.aws_s3_bucket.data.arn]
  }

  statement {
    sid       = "Alert"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
  }

  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.watchdog.arn}:*"]
  }
}

resource "aws_iam_role_policy" "watchdog" {
  name   = "cloud-pricing-watchdog-${local.env}"
  role   = aws_iam_role.watchdog.id
  policy = data.aws_iam_policy_document.watchdog.json
}

resource "aws_lambda_function" "watchdog" {
  function_name    = "cloud-pricing-watchdog-${local.env}"
  role             = aws_iam_role.watchdog.arn
  runtime          = "python3.13"
  handler          = "freshness_check.handler"
  filename         = data.archive_file.watchdog.output_path
  source_code_hash = data.archive_file.watchdog.output_base64sha256
  memory_size      = 128
  timeout          = 30

  environment {
    variables = {
      DATA_BUCKET           = data.aws_s3_bucket.data.bucket
      DATA_PREFIX           = ""
      PROVIDERS             = join(",", var.pricing_providers)
      MAX_SNAPSHOT_AGE_DAYS = tostring(var.max_snapshot_age_days)
      ALERT_TOPIC_ARN       = aws_sns_topic.alerts.arn
      ENVIRONMENT           = local.env
    }
  }

  depends_on = [aws_cloudwatch_log_group.watchdog]
}

# Daily invocation at 14:00 UTC.
data "aws_iam_policy_document" "watchdog_scheduler" {
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.watchdog.arn]
  }
}

resource "aws_iam_role" "watchdog_scheduler" {
  name               = "cloud-pricing-watchdog-scheduler-${local.env}"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "watchdog_scheduler" {
  name   = "cloud-pricing-watchdog-scheduler-${local.env}"
  role   = aws_iam_role.watchdog_scheduler.id
  policy = data.aws_iam_policy_document.watchdog_scheduler.json
}

resource "aws_scheduler_schedule" "watchdog" {
  name                         = "cloud-pricing-watchdog-${local.env}"
  schedule_expression          = "cron(0 14 * * ? *)"
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.watchdog.arn
    role_arn = aws_iam_role.watchdog_scheduler.arn
  }
}

# WATCHDOG ERROR: a broken watchdog must not go unnoticed.
resource "aws_cloudwatch_metric_alarm" "watchdog_errors" {
  alarm_name          = "cloud-pricing-watchdog-error-${local.env}"
  alarm_description   = "WATCHDOG ERROR: the missed-run watchdog Lambda failed"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.watchdog.function_name }
  statistic           = "Sum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}
