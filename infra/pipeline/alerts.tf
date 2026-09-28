# Owner alerts (FR-026, research R12). One SNS topic with an email subscription; the
# recipient confirms the subscription once (the only manual step).
#
# Sources: the container itself (RUN FAILED / PARTIAL / REFUSED / CRASHED / RETENTION ERROR /
# SUCCEEDED), the ECS task-stopped rule below (TASK CRASHED), the scheduler DLQ alarm
# (SCHEDULE FAILED, scheduler.tf) and the watchdog (MISSED RUN / WATCHDOG ERROR, watchdog.tf).

resource "aws_sns_topic" "alerts" {
  name = "cloud-pricing-pipeline-alerts-${local.env}"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email == null ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

data "aws_iam_policy_document" "alerts_topic" {
  statement {
    sid       = "EventBridgeAndCloudWatchPublish"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com", "cloudwatch.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_sns_topic_policy" "alerts" {
  arn    = aws_sns_topic.alerts.arn
  policy = data.aws_iam_policy_document.alerts_topic.json
}

# The container publishes its own run alerts.
data "aws_iam_policy_document" "task_alerts" {
  statement {
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
  }
}

resource "aws_iam_role_policy" "task_alerts" {
  name   = "cloud-pricing-task-alerts-${local.env}"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_alerts.json
}

# TASK CRASHED: the task stopped with a non-zero exit (including OOM, 137) or never started.
# A run the container handled itself (even partial or refused) exits 0, so it isn't
# alerted twice.
resource "aws_cloudwatch_event_rule" "task_crashed" {
  name        = "cloud-pricing-task-crashed-${local.env}"
  description = "Pipeline task stopped abnormally"
  event_pattern = jsonencode({
    source      = ["aws.ecs"]
    detail-type = ["ECS Task State Change"]
    detail = {
      clusterArn = [aws_ecs_cluster.pipeline.arn]
      group      = ["family:${local.task_family}"]
      lastStatus = ["STOPPED"]
      "$or" = [
        { containers = { exitCode = [{ "anything-but" = 0 }] } },
        { stopCode = ["TaskFailedToStart"] },
      ]
    }
  })
}

resource "aws_cloudwatch_event_target" "task_crashed" {
  rule = aws_cloudwatch_event_rule.task_crashed.name
  arn  = aws_sns_topic.alerts.arn

  input_transformer {
    input_paths = {
      task     = "$.detail.taskArn"
      stopCode = "$.detail.stopCode"
      reason   = "$.detail.stoppedReason"
      exitCode = "$.detail.containers[0].exitCode"
    }
    input_template = "\"[cloud-pricing ${local.env}] TASK CRASHED: pipeline task <task> stopped (stopCode=<stopCode>, exitCode=<exitCode>): <reason>. Check CloudWatch Logs group ${aws_cloudwatch_log_group.pipeline.name}.\""
  }
}
