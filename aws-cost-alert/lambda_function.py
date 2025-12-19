import boto3
import os
import requests
import json
from datetime import datetime, timedelta, timezone
import pytz

ce = boto3.client("ce")

# Slack Bot Token and Channel Configuration
SLACK_BOT_TOKEN = os.getenv(
    "SLACK_BOT_TOKEN",
    "xoxb-8538024246390-10163017103233-b4L515AxLdKfuAZ9pYaPuXK3")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "#recruiter-insights-ops")
THRESHOLD = 3.00  # Alert threshold in USD


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

            # Calculate actual cost from all groups (since Total is empty when grouping by SERVICE)
            if "Groups" in result:
                group_costs = []
                for group in result["Groups"]:
                    cost_amount = group.get("Metrics", {}).get(
                        "UnblendedCost", {}).get("Amount", "0")
                    if cost_amount != "0":
                        cost_float = float(cost_amount)
                        actual += cost_float
                        group_costs.append((group["Keys"][0], cost_float))

                # Get top 10 services by cost (increased from 3 to capture more services)
                services = sorted(
                    group_costs, key=lambda x: x[1], reverse=True)[:10]
                
                # Debug: Print all services with costs for troubleshooting
                print(f"All services with costs: {group_costs}")
    except (KeyError, IndexError, ValueError) as e:
        print(f"Error parsing cost data: {e}")
        # Continue with defaults

    # Get FORECAST for entire month
    try:
        forecast_response = ce.get_cost_forecast(
            TimePeriod={
                "Start": tomorrow,
                "End": (now.replace(month=now.month+1, day=1) if now.month < 12
                        else now.replace(year=now.year+1, month=1, day=1)).strftime("%Y-%m-%d"),
            },
            Metric="UNBLENDED_COST"
        )
        forecast = float(
            forecast_response["ForecastResultsByTime"][0]["MeanValue"])
        monthly_forecast = actual + forecast
    except Exception:
        # Fallback: estimate based on daily average
        days_in_month = (now.replace(month=now.month+1, day=1) if now.month < 12
                         else now.replace(year=now.year+1, month=1, day=1) - timedelta(days=1)).day
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

    # Analyze top 5 services with more detail (increased from 3)
    for service_name, service_cost in top_services[:5]:
        if service_cost < 0.01:  # Lowered threshold from $0.05 to $0.01 to capture smaller services
            continue
            
        print(f"Analyzing service: {service_name} with cost: ${service_cost:.2f}")

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
                    cost_amount = float(group.get("Metrics", {}).get("UnblendedCost", {}).get("Amount", "0"))
                    usage_qty = group.get("Metrics", {}).get("UsageQuantity", {}).get("Amount", "0")
                    
                    if cost_amount > 0.01:
                        region = group["Keys"][1] if len(group["Keys"]) > 1 else "Global"
                        # Convert region codes to readable names
                        region_name = get_region_name(region)
                        service_data["regions"].append((region_name, cost_amount, usage_qty))

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
                    cost_amount = float(group.get("Metrics", {}).get("UnblendedCost", {}).get("Amount", "0"))
                    usage_qty = float(group.get("Metrics", {}).get("UsageQuantity", {}).get("Amount", "0"))
                    
                    if cost_amount > 0.01:
                        keys = group["Keys"]
                        usage_type = keys[1] if len(keys) > 1 else "Unknown"
                        
                        # Clean up usage type for better readability
                        display_usage = clean_usage_type(usage_type, service_name)
                        
                        resource_detail = {
                            "usage_type": display_usage,
                            "instance_type": "",  # Will get this separately
                            "cost": cost_amount,
                            "usage_qty": usage_qty,
                            "original_usage": usage_type
                        }
                        service_data["resource_details"].append(resource_detail)

            # 3. Get instance type breakdown separately (for EC2, RDS, etc.)
            if service_name in ["Amazon Elastic Compute Cloud - Compute", "Amazon Relational Database Service", "Amazon ElastiCache"]:
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
                        for group in instance_response["ResultsByTime"][0].get("Groups", []):
                            cost_amount = float(group.get("Metrics", {}).get("UnblendedCost", {}).get("Amount", "0"))
                            if cost_amount > 0.01:
                                instance_type = group["Keys"][1] if len(group["Keys"]) > 1 else ""
                                if instance_type:
                                    service_data["instance_types"].append((instance_type, cost_amount))
                except Exception as e:
                    print(f"Error getting instance types for {service_name}: {e}")

            # 3. Get operation-level breakdown for some services
            if service_name in ["Amazon Simple Storage Service", "AWS Lambda", "Amazon API Gateway"]:
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
                    for group in operation_response["ResultsByTime"][0].get("Groups", []):
                        cost_amount = float(group.get("Metrics", {}).get("UnblendedCost", {}).get("Amount", "0"))
                        if cost_amount > 0.01:
                            operation = group["Keys"][1] if len(group["Keys"]) > 1 else "Unknown"
                            operations.append((operation, cost_amount))
                
                service_data["operations"] = sorted(operations, key=lambda x: x[1], reverse=True)[:5]

            # Sort and limit data
            service_data["regions"] = sorted(service_data["regions"], key=lambda x: x[1], reverse=True)[:4]
            service_data["resource_details"] = sorted(service_data["resource_details"], key=lambda x: x["cost"], reverse=True)[:5]
            service_data["instance_types"] = sorted(service_data.get("instance_types", []), key=lambda x: x[1], reverse=True)[:4]

            detailed_breakdown[service_name] = service_data

        except Exception as e:
            print(f"Error getting detailed breakdown for {service_name}: {e}")
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
        "Amazon Simple Storage Service": "Simple Storage Service",
        "AWS Key Management Service": "Key Management Service",
        "AWS Cost Explorer": "Cost Explorer",
        "AWS Lambda": "Lambda",
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
    return service_mapping.get(service_name, service_name.replace("Amazon ", "").replace("AWS ", ""))


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
        cleaned = cleaned.replace("Lambda-", "").replace("Request", "Requests").replace("Duration", "Execution Time")
    elif "Key Management" in service_name:
        cleaned = cleaned.replace("KMS-", "").replace("Keys", "Key Usage")
    elif "Cost Explorer" in service_name:
        cleaned = cleaned.replace("APIRequest", "API Requests")
    elif "EC2" in service_name:
        cleaned = cleaned.replace("BoxUsage", "Instance Hours").replace("EBS", "Storage")
    
    # General cleaning
    cleaned = cleaned.replace("-", " ").replace("_", " ")
    
    return cleaned if cleaned != usage_type else usage_type[:50]


