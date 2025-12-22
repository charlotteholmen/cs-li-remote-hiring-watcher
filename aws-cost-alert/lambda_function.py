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


# Alert threshold configuration - dynamically from budgets or fallback to env/default
budget_thresholds = get_budget_threshold()
THRESHOLD = budget_thresholds[0] if budget_thresholds else float(
    os.getenv("COST_THRESHOLD", "3.0"))

# Slack Bot Token and Channel Configuration
SLACK_BOT_TOKEN = os.getenv(
    "SLACK_BOT_TOKEN",
    "xoxb-8538024246390-10163017103233-b4L515AxLdKfuAZ9pYaPuXK3")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "#recruiter-insights-ops")


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
        forecast = float(
            forecast_response["ForecastResultsByTime"][0]["MeanValue"])
        monthly_forecast = actual + forecast
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

    return actual, monthly_forecast, services


def get_detailed_cost_breakdown(top_services):
    """Get basic service breakdown - simplified to avoid excessive API calls"""
    # COST OPTIMIZATION: Removed detailed breakdown to prevent excessive API calls
    # Each additional API call costs $0.01, and detailed breakdowns can generate
    # hundreds of API calls (region x service x usage_type combinations)

    if not top_services:
        return {}

    # Return simplified breakdown without additional API calls
    detailed_breakdown = {}
    for service_name, service_cost in top_services:
        if service_cost >= 0.001:
            detailed_breakdown[service_name] = {
                "total_cost": service_cost,
                "optimization_note": "Detailed breakdown disabled to prevent high Cost Explorer API charges"
            }

    return detailed_breakdown


def get_region_name(region_code):
    """Convert AWS region codes to readable names"""
    region_names = {
        "us-east-1": "N. Virginia",
        "us-east-2": "Ohio",
        "us-west-1": "N. California",
        "us-west-2": "Oregon",
        "eu-west-1": "Ireland",
        "eu-west-2": "London",
        "eu-central-1": "Frankfurt",
        "ap-south-1": "Mumbai",
        "ap-southeast-1": "Singapore",
        "ap-southeast-2": "Sydney",
        "ap-northeast-1": "Tokyo",
        "ap-northeast-2": "Seoul",
        "ca-central-1": "Canada",
        "sa-east-1": "São Paulo",
        "NoRegion": "Global",
        "": "Global"
    }
    return region_names.get(region_code, region_code)


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


def clean_usage_type(usage_type, service_name):
    """Clean up usage type names for better readability"""
    # Remove service prefixes and common suffixes
    cleaned = usage_type

    # Remove region prefixes like "USE1-", "APS1-"
    if "-" in cleaned:
        parts = cleaned.split("-", 1)
        if len(parts[0]) <= 5 and parts[0].isupper():
            cleaned = parts[1]

    # Service-specific cleaning
    if "Lambda" in service_name:
        cleaned = cleaned.replace(
            "Lambda-",
            "").replace(
            "Request",
            "Requests").replace("Duration",
                                "Execution Time")
    elif "Key Management" in service_name:
        cleaned = cleaned.replace("KMS-", "").replace("Keys", "Key Usage")
    elif "Cost Explorer" in service_name:
        cleaned = cleaned.replace("APIRequest", "API Requests")
    elif "EC2" in service_name:
        cleaned = cleaned.replace(
            "BoxUsage", "Instance Hours").replace("EBS", "Storage")

    # General cleaning
    cleaned = cleaned.replace("-", " ").replace("_", " ")

    return cleaned if cleaned != usage_type else usage_type[:50]


def format_detailed_breakdown(detailed_breakdown):
    if not detailed_breakdown:
        return ""

    breakdown_text = "\n\n*🔍 Detailed Resource Breakdown (AWS Console Style):*\n"
    max_message_length = 2500
    current_length = len(breakdown_text)
    services_shown = 0
    total_services = len(detailed_breakdown)

    for service, details in detailed_breakdown.items():
        service_display = map_service_name(service)
        service_section = f"\n*💰 {service_display}* - Total: *${details['total_cost']:.3f}*\n"

# Create horizontal tabular format using monospace
        if details['regions'] or details['resource_details'] or details.get('instance_types') or details.get('operations'):
            service_section += "```\n"

            # Regional breakdown - horizontal
            if details['regions']:
                regions_data = []
                for region_name, cost, usage_qty in details['regions'][:3]:
                    usage_display = f"{float(usage_qty):.1f}" if usage_qty and float(
                        usage_qty) > 0 else "N/A"
                    regions_data.append(
                        f"{region_name}: ${cost:.3f}({usage_display})")
                service_section += f"🌍 REGIONS: {' | '.join(regions_data)}\n\n"

            # Resource details - horizontal
            if details['resource_details']:
                resource_data = []
                for resource in details['resource_details'][:3]:
                    # Truncate for horizontal display
                    resource_name = resource['usage_type'][:15]
                    qty = resource['usage_qty']
                    if qty >= 1000:
                        qty_display = f"{qty/1000:.1f}K"
                    elif qty >= 1:
                        qty_display = f"{qty:.1f}"
                    elif qty > 0:
                        qty_display = f"{qty:.3f}"
                    else:
                        qty_display = "N/A"
                    resource_data.append(
                        f"{resource_name}: ${resource['cost']:.3f}({qty_display})")
                service_section += f"🛠️ RESOURCES: {' | '.join(resource_data)}\n\n"

            # Instance types - horizontal
            if details.get('instance_types'):
                instance_data = []
                for instance_type, cost in details['instance_types'][:3]:
                    instance_data.append(f"{instance_type}: ${cost:.3f}")
                service_section += f"💻 INSTANCES: {' | '.join(instance_data)}\n\n"

            # Operations - horizontal
            if details.get('operations'):
                operation_data = []
                for operation, cost in details['operations'][:3]:
                    op_name = operation[:15]  # Truncate for horizontal display
                    operation_data.append(f"{op_name}: ${cost:.3f}")
                service_section += f"⚡ OPERATIONS: {' | '.join(operation_data)}\n\n"

            service_section += "```"

        if current_length + len(service_section) > max_message_length:
            breakdown_text += f"\n_... and {total_services - services_shown} more services (truncated for message size)_\n"
            break

        breakdown_text += service_section
        current_length += len(service_section)
        services_shown += 1

    breakdown_text += "\n_💡 All AWS services are automatically detected and included_\n"
    return breakdown_text


