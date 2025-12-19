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

                # Get top 3 services by cost
                services = sorted(
                    group_costs, key=lambda x: x[1], reverse=True)[:3]
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
    """Get detailed breakdown of resources for top services"""
    if not top_services:
        return {}

    now = datetime.now()
    start_of_month = now.replace(day=1).strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    detailed_breakdown = {}

    # Analyze top 2 services to avoid API limits and keep message readable
    for service_name, service_cost in top_services[:2]:
        if service_cost < 0.10:  # Skip services with very low costs
            continue

        try:
            # Get region-wise breakdown for this service
            region_response = ce.get_cost_and_usage(
                TimePeriod={"Start": start_of_month, "End": tomorrow},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
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

            regions = []
            if region_response.get("ResultsByTime"):
                for group in region_response["ResultsByTime"][0].get("Groups", []):
                    cost_amount = float(group.get("Metrics", {}).get(
                        "UnblendedCost", {}).get("Amount", "0"))
                    if cost_amount > 0.01:  # Only include regions with significant cost
                        region = group["Keys"][1] if len(
                            group["Keys"]) > 1 else "Unknown"
                        regions.append((region, cost_amount))

            # Get usage type breakdown for this service
            usage_response = ce.get_cost_and_usage(
                TimePeriod={"Start": start_of_month, "End": tomorrow},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
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

            usage_types = []
            if usage_response.get("ResultsByTime"):
                for group in usage_response["ResultsByTime"][0].get("Groups", []):
                    cost_amount = float(group.get("Metrics", {}).get(
                        "UnblendedCost", {}).get("Amount", "0"))
                    if cost_amount > 0.01:
                        usage_type = group["Keys"][1] if len(
                            group["Keys"]) > 1 else "Unknown"
                        # Clean up usage type names for better readability
                        usage_type = usage_type.replace(
                            f"{service_name}-", "").replace(":", " ")
                        usage_types.append((usage_type, cost_amount))

            detailed_breakdown[service_name] = {
                "total_cost": service_cost,
                # Top 3 regions
                "regions": sorted(regions, key=lambda x: x[1], reverse=True)[:3],
                # Top 4 usage types
                "usage_types": sorted(usage_types, key=lambda x: x[1], reverse=True)[:4]
            }

        except Exception as e:
            print(f"Error getting detailed breakdown for {service_name}: {e}")
            continue

    return detailed_breakdown


def format_detailed_breakdown(detailed_breakdown):
    """Format detailed cost breakdown for Slack message"""
    if not detailed_breakdown:
        return ""

    breakdown_text = "\n\n*🔍 Detailed Resource Breakdown:*\n"

    for service, details in detailed_breakdown.items():
        breakdown_text += f"\n*{service}* (${details['total_cost']:.2f}):\n"

        # Add region breakdown
        if details['regions']:
            breakdown_text += "  📍 *Regions:*\n"
            for region, cost in details['regions']:
                breakdown_text += f"    • {region}: ${cost:.2f}\n"

        # Add usage type breakdown
        if details['usage_types']:
            breakdown_text += "  🛠️ *Usage Types:*\n"
            for usage_type, cost in details['usage_types']:
                # Truncate long usage type names
                display_name = usage_type[:40] + \
                    "..." if len(usage_type) > 40 else usage_type
                breakdown_text += f"    • {display_name}: ${cost:.2f}\n"

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

        # Format top services
        services_text = "\n".join(
            [f"• {svc}: ${amt:.2f}" for svc, amt in top_services]) if top_services else "• No significant costs yet"

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
