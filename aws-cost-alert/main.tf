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
  timeout       = 30  # Increased timeout for detailed cost analysis
  memory_size   = 256 # Increased memory for better performance

  environment {
    variables = {
      SLACK_BOT_TOKEN = "xoxb-8538024246390-10163017103233-b4L515AxLdKfuAZ9pYaPuXK3"
      SLACK_CHANNEL   = "#recruiter-insights-ops"
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
# NOTIFICATION SCHEDULES CONFIGURATION
########################################
# 🕐 NOTIFICATION SCHEDULES
# To add new notification times, simply add a new entry below.
# Format: cron(minute hour * * ? *) where hour is in UTC
# EST = UTC + 5, so 8:15 AM EST = 13:15 UTC
locals {
  notification_schedules = {
    "Morning" = {
      name         = "DailyCostAlertMorning"
      cron         = "cron(15 13 * * ? *)"  # 8:15 AM EST (13:15 UTC)
      description  = "Trigger morning cost alert at 8:15 AM EST"
      statement_id = "AllowMorningRule"
    }
    "Evening" = {
      name         = "DailyCostAlertEvening" 
      cron         = "cron(15 21 * * ? *)"  # 4:15 PM EST (21:15 UTC)
      description  = "Trigger evening cost alert at 4:15 PM EST"
      statement_id = "AllowEveningRule"
    }
    
    # 📝 TO ADD 9:15 AM EST NOTIFICATION: 
    # Uncomment the block below and run `terraform apply`
    # "MidMorning" = {
    #   name         = "DailyCostAlertMidMorning"
    #   cron         = "cron(15 14 * * ? *)"  # 9:15 AM EST (14:15 UTC)
    #   description  = "Trigger mid-morning cost alert at 9:15 AM EST"
    #   statement_id = "AllowMidMorningRule"
    # }
    
    # 📝 TEMPLATE FOR MORE NOTIFICATIONS:
    # "YourName" = {
    #   name         = "DailyCostAlertYourName"
    #   cron         = "cron(MINUTE HOUR_UTC * * ? *)"
    #   description  = "Your description"
    #   statement_id = "AllowYourNameRule"
    # }
  }
}

########################################
# STATE MIGRATION (for existing resources)
########################################
moved {
  from = aws_cloudwatch_event_rule.morning
  to   = aws_cloudwatch_event_rule.cost_alert_schedules["Morning"]
}

moved {
  from = aws_cloudwatch_event_rule.evening
  to   = aws_cloudwatch_event_rule.cost_alert_schedules["Evening"]
}

moved {
  from = aws_cloudwatch_event_target.morning_target
  to   = aws_cloudwatch_event_target.cost_alert_targets["Morning"]
}

moved {
  from = aws_cloudwatch_event_target.evening_target
  to   = aws_cloudwatch_event_target.cost_alert_targets["Evening"]
}

moved {
  from = aws_lambda_permission.morning_permission
  to   = aws_lambda_permission.cost_alert_permissions["Morning"]
}

moved {
  from = aws_lambda_permission.evening_permission
  to   = aws_lambda_permission.cost_alert_permissions["Evening"]
}

########################################
# EVENTBRIDGE RULES (DYNAMIC)
########################################

resource "aws_cloudwatch_event_rule" "cost_alert_schedules" {
  for_each = local.notification_schedules

  name                = each.value.name
  schedule_expression = each.value.cron
  description         = each.value.description
}

resource "aws_cloudwatch_event_target" "cost_alert_targets" {
  for_each = local.notification_schedules

  rule      = aws_cloudwatch_event_rule.cost_alert_schedules[each.key].name
  target_id = "1"
  arn       = aws_lambda_function.cost_alert_lambda.arn
}

resource "aws_lambda_permission" "cost_alert_permissions" {
  for_each = local.notification_schedules

  statement_id  = each.value.statement_id
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.cost_alert_lambda.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.cost_alert_schedules[each.key].arn
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
    "Monthly-Budget-3.67USD" = 3.67
    "Monthly-Budget-3.70USD" = 3.70
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