def send_slack(message):
    if not SLACK_BOT_TOKEN:
        return False

    try:
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                "Content-Type": "application/json"},
            json={
                "channel": SLACK_CHANNEL,
                "text": message,
                "username": "AWS Cost Alert Bot",
                "icon_emoji": ":money_with_wings:"},
            timeout=10)
        response.raise_for_status()
        return response.json().get("ok", False)
    except Exception:
        return False


def is_scheduled_notification(event):
    return event.get('source') == 'aws.events' or event.get(
        'detail-type') == 'Scheduled Event'


def lambda_handler(event, context):
    global api_call_count
    api_call_count = 0  # Reset counter for each execution

    try:
        actual, forecast, top_services = get_costs()
        est = pytz.timezone('US/Eastern')
        current_time_est = datetime.now(est)
        is_scheduled = is_scheduled_notification(event)

        # Check which specific threshold was exceeded
        exceeded_threshold = get_exceeded_threshold(
            actual, budget_thresholds) if budget_thresholds else None
        is_threshold_exceeded = exceeded_threshold is not None or actual > THRESHOLD

        # Debug logging for threshold detection
        exceeded_thresholds_list = [t for t in (
            budget_thresholds or []) if actual > t]
        print(f"🎯 Threshold Analysis:")
        print(f"  Current cost: ${actual:.2f}")
        print(f"  Budget thresholds: {budget_thresholds}")
        print(f"  All exceeded thresholds: {exceeded_thresholds_list}")
        print(
            f"  Highest exceeded: ${exceeded_threshold:.2f}" if exceeded_threshold else "  Highest exceeded: None")
        print(f"  Is threshold exceeded: {is_threshold_exceeded}")
        print(f"  Is scheduled: {is_scheduled}")

        services_text = "\n".join([f"• {map_service_name(svc)}: ${amt:.2f}" for svc,
                                   amt in top_services[:6]]) if top_services else "• No significant costs yet"

        # FIXED: Check for threshold alerts regardless of scheduled vs manual
        # Priority: Threshold alerts > Scheduled reports > Manual checks
        if is_threshold_exceeded:
            alert_threshold = exceeded_threshold if exceeded_threshold else THRESHOLD
            alert_type = "🚨 SCHEDULED THRESHOLD ALERT" if is_scheduled else "🚨 THRESHOLD EXCEEDED"

            # Show all exceeded thresholds for context
            exceeded_thresholds_list = [t for t in (
                budget_thresholds or []) if actual > t]
            exceeded_info = f"Current Threshold: ${alert_threshold:.2f}"
            if len(exceeded_thresholds_list) > 1:
                all_exceeded = ", ".join(
                    [f"${t:.2f}" for t in exceeded_thresholds_list])
                exceeded_info += f"\n*All Exceeded:* {all_exceeded}"

            message = f"{alert_type}\n\n*Current Spend:* ${actual:.2f}\n*Monthly Forecast:* ${forecast:.2f}\n*{exceeded_info}*\n\n*Top Services:*\n{services_text}\n\n_Alert sent at {current_time_est.strftime('%Y-%m-%d %I:%M %p EST')}_"
        elif is_scheduled:
            message = f"🔔 *Daily AWS Cost Update*\n\n*Current Month Spend:* ${actual:.2f}\n*Projected Month Total:* ${forecast:.2f}\n\n*Top Services This Month:*\n{services_text}\n\n_Daily report sent at {current_time_est.strftime('%Y-%m-%d %I:%M %p EST')}_"
        else:
            message = f"ℹ️ *AWS Cost Check*\n\n*Current Spend:* ${actual:.2f}\n*Monthly Forecast:* ${forecast:.2f}\n\n*Top Services:*\n{services_text}\n\n_Manual check at {current_time_est.strftime('%Y-%m-%d %I:%M %p EST')}_"

        # Send to Slack
        success = send_slack(message)

        # Log API usage summary
        api_cost = api_call_count * 0.01
        print(
            f"📊 API Usage Summary: {api_call_count} calls, estimated cost: ${api_cost:.2f}")

        return {
            "statusCode": 200,
            "body": {
                "status": "success",
                "actual_cost": actual,
                "forecast": forecast,
                "threshold_exceeded": is_threshold_exceeded,
                "exceeded_threshold": exceeded_threshold,
                "all_exceeded_thresholds": [t for t in (budget_thresholds or []) if actual > t],
                "is_scheduled": is_scheduled,
                "api_calls_made": api_call_count,
                "api_cost_estimate": api_cost,
                "timestamp": current_time_est.isoformat()
            }
        }

    except Exception as e:
        send_slack(f":x: *AWS Cost Alert Error*\n```{str(e)}```")
        return {"statusCode": 500, "body": {"status": "error", "message": str(e)}}
