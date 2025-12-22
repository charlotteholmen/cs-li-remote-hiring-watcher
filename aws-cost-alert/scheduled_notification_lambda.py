"""
AWS Cost Scheduled Notification Lambda
Triggers: EventBridge scheduled events
Purpose: Send daily cost reports at scheduled times
"""

from cost_utils import (
    get_costs, map_service_name, send_slack,
    get_current_time_est, get_api_call_count, reset_api_call_count
)


def is_scheduled_notification(event):
    """Check if this is a scheduled EventBridge notification"""
    return event.get('source') == 'aws.events' or event.get('detail-type') == 'Scheduled Event'


def lambda_handler(event, context):
    """Handle scheduled cost notifications"""
    reset_api_call_count()

    try:
        # Verify this is indeed a scheduled event
        if not is_scheduled_notification(event):
            return {
                "statusCode": 400,
                "body": {
                    "status": "error",
                    "message": "This lambda only handles scheduled notifications"
                }
            }

        # Get cost data
        actual, forecast, top_services = get_costs()
        current_time_est = get_current_time_est()

        # Format services text
        services_text = "\n".join([
            f"• {map_service_name(svc)}: ${amt:.2f}"
            for svc, amt in top_services[:6]
        ]) if top_services else "• No significant costs yet"

        # Create scheduled notification message
        message = (
            f"🔔 *Daily AWS Cost Update*\n\n"
            f"*Current Month Spend:* ${actual:.2f}\n"
            f"*Projected Month Total:* ${forecast:.2f}\n\n"
            f"*Top Services This Month:*\n{services_text}\n\n"
            f"_Daily report sent at {current_time_est.strftime('%Y-%m-%d %I:%M %p EST')}_"
        )

        # Send notification to Slack
        success = send_slack(message)

        # Log API usage summary
        api_calls = get_api_call_count()
        api_cost = api_calls * 0.01
        print(
            f"📊 API Usage Summary: {api_calls} calls, estimated cost: ${api_cost:.2f}")

        return {
            "statusCode": 200,
            "body": {
                "status": "success",
                "message_type": "scheduled_notification",
                "actual_cost": actual,
                "forecast": forecast,
                "api_calls_made": api_calls,
                "api_cost_estimate": api_cost,
                "slack_sent": success,
                "timestamp": current_time_est.isoformat()
            }
        }

    except Exception as e:
        error_message = f":x: *Scheduled Cost Report Error*\n```{str(e)}```"
        send_slack(error_message)
        print(f"Error in scheduled notification: {e}")

        return {
            "statusCode": 500,
            "body": {
                "status": "error",
                "message": str(e),
                "message_type": "scheduled_notification"
            }
        }
