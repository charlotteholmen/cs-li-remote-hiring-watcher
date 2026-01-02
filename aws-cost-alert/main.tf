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
        Effect = "Allow"
        Action = [
          "ce:GetCostAndUsage",
          "ce:GetCostForecast"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "budgets:ViewBudget",
          "budgets:DescribeBudgets",
          "budgets:DescribeNotificationsForBudget",
          "sts:GetCallerIdentity"
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
# SNS TOPIC ACCESS POLICY (CRITICAL FIX)
########################################
resource "aws_sns_topic_policy" "cost_alerts_policy" {
  arn = aws_sns_topic.cost_alerts.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DefaultAllowAccountOwner"
        Effect = "Allow"
        Principal = {
          AWS = "*"
        }
        Action = [
          "SNS:GetTopicAttributes",
          "SNS:SetTopicAttributes",
          "SNS:AddPermission",
          "SNS:RemovePermission",
          "SNS:DeleteTopic",
          "SNS:Subscribe",
          "SNS:ListSubscriptionsByTopic",
          "SNS:Publish"
        ]
        Resource = aws_sns_topic.cost_alerts.arn
        Condition = {
          StringEquals = {
            "AWS:SourceOwner" = "615311846444"
          }
        }
      },
      {
        Sid    = "AllowAWSBudgetsToPublish"
        Effect = "Allow"
        Principal = {
          Service = "budgets.amazonaws.com"
        }
        Action   = "SNS:Publish"
        Resource = aws_sns_topic.cost_alerts.arn
      }
    ]
  })
}

########################################
# LAMBDA FUNCTIONS
########################################

# 1. SCHEDULED NOTIFICATION LAMBDA (EventBridge Triggered)
resource "aws_lambda_function" "scheduled_cost_notification" {
  function_name = "aws-cost-scheduled-notification"
  handler       = "scheduled_notification_lambda.lambda_handler"
  runtime       = "python3.10"
  role          = aws_iam_role.lambda_role.arn
  filename      = "scheduled_function.zip"
  timeout       = 30
  memory_size   = 256

  environment {
    variables = {
      SLACK_BOT_TOKEN = "xoxb-8538024246390-10163017103233-b4L515AxLdKfuAZ9pYaPuXK3"
      SLACK_CHANNEL   = "#recruiter-insights-ops"
    }
  }
}

# 2. THRESHOLD ALERT LAMBDA (SNS Triggered)
resource "aws_lambda_function" "threshold_alert" {
  function_name = "aws-cost-threshold-alert"
  handler       = "threshold_alert_lambda.lambda_handler"
  runtime       = "python3.10"
  role          = aws_iam_role.lambda_role.arn
  filename      = "threshold_function.zip"
  timeout       = 30
  memory_size   = 256

  environment {
    variables = {
      SLACK_BOT_TOKEN = "xoxb-8538024246390-10163017103233-b4L515AxLdKfuAZ9pYaPuXK3"
      SLACK_CHANNEL   = "#recruiter-insights-ops"
      COST_THRESHOLD  = "3.0"
    }
  }
}

########################################
# SNS → LAMBDA PERMISSION + SUBSCRIPTION
########################################
resource "aws_lambda_permission" "sns_permission" {
  statement_id  = "AllowSNSInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.threshold_alert.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.cost_alerts.arn
}

resource "aws_sns_topic_subscription" "sns_to_lambda" {
  topic_arn = aws_sns_topic.cost_alerts.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.threshold_alert.arn
}

########################################
# EVENTBRIDGE SCHEDULES (UNCHANGED)
########################################
# 🕐 NOTIFICATION SCHEDULES
# To add new notification times, simply add a new entry below.
# Format: cron(minute hour * * ? *) where hour is in UTC
# EST = UTC + 5, so 8:15 AM EST = 13:15 UTC
locals {
  notification_schedules = {
    "Morning" = {
      name         = "DailyCostAlertMorning"
      cron         = "cron(15 13 * * ? *)" # 8:15 AM EST (13:15 UTC)
      description  = "Trigger morning cost alert at 8:15 AM EST"
      statement_id = "AllowMorningRule"
    }
    "Evening" = {
      name         = "DailyCostAlertEvening"
      cron         = "cron(15 21 * * ? *)" # 4:15 PM EST (21:15 UTC)
      description  = "Trigger evening cost alert at 4:15 PM EST"
      statement_id = "AllowEveningRule"
    }
  }
}

resource "aws_cloudwatch_event_rule" "cost_alert_schedules" {
  for_each            = local.notification_schedules
  name                = each.value.name
  schedule_expression = each.value.cron
  description         = each.value.description
}

resource "aws_cloudwatch_event_target" "cost_alert_targets" {
  for_each  = local.notification_schedules
  rule      = aws_cloudwatch_event_rule.cost_alert_schedules[each.key].name
  target_id = "1"
  arn       = aws_lambda_function.scheduled_cost_notification.arn
}

resource "aws_lambda_permission" "cost_alert_permissions" {
  for_each      = local.notification_schedules
  statement_id  = each.value.statement_id
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.scheduled_cost_notification.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.cost_alert_schedules[each.key].arn
}

########################################
# AWS BUDGETS
########################################
locals {
  monthly_budgets = {
    "Monthly-Budget-10USD" = 10
    "Monthly-Budget-20USD" = 20
    "Monthly-Budget-TEST"  = 0.01
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
