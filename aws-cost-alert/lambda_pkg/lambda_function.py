import boto3
import os
import requests
from datetime import datetime, timedelta, timezone
import pytz

ce = boto3.client("ce")

SLACK_WEBHOOK_URL = "https://hooks.slack.com/services/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"
THRESHOLD = 3.00  # Alert threshold in USD

def get_costs():
    """Get current month's actual costs and forecast"""
    # Get current date - Cost Explorer needs tomorrow as end date to include today
    now = datetime.now()
    start_of_month = now.replace(day=1).strftime("%Y-%m-%d")
    # Use tomorrow as end date to include today's costs
    from datetime import timedelta
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
                    cost_amount = group.get("Metrics", {}).get("UnblendedCost", {}).get("Amount", "0")
                    if cost_amount != "0":
                        cost_float = float(cost_amount)
                        actual += cost_float
                        group_costs.append((group["Keys"][0], cost_float))
                
                # Get top 3 services by cost
                services = sorted(group_costs, key=lambda x: x[1], reverse=True)[:3]
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
        forecast = float(forecast_response["ForecastResultsByTime"][0]["MeanValue"])
        monthly_forecast = actual + forecast
    except Exception:
        # Fallback: estimate based on daily average
        days_in_month = (now.replace(month=now.month+1, day=1) if now.month < 12 
                        else now.replace(year=now.year+1, month=1, day=1) - timedelta(days=1)).day
        days_elapsed = now.day
        daily_avg = actual / days_elapsed if days_elapsed > 0 else 0
        monthly_forecast = daily_avg * days_in_month

    return actual, monthly_forecast, services


def send_slack(message):
    """Send message to Slack"""
    payload = {"text": message}
    try:
        response = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        response.raise_for_status()
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
        
        # Get current time in EST
        est = pytz.timezone('US/Eastern')
        current_time_est = datetime.now(est)
        
        # Check if this is scheduled notification or threshold alert
        is_scheduled = is_scheduled_notification(event)
        is_threshold_exceeded = actual > THRESHOLD
        
        # Format top services
        services_text = "\n".join([f"• {svc}: ${amt:.2f}" for svc, amt in top_services]) if top_services else "• No significant costs yet"
        
        if is_threshold_exceeded and not is_scheduled:
            # ALERT MESSAGE - Threshold exceeded
            icon = ":rotating_light:"
            title = "🚨 AWS Cost Alert - Threshold Exceeded!"
            color = "danger"
            message = f"""{icon} *{title}*

*Current Spend:* ${actual:.2f}
*Monthly Forecast:* ${forecast:.2f}
*Threshold:* ${THRESHOLD:.2f}

*Top Services:*
{services_text}

_Alert sent at {current_time_est.strftime('%Y-%m-%d %I:%M %p EST')}_"""

        elif is_scheduled:
            # SCHEDULED NOTIFICATION - Daily updates
            icon = ":chart_with_upwards_trend:"
            title = "📊 Daily AWS Cost Update"
            message = f"""{icon} *{title}*

*Current Month Spend:* ${actual:.2f}
*Projected Month Total:* ${forecast:.2f}
*Alert Threshold:* ${THRESHOLD:.2f}

*Top Services This Month:*
{services_text}

_Daily report sent at {current_time_est.strftime('%Y-%m-%d %I:%M %p EST')}_"""

        else:
            # Manual invocation or other trigger
            icon = ":information_source:"
            title = "AWS Cost Check"
            message = f"""{icon} *{title}*

*Current Spend:* ${actual:.2f}
*Monthly Forecast:* ${forecast:.2f}

*Top Services:*
{services_text}

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
                "timestamp": current_time_est.isoformat()
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