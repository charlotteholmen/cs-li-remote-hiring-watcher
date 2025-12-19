import boto3
import os
import requests
import json
from datetime import datetime, timedelta, timezone
import pytz

ce = boto3.client("ce")
budgets = boto3.client("budgets")


def get_budget_threshold():
    """Get alert threshold from AWS Budgets configuration"""
    try:
        # Get AWS account ID
        sts = boto3.client('sts')
        account_id = sts.get_caller_identity()['Account']

        # List budgets to find cost alert configurations
        response = budgets.describe_budgets(
            AccountId=account_id,
            MaxResults=10
        )

        # Find the first budget with actual cost alerts configured
        for budget in response.get('Budgets', []):
            try:
                # Get budget notifications/alerts
                notifications_response = budgets.describe_notifications_for_budget(
                    AccountId=account_id,
                    BudgetName=budget['BudgetName']
                )

                # Look for ACTUAL cost threshold alerts
                for notification in notifications_response.get('Notifications', []):
                    if (notification.get('ComparisonOperator') == 'GREATER_THAN' and
                            notification.get('NotificationType') == 'ACTUAL'):

                        # Calculate threshold from budget amount and threshold
                        # percentage
                        budget_amount = float(budget.get(
                            'BudgetLimit', {}).get('Amount', 0))
                        threshold_percent = float(
                            notification.get('Threshold', 0))

                        if budget_amount > 0 and threshold_percent > 0:
                            return (budget_amount * threshold_percent) / 100
            except Exception:
                continue
        return None
    except Exception:
        return None