def format_detailed_breakdown(detailed_breakdown):
    """Format detailed cost breakdown for Slack message in AWS console style"""
    if not detailed_breakdown:
        return ""

    breakdown_text = "\n\n*🔍 Detailed Resource Breakdown (AWS Console Style):*\n"

    for service, details in detailed_breakdown.items():
        # Service header with total cost - use mapped name
        service_display = map_service_name(service)
        breakdown_text += f"\n*💰 {service_display}* - Total: *${details['total_cost']:.2f}*\n"

        # Regional breakdown with usage quantity
        if details['regions']:
            breakdown_text += "  🌍 *Regional Distribution:*\n"
            for region_name, cost, usage_qty in details['regions']:
                usage_info = f" ({usage_qty} units)" if usage_qty and float(usage_qty) > 0 else ""
                breakdown_text += f"    📍 {region_name}: ${cost:.2f}{usage_info}\n"

        # Resource-level details (like AWS console shows)
        if details['resource_details']:
            breakdown_text += "  🛠️ *Resource Usage Types:*\n"
            for resource in details['resource_details']:
                usage_type = resource['usage_type']
                cost = resource['cost']
                usage_qty = resource['usage_qty']
                
                # Format like AWS console: Usage Type - Cost (Usage)
                resource_line = f"    • {usage_type}: ${cost:.2f}"
                if usage_qty > 0:
                    # Format usage quantity nicely
                    if usage_qty >= 1000:
                        resource_line += f" ({usage_qty/1000:.1f}K units)"
                    elif usage_qty >= 1:
                        resource_line += f" ({usage_qty:.1f} units)"
                    else:
                        resource_line += f" ({usage_qty:.3f} units)"
                
                breakdown_text += resource_line + "\n"

        # Instance types breakdown (for compute services)
        if details.get('instance_types'):
            breakdown_text += "  💻 *Instance Types:*\n"
            for instance_type, cost in details['instance_types']:
                breakdown_text += f"    • {instance_type}: ${cost:.2f}\n"

        # Operations breakdown for supported services
        if 'operations' in details and details['operations']:
            breakdown_text += "  ⚡ *Operations:*\n"
            for operation, cost in details['operations']:
                breakdown_text += f"    • {operation}: ${cost:.2f}\n"

        # Add separator between services
        breakdown_text += "\n"

    # Add helpful footer
    breakdown_text += "_💡 Resource costs are shown by region and usage type, similar to AWS console_\n"
    
    return breakdown_text


