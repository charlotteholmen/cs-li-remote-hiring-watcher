import boto3
import os
import requests
import json
from datetime import datetime, timedelta, timezone
import pytz

ce = boto3.client("ce")
budgets = boto3.client("budgets")

# API Call Counter (each Cost Explorer API call costs $0.01)
api_call_count = 0


def log_api_call(api_name):
    """Track API calls for cost monitoring"""
    global api_call_count
    api_call_count += 1
    print(
        f"💰 Cost Explorer API Call #{api_call_count}: {api_name} (Cost: $0.01)")


def get_slack_config():
    """Fetch Slack configuration from environment variables."""
    return {
        "slack_bot_token": os.getenv("SLACK_BOT_TOKEN"),
        "slack_channel": os.getenv("SLACK_CHANNEL"),
    }


def get_budget_threshold():
    """Get alert threshold from AWS Budgets configuration"""
    try:
        sts = boto3.client('sts')
        account_id = sts.get_caller_identity()['Account']
        response = budgets.describe_budgets(
            AccountId=account_id, MaxResults=10)

        all_thresholds = []
        for budget in response.get('Budgets', []):
            try:
                notifications_response = budgets.describe_notifications_for_budget(
                    AccountId=account_id, BudgetName=budget['BudgetName'])

                for notification in notifications_response.get('Notifications', []):
                    if (notification.get('ComparisonOperator') == 'GREATER_THAN' and
                            notification.get('NotificationType') == 'ACTUAL'):

                        budget_amount = float(budget.get(
                            'BudgetLimit', {}).get('Amount', 0))
                        threshold_percent = float(
                            notification.get('Threshold', 0))

                        if budget_amount > 0 and threshold_percent > 0:
                            calculated_threshold = (
                                budget_amount * threshold_percent) / 100
                            all_thresholds.append(calculated_threshold)
            except Exception:
                continue

        # Return all thresholds sorted ascending so we can find the first one exceeded
        return sorted(all_thresholds) if all_thresholds else None
    except Exception:
        return None


def get_exceeded_threshold(actual_cost, thresholds):
    """Get the specific threshold that was exceeded"""
    if not thresholds or actual_cost <= 0:
        return None

    # Find the highest threshold that was exceeded
    exceeded_threshold = None
    for threshold in thresholds:
        if actual_cost > threshold:
            exceeded_threshold = threshold

    # Additional logging for debugging
    if exceeded_threshold:
        print(
            f"💰 Threshold exceeded: ${exceeded_threshold:.2f} (current: ${actual_cost:.2f})")

    return exceeded_threshold


