import boto3
import requests
from datetime import datetime, timedelta

WEBHOOK_URL = "https://hooks.slack.com/services/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"


def lambda_handler(event, context):
    ce = boto3.client("ce")

    now = datetime.utcnow()
    start = now.replace(day=1).strftime("%Y-%m-%d")
    end = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    data = ce.get_cost_and_usage(
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )

    total_cost = float(data["ResultsByTime"][0]["Total"]
                       ["UnblendedCost"]["Amount"])

    # Top 5 services by cost
    groups = data["ResultsByTime"][0]["Groups"]
    services = sorted(
        [(g["Keys"][0], float(g["Metrics"]["UnblendedCost"]["Amount"]))
         for g in groups],
        key=lambda x: x[1],
        reverse=True,
    )[:5]

    msg = ":rotating_light: *AWS Cost Alert*\n"
    msg += f"*Monthly Spend:* ${total_cost:.2f}\n\n"
    msg += "*Top Services:*\n"
    for i, (svc, amt) in enumerate(services, 1):
        msg += f"{i}. {svc} — ${amt:.2f}\n"

    requests.post(WEBHOOK_URL, json={"text": msg})

    return {"status": "sent"}