def send_slack(message):
    """Send message to Slack using Bot Token"""
    if not SLACK_BOT_TOKEN:
        print("SLACK_BOT_TOKEN not configured")
        return False

    url = "https://slack.com/api/chat.postMessage"
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "channel": SLACK_CHANNEL,
        "text": message,
        "username": "AWS Cost Alert Bot",
        "icon_emoji": ":money_with_wings:"
    }

    try:
        response = requests.post(url, headers=headers,
                                 json=payload, timeout=10)
        response.raise_for_status()

        result = response.json()
        if not result.get("ok"):
            print(f"Slack API error: {result.get('error', 'Unknown error')}")
            return False

        print(f"Message sent successfully to {SLACK_CHANNEL}")
        return True

    except Exception as e:
        print(f"Failed to send Slack message: {e}")
        return False


def is_scheduled_notification(event):
    """Check if this is a scheduled notification (EventBridge) or alert"""
    # EventBridge scheduled events have 'source': 'aws.events'
    return event.get('source') == 'aws.events' or event.get('detail-type') == 'Scheduled Event'


def lambda_handler(event, context):
    """Main Lambda handler"""
    try:
        # Get current costs
        actual, forecast, top_services = get_costs()

        # Get detailed breakdown for top services
        detailed_breakdown = get_detailed_cost_breakdown(top_services)
        detailed_text = format_detailed_breakdown(detailed_breakdown)

        # Get current time in EST
        est = pytz.timezone('US/Eastern')
        current_time_est = datetime.now(est)

        # Check if this is scheduled notification or threshold alert
        is_scheduled = is_scheduled_notification(event)
        is_threshold_exceeded = actual > THRESHOLD

        # Format top services with mapped names
        services_text = "\n".join(
            [f"• {map_service_name(svc)}: ${amt:.2f}" for svc, amt in top_services[:6]]) if top_services else "• No significant costs yet"

        if is_threshold_exceeded and not is_scheduled:
            # ALERT MESSAGE - Threshold exceeded
            icon = "🚨"
            title = "🚨 AWS Cost Alert - Threshold Exceeded!"
            message = f"""{icon} *{title}*

*Current Spend:* ${actual:.2f}
*Monthly Forecast:* ${forecast:.2f}
*Threshold:* ${THRESHOLD:.2f}

*Top Services:*
{services_text}{detailed_text}

_Alert sent at {current_time_est.strftime('%Y-%m-%d %I:%M %p EST')}_"""

        elif is_scheduled:
            # SCHEDULED NOTIFICATION - Daily updates
            icon = "🔔"
            title = "🔔 Daily AWS Cost Update"
            message = f"""{icon} *{title}*

*Current Month Spend:* ${actual:.2f}
*Projected Month Total:* ${forecast:.2f}
*Alert Threshold:* ${THRESHOLD:.2f}

*Top Services This Month:*
{services_text}{detailed_text}

_Daily report sent at {current_time_est.strftime('%Y-%m-%d %I:%M %p EST')}_"""

        else:
            # Manual invocation or other trigger
            icon = "ℹ️"
            title = "ℹ️ AWS Cost Check"
            message = f"""{icon} *{title}*

*Current Spend:* ${actual:.2f}
*Monthly Forecast:* ${forecast:.2f}

*Top Services:*
{services_text}{detailed_text}

_Manual check at {current_time_est.strftime('%Y-%m-%d %I:%M %p EST')}_"""

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
                "timestamp": current_time_est.isoformat(),
                "detailed_breakdown": detailed_breakdown
            }
        }

    except Exception as e:
        error_msg = f"Lambda execution failed: {str(e)}"
        print(error_msg)

        # Send error notification to Slack
        send_slack(f":x: *AWS Cost Alert Error*\n```{error_msg}```")

        return {
            "statusCode": 500,
            "body": {"status": "error", "message": str(e)}
        }
