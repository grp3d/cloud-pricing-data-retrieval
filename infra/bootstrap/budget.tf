# Account-wide cost guard (FR-030): the whole project should cost $15–20/month, with a hard
# cap of $30. An account singleton (constitution V), tagged environment=shared.

resource "aws_budgets_budget" "account" {
  count        = var.budget_email == null ? 0 : 1
  name         = "cloud-pricing-shared-budget"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_alert_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    notification_type          = "ACTUAL"
    threshold                  = var.budget_warning_usd
    threshold_type             = "ABSOLUTE_VALUE"
    subscriber_email_addresses = [var.budget_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    notification_type          = "ACTUAL"
    threshold                  = var.budget_alert_usd
    threshold_type             = "ABSOLUTE_VALUE"
    subscriber_email_addresses = [var.budget_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    notification_type          = "FORECASTED"
    threshold                  = var.budget_alert_usd
    threshold_type             = "ABSOLUTE_VALUE"
    subscriber_email_addresses = [var.budget_email]
  }
}
