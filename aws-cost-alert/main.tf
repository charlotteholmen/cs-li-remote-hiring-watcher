provider "aws" {
  region = "ap-south-1"
}

########################################
# IAM ROLE FOR LAMBDA
########################################
resource "aws_iam_role" "lambda_role" {
  name = "cost_alert_lambda_role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "lambda_policy" {
  name = "cost_alert_policy"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = [
          "ce:GetCostAndUsage",
          "ce:GetCostForecast"
        ]
        Resource = "*"
      }
    ]
  })
}

########################################
# SNS TOPIC
########################################
resource "aws_sns_topic" "cost_alerts" {
  name = "recruiter-insights-cost-alerts"
}

########################################
# LAMBDA FUNCTION
########################################
resource "aws_lambda_function" "cost_alert_lambda" {
  function_name = "aws-cost-alert-slack"
  handler       = "lambda_function.lambda_handler"
  runtime       = "python3.10"
  role          = aws_iam_role.lambda_role.arn
  filename      = "function.zip"

  environment {
    variables = {
      SLACK_WEBHOOK_URL = "https://hooks.slack.com/services/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"
    }
  }
}

########################################
# SNS → Lambda PERMISSION + SUBSCRIPTION
########################################
resource "aws_lambda_permission" "sns_permission" {
  statement_id  = "AllowSNSInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.cost_alert_lambda.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.cost_alerts.arn
}

resource "aws_sns_topic_subscription" "sns_to_lambda" {
  topic_arn = aws_sns_topic.cost_alerts.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.cost_alert_lambda.arn
}

########################################
# EVENTBRIDGE RULES (8:15 AM & 4:15 PM EST)
########################################

# 8:15 AM EST (13:15 UTC) / 8:15 AM EDT (12:15 UTC)
# Using EST time (13:15 UTC) - adjust for daylight savings manually if needed
resource "aws_cloudwatch_event_rule" "morning" {
  name                = "DailyCostAlertMorning"
  schedule_expression = "cron(15 13 * * ? *)"
  description         = "Trigger morning cost alert at 8:15 AM EST"
}

resource "aws_cloudwatch_event_target" "morning_target" {
  rule      = aws_cloudwatch_event_rule.morning.name
  target_id = "1"
  arn       = aws_lambda_function.cost_alert_lambda.arn
}

resource "aws_lambda_permission" "morning_permission" {
  statement_id  = "AllowMorningRule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.cost_alert_lambda.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.morning.arn
}

# 4:15 PM EST (21:15 UTC) / 4:15 PM EDT (20:15 UTC)
# Using EST time (21:15 UTC) - adjust for daylight savings manually if needed
resource "aws_cloudwatch_event_rule" "evening" {
  name                = "DailyCostAlertEvening"
  schedule_expression = "cron(15 21 * * ? *)"
  description         = "Trigger evening cost alert at 4:15 PM EST"
}

resource "aws_cloudwatch_event_target" "evening_target" {
  rule      = aws_cloudwatch_event_rule.evening.name
  target_id = "1"
  arn       = aws_lambda_function.cost_alert_lambda.arn
}

resource "aws_lambda_permission" "evening_permission" {
  statement_id  = "AllowEveningRule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.cost_alert_lambda.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.evening.arn
}

########################################
# MULTIPLE AWS BUDGETS ($3, $5, $10, $20)
########################################

locals {
  monthly_budgets = {
    "Monthly-Budget-4USD"  = 4
    "Monthly-Budget-5USD"  = 5
    "Monthly-Budget-10USD" = 10
    "Monthly-Budget-20USD" = 20
  }
}

resource "aws_budgets_budget" "monthly_budgets" {
  for_each = local.monthly_budgets

  name         = each.key
  budget_type  = "COST"
  limit_amount = each.value
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator = "GREATER_THAN"
    threshold           = 100
    threshold_type      = "PERCENTAGE"
    notification_type   = "ACTUAL"

    subscriber_sns_topic_arns = [
      aws_sns_topic.cost_alerts.arn
    ]
  }
}