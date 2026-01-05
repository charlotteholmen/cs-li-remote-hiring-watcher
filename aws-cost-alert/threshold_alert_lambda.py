"""
AWS Cost Threshold Alert Lambda  
Triggers: SNS notifications from AWS Budgets
Purpose: Send immediate alerts when cost thresholds are exceeded
"""

import json
from cost_utils import (
    get_costs, get_budget_threshold, get_exceeded_threshold,
    map_service_name, send_slack, get_current_time_est,
    get_api_call_count, reset_api_call_count
)


def parse_sns_message(event):
    """Parse SNS message to extract budget alert information"""
    try:
        # Check if this is an SNS trigger
        if 'Records' not in event or not event['Records']:
            return None

        record = event['Records'][0]
        if record.get('EventSource') != 'aws:sns':
            return None

        # Parse SNS message
        sns_message = json.loads(record['Sns']['Message'])
        return sns_message
    except Exception as e:
        print(f"Error parsing SNS message: {e}")
        return None


def extract_budget_info(sns_message):
    """Extract budget threshold information from SNS message"""
    try:
        # AWS Budget notification typically contains these fields
        alert_type = sns_message.get('AlarmName', 'Budget Alert')
        threshold_type = sns_message.get('NewStateReason', '')

        # Try to extract threshold amount from the message
        message_text = sns_message.get(
            'NewStateReason', '') or sns_message.get('Subject', '')

        return {
            'alert_type': alert_type,
            'threshold_type': threshold_type,
            'raw_message': message_text
        }
    except Exception as e:
        print(f"Error extracting budget info: {e}")
        return None


def lambda_handler(event, context):
    """Handle cost threshold alerts from SNS"""
    reset_api_call_count()

    try:
        # Parse the SNS message
        sns_message = parse_sns_message(event)
        if not sns_message:
            return {
                "statusCode": 400,
                "body": {
                    "status": "error",
                    "message": "This lambda only handles SNS budget alerts"
                }
            }

        # Extract budget information
        budget_info = extract_budget_info(sns_message)

        # Get current cost data
        actual, forecast, top_services = get_costs()
        current_time_est = get_current_time_est()

        # Get budget thresholds
        budget_thresholds = get_budget_threshold()
        exceeded_threshold = get_exceeded_threshold(
            actual, budget_thresholds) if budget_thresholds else None

        # Debug logging for threshold detection
        exceeded_thresholds_list = [t for t in (
            budget_thresholds or []) if actual > t]
        print(f"🎯 Threshold Analysis:")
        print(f"  Current cost: ${actual:.2f}")
        print(f"  Budget thresholds: {budget_thresholds}")
        print(f"  All exceeded thresholds: {exceeded_thresholds_list}")
        print(
            f"  Highest exceeded: ${exceeded_threshold:.2f}" if exceeded_threshold else "  Highest exceeded: None")

        # Format services text
        services_text = "\n".join([
            f"• {map_service_name(svc)}: ${amt:.2f}"
            for svc, amt in top_services[:6]
        ]) if top_services else "• No significant costs yet"

        # Determine alert threshold and create message
        if exceeded_threshold:
            threshold_info = f"Exceeded Threshold: ${exceeded_threshold:.2f}"

            # Show all exceeded thresholds for context
            if len(exceeded_thresholds_list) > 1:
                all_exceeded = ", ".join(
                    [f"${t:.2f}" for t in exceeded_thresholds_list])
                threshold_info += f"\n*All Exceeded:* {all_exceeded}"
        elif budget_thresholds:
            thresholds_text = ", ".join(
                [f"${t:.2f}" for t in budget_thresholds])
            threshold_info = f"Budget Thresholds: {thresholds_text}"
        else:
            threshold_info = "Budget thresholds not available"

        # Create threshold alert message
        message = (
            f"🚨 *COST THRESHOLD ALERT*\n\n"
            f"*Current Spend:* ${actual:.2f}\n"
            f"*Monthly Forecast:* ${forecast:.2f}\n"
            f"*{threshold_info}*\n\n"
            f"*Top Services:*\n{services_text}\n\n"
        )

        # Add budget alert details if available
        if budget_info:
            message += f"*Budget Alert:* {budget_info.get('alert_type', 'N/A')}\n"

        message += f"_Alert triggered at {current_time_est.strftime('%Y-%m-%d %I:%M %p EST')}_"

        # Send alert to Slack using environment variables
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
                "message_type": "threshold_alert",
                "actual_cost": actual,
                "forecast": forecast,
                "exceeded_threshold": exceeded_threshold,
                "all_exceeded_thresholds": exceeded_thresholds_list,
                "api_calls_made": api_calls,
                "api_cost_estimate": api_cost,
                "slack_sent": success,
                "budget_alert_info": budget_info,
                "timestamp": current_time_est.isoformat()
            }
        }

    except Exception as e:
        try:
            error_message = f":x: *Cost Threshold Alert Error*\n```{str(e)}```"
            send_slack(error_message)
        except Exception as send_error:
            print(f"Failed to send error alert: {send_error}")
        print(f"Error in threshold alert: {e}")

        return {
            "statusCode": 500,
            "body": {
                "status": "error",
                "message": str(e),
                "message_type": "threshold_alert"
            }
        }
