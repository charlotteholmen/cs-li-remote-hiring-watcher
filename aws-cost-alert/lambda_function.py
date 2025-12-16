import json
import os
from datetime import datetime, timezone

import boto3
import requests
from botocore.exceptions import BotoCoreError, ClientError

# Environment-driven configuration; avoid hardcoding secrets
WEBHOOK_URL = "https://hooks.slack.com/services/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"
THRESHOLD_AMOUNT = float(os.getenv("THRESHOLD_AMOUNT", "3.00"))


def lambda_handler(event, context):
    if not WEBHOOK_URL:
        # Do not proceed without the webhook; helps avoid accidental leakage
        return {"status": "error", "reason": "missing_webhook"}

    ce = boto3.client("ce")

    # Determine month start → today (UTC)
    today = datetime.now(timezone.utc).date()
    start_date = today.replace(day=1).isoformat()
    end_date = today.isoformat()

    try:
        response = ce.get_cost_and_usage(
            TimePeriod={"Start": start_date, "End": end_date},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
    except (BotoCoreError, ClientError) as exc:
        return {"status": "error", "reason": "cost_explorer_failed", "detail": str(exc)}

    results = response.get("ResultsByTime", [])
    if not results:
        return {"status": "error", "reason": "no_cost_data"}

    period = results[0]
    try:
        amount = float(period["Total"]["UnblendedCost"]["Amount"])
    except (KeyError, TypeError, ValueError) as exc:
        return {"status": "error", "reason": "invalid_cost_amount", "detail": str(exc)}

    groups = period.get("Groups", [])

    # Top 5 AWS services
    services = sorted(
        [
            (g["Keys"][0], float(g["Metrics"]["UnblendedCost"]["Amount"]))
            for g in groups
            if g.get("Keys") and g.get("Metrics")
        ],
        key=lambda x: x[1],
        reverse=True,
    )[:5]

    top_services = "\n".join(
        [f"{i+1}. {service} — ${cost:.2f}" for i, (service, cost) in enumerate(services)]
    ) or "No service breakdown available."

    # 🚨 vs 🔔 based on threshold
    if amount >= THRESHOLD_AMOUNT:
        emoji = "🚨"
        title = f"{emoji} AWS Monthly Cost Alert Triggered"
    else:
        emoji = "🔔"
        title = f"{emoji} AWS Monthly Cost Notification"

    # Slack message format
    slack_text = (
        f"{title}\n"
        f"Actual Spend: ${amount:.2f}\n"
        f"Top Services:\n{top_services}"
    )

    payload = {"text": slack_text}

    try:
        resp = requests.post(WEBHOOK_URL, data=json.dumps(payload), timeout=5)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return {"status": "error", "reason": "slack_post_failed", "detail": str(exc)}

    print(f"Slack alert sent at {datetime.now(timezone.utc)} | Amount: {amount}")

    return {"status": "ok", "amount": amount}