# Alert threshold configuration - dynamically from budgets or fallback to env/default
budget_threshold = get_budget_threshold()
THRESHOLD = budget_threshold if budget_threshold else float(
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
    """Get AWS console-style detailed breakdown of resources for top services"""
    if not top_services:
        return {}

    now = datetime.now()
    start_of_month = now.replace(day=1).strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    detailed_breakdown = {}

    services_to_analyze = [
        (service_name,
         service_cost) for service_name,
        service_cost in top_services if service_cost >= 0.001]

    for service_name, service_cost in services_to_analyze:

        try:
            service_data = {
                "total_cost": service_cost,
                "regions": [],
                "usage_types": [],
                "instance_types": [],
                "resource_details": []
            }

            # 1. Get region-wise breakdown
            region_response = ce.get_cost_and_usage(
                TimePeriod={"Start": start_of_month, "End": tomorrow},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost", "UsageQuantity"],
                GroupBy=[
                    {"Type": "DIMENSION", "Key": "SERVICE"},
                    {"Type": "DIMENSION", "Key": "REGION"}
                ],
                Filter={
                    "Dimensions": {
                        "Key": "SERVICE",
                        "Values": [service_name]
                    }
                }
            )

            if region_response.get("ResultsByTime"):
                for group in region_response["ResultsByTime"][0].get("Groups", []):
                    cost_amount = float(group.get("Metrics", {}).get(
                        "UnblendedCost", {}).get("Amount", "0"))
                    usage_qty = group.get("Metrics", {}).get(
                        "UsageQuantity", {}).get("Amount", "0")

                    if cost_amount > 0.01:
                        region = group["Keys"][1] if len(
                            group["Keys"]) > 1 else "Global"
                        # Convert region codes to readable names
                        region_name = get_region_name(region)
                        service_data["regions"].append(
                            (region_name, cost_amount, usage_qty))

            # 2. Get usage type breakdown for this service
            usage_response = ce.get_cost_and_usage(
                TimePeriod={"Start": start_of_month, "End": tomorrow},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost", "UsageQuantity"],
                GroupBy=[
                    {"Type": "DIMENSION", "Key": "SERVICE"},
                    {"Type": "DIMENSION", "Key": "USAGE_TYPE"}
                ],
                Filter={
                    "Dimensions": {
                        "Key": "SERVICE",
                        "Values": [service_name]
                    }
                }
            )

            if usage_response.get("ResultsByTime"):
                for group in usage_response["ResultsByTime"][0].get("Groups", []):
                    cost_amount = float(group.get("Metrics", {}).get(
                        "UnblendedCost", {}).get("Amount", "0"))
                    usage_qty = float(group.get("Metrics", {}).get(
                        "UsageQuantity", {}).get("Amount", "0"))

                    if cost_amount > 0.01:
                        keys = group["Keys"]
                        usage_type = keys[1] if len(keys) > 1 else "Unknown"

                        # Clean up usage type for better readability
                        display_usage = clean_usage_type(
                            usage_type, service_name)

                        resource_detail = {
                            "usage_type": display_usage,
                            "instance_type": "",  # Will get this separately
                            "cost": cost_amount,
                            "usage_qty": usage_qty,
                            "original_usage": usage_type
                        }
                        service_data["resource_details"].append(
                            resource_detail)

            # 3. Get instance type breakdown separately (for EC2, RDS, etc.)
            if service_name in [
                "Amazon Elastic Compute Cloud - Compute",
                "Amazon Relational Database Service",
                    "Amazon ElastiCache"]:
                try:
                    instance_response = ce.get_cost_and_usage(
                        TimePeriod={"Start": start_of_month, "End": tomorrow},
                        Granularity="MONTHLY",
                        Metrics=["UnblendedCost"],
                        GroupBy=[
                            {"Type": "DIMENSION", "Key": "SERVICE"},
                            {"Type": "DIMENSION", "Key": "INSTANCE_TYPE"}
                        ],
                        Filter={
                            "Dimensions": {
                                "Key": "SERVICE",
                                "Values": [service_name]
                            }
                        }
                    )

                    if instance_response.get("ResultsByTime"):
                        for group in instance_response["ResultsByTime"][0].get(
                                "Groups", []):
                            cost_amount = float(group.get("Metrics", {}).get(
                                "UnblendedCost", {}).get("Amount", "0"))
                            if cost_amount > 0.01:
                                instance_type = group["Keys"][1] if len(
                                    group["Keys"]) > 1 else ""
                                if instance_type:
                                    service_data["instance_types"].append(
                                        (instance_type, cost_amount))
                except Exception:
                    pass

            # 3. Get operation-level breakdown for some services
            if service_name in [
                "Amazon Simple Storage Service",
                "AWS Lambda",
                    "Amazon API Gateway"]:
                operation_response = ce.get_cost_and_usage(
                    TimePeriod={"Start": start_of_month, "End": tomorrow},
                    Granularity="MONTHLY",
                    Metrics=["UnblendedCost"],
                    GroupBy=[
                        {"Type": "DIMENSION", "Key": "SERVICE"},
                        {"Type": "DIMENSION", "Key": "OPERATION"}
                    ],
                    Filter={
                        "Dimensions": {
                            "Key": "SERVICE",
                            "Values": [service_name]
                        }
                    }
                )

                operations = []
                if operation_response.get("ResultsByTime"):
                    for group in operation_response["ResultsByTime"][0].get(
                            "Groups", []):
                        cost_amount = float(group.get("Metrics", {}).get(
                            "UnblendedCost", {}).get("Amount", "0"))
                        if cost_amount > 0.01:
                            operation = group["Keys"][1] if len(
                                group["Keys"]) > 1 else "Unknown"
                            operations.append((operation, cost_amount))

                service_data["operations"] = sorted(
                    operations, key=lambda x: x[1], reverse=True)[:5]

            # Sort and limit data
            service_data["regions"] = sorted(
                service_data["regions"], key=lambda x: x[1], reverse=True)[:4]
            service_data["resource_details"] = sorted(
                service_data["resource_details"],
                key=lambda x: x["cost"],
                reverse=True)[
                :5]
            service_data["instance_types"] = sorted(service_data.get(
                "instance_types", []), key=lambda x: x[1], reverse=True)[:4]

            detailed_breakdown[service_name] = service_data

        except Exception:
            continue

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
                    usage_display = f"{float(usage_qty):.1f}" if usage_qty and float(usage_qty) > 0 else "N/A"
                    regions_data.append(f"{region_name}: ${cost:.3f}({usage_display})")
                service_section += f"🌍 REGIONS: {' | '.join(regions_data)}\n\n"
            
            # Resource details - horizontal
            if details['resource_details']:
                resource_data = []
                for resource in details['resource_details'][:3]:
                    resource_name = resource['usage_type'][:15]  # Truncate for horizontal display
                    qty = resource['usage_qty']
                    if qty >= 1000:
                        qty_display = f"{qty/1000:.1f}K"
                    elif qty >= 1:
                        qty_display = f"{qty:.1f}"
                    elif qty > 0:
                        qty_display = f"{qty:.3f}"
                    else:
                        qty_display = "N/A"
                    resource_data.append(f"{resource_name}: ${resource['cost']:.3f}({qty_display})")
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
    try:
        actual, forecast, top_services = get_costs()
        est = pytz.timezone('US/Eastern')
        current_time_est = datetime.now(est)
        is_scheduled = is_scheduled_notification(event)
        is_threshold_exceeded = actual > THRESHOLD

        services_text = "\n".join([f"• {map_service_name(svc)}: ${amt:.2f}" for svc,
                                   amt in top_services[:6]]) if top_services else "• No significant costs yet"

        if is_threshold_exceeded and not is_scheduled:
            message = f"🚨 *AWS Cost Alert - Threshold Exceeded!*\n\n*Current Spend:* ${actual:.2f}\n*Monthly Forecast:* ${forecast:.2f}\n*Threshold:* ${THRESHOLD:.2f}\n\n*Top Services:*\n{services_text}\n\n_Alert sent at {current_time_est.strftime('%Y-%m-%d %I:%M %p EST')}_"
        elif is_scheduled:
            message = f"🔔 *Daily AWS Cost Update*\n\n*Current Month Spend:* ${actual:.2f}\n*Projected Month Total:* ${forecast:.2f}\n*Alert Threshold:* ${THRESHOLD:.2f}\n\n*Top Services This Month:*\n{services_text}\n\n_Daily report sent at {current_time_est.strftime('%Y-%m-%d %I:%M %p EST')}_"
        else:
            message = f"ℹ️ *AWS Cost Check*\n\n*Current Spend:* ${actual:.2f}\n*Monthly Forecast:* ${forecast:.2f}\n\n*Top Services:*\n{services_text}\n\n_Manual check at {current_time_est.strftime('%Y-%m-%d %I:%M %p EST')}_"

        # Send to Slack
        success = send_slack(message)

        return {
            "statusCode": 200,
            "body": {
                "status": "success" if success else "failed",
                "actual_cost": actual,
                "forecast": forecast,
                "threshold_exceeded": is_threshold_exceeded,
                "is_scheduled": is_scheduled,
                "timestamp": current_time_est.isoformat()
            }
        }

    except Exception as e:
        send_slack(f":x: *AWS Cost Alert Error*\n```{str(e)}```")
        return {"statusCode": 500, "body": {"status": "error", "message": str(e)}}