def get_costs():
    """Get current month's actual costs and forecast"""
    # Get current date - Cost Explorer needs tomorrow as end date to include today
    now = datetime.now()
    start_of_month = now.replace(day=1).strftime("%Y-%m-%d")
    # Use tomorrow as end date to include today's costs
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    # Get ACTUAL cost for current month to date
    log_api_call("GetCostAndUsage - Monthly Service Breakdown")
    response = ce.get_cost_and_usage(
        TimePeriod={
            "Start": start_of_month,
            "End": tomorrow,
        },
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}]
    )

    # Fix the metric access - handle various response structures
    actual = 0.0
    services = []

    try:
        if response.get("ResultsByTime") and len(response["ResultsByTime"]) > 0:
            result = response["ResultsByTime"][0]

            if "Groups" in result:
                for group in result["Groups"]:
                    cost_amount = group.get(
                        "Metrics",
                        {}).get(
                        "UnblendedCost",
                        {}).get(
                        "Amount",
                        "0")
                    if cost_amount != "0":
                        actual += float(cost_amount)

                services = sorted(
                    [
                        (group["Keys"][0],
                         float(
                            group.get(
                                "Metrics",
                                {}).get(
                                "UnblendedCost",
                                {}).get(
                                "Amount",
                                "0"))) for group in result["Groups"] if group.get(
                            "Metrics",
                            {}).get(
                                "UnblendedCost",
                                {}).get(
                                    "Amount",
                                    "0") != "0" and float(
                                        group.get(
                                            "Metrics",
                                            {}).get(
                                                "UnblendedCost",
                                                {}).get(
                                                    "Amount",
                                                    "0")) >= 0.001],
                    key=lambda x: x[1],
                    reverse=True)
    except (KeyError, IndexError, ValueError):
        pass

    # Get FORECAST for entire month
    try:
        log_api_call("GetCostForecast - Monthly Forecast")
        forecast_response = ce.get_cost_forecast(
            TimePeriod={
                "Start": tomorrow,
                "End": (
                    now.replace(
                        month=now.month + 1,
                        day=1) if now.month < 12 else now.replace(
                        year=now.year + 1,
                        month=1,
                        day=1)).strftime("%Y-%m-%d"),
            },
            Metric="UNBLENDED_COST")
        forecast_total = 0.0
        for entry in forecast_response.get("ForecastResultsByTime", []):
            try:
                forecast_total += float(entry.get("MeanValue", 0))
            except (TypeError, ValueError):
                continue
        monthly_forecast = actual + forecast_total
    except Exception:
        # Fallback: estimate based on daily average
        days_in_month = (
            now.replace(
                month=now.month +
                1,
                day=1) if now.month < 12 else now.replace(
                year=now.year +
                1,
                month=1,
                day=1) -
            timedelta(
                days=1)).day
        days_elapsed = now.day
        daily_avg = actual / days_elapsed if days_elapsed > 0 else 0
        monthly_forecast = daily_avg * days_in_month

    if monthly_forecast < actual:
        monthly_forecast = actual

    return actual, monthly_forecast, services


def map_service_name(service_name):
    """Map AWS Cost Explorer service names to AWS Console display names"""
    service_mapping = {
        "Amazon Elastic Compute Cloud - Compute": "EC2-Instance",
        "Amazon Elastic Compute Cloud - Other": "EC2-Other",
        "Amazon Relational Database Service": "Relational Database Service",
        "Amazon EC2 Container Registry": "EC2 Container Registry",
        "Amazon EC2 Container Registry (ECR)": "EC2 Container Registry",
        "Amazon Simple Storage Service": "Simple Storage Service",
        "AWS Key Management Service": "Key Management Service",
        "AWS Cost Explorer": "Cost Explorer",
        "AWS Lambda": "Lambda",
        "AWS Secrets Manager": "Secrets Manager",
        "Amazon API Gateway": "API Gateway",
        "Amazon CloudWatch": "CloudWatch",
        "Amazon Route 53": "Route 53",
        "Amazon ElastiCache": "ElastiCache",
        "Amazon Elastic Load Balancing": "Elastic Load Balancing",
        "AWS Data Transfer": "Data Transfer",
        "Amazon CloudFront": "CloudFront",
        "Amazon Virtual Private Cloud": "VPC",
        "AWS Support (Business)": "Support",
        "Tax": "Tax"
    }
    return service_mapping.get(
        service_name,
        service_name.replace(
            "Amazon ",
            "").replace(
            "AWS ",
            ""))


def send_slack(message, slack_token=None, slack_channel=None):
    config = get_slack_config()
    token = slack_token or config["slack_bot_token"]
    channel = slack_channel or config["slack_channel"]

    if not token:
        print("❌ SLACK_BOT_TOKEN is missing")
        return False

    if not channel:
        print("❌ SLACK_CHANNEL is missing")
        return False

    try:
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "channel": channel,
                "text": message,
                "username": "AWS Cost Alert Bot",
                "icon_emoji": ":money_with_wings:",
            },
            timeout=10,
        )

        slack_response = response.json()
        print(f"📨 Slack API response: {slack_response}")

        return slack_response.get("ok", False)

    except Exception as e:
        print(f"❌ Slack exception: {e}")
        return False


def get_current_time_est():
    """Get current time in Eastern timezone"""
    est = pytz.timezone('US/Eastern')
    return datetime.now(est)


def get_api_call_count():
    """Get current API call count"""
    global api_call_count
    return api_call_count


def reset_api_call_count():
    """Reset API call count"""
    global api_call_count
    api_call_count = 0
