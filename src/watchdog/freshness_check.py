"""
Missed-run watchdog (FR-027, research R11): an AWS Lambda run daily by EventBridge
Scheduler. It alerts if a provider's latest.json is missing or names a snapshot older than
MAX_SNAPSHOT_AGE_DAYS — whoever produces snapshots (Fargate or an off-AWS writer).

Independent of the pipeline image on purpose, so it still fires when the image is broken.
Standard library + boto3 only (Lambda python3.13 runtime).

Environment: DATA_BUCKET, DATA_PREFIX, PROVIDERS, MAX_SNAPSHOT_AGE_DAYS, ALERT_TOPIC_ARN,
ENVIRONMENT.
"""

import datetime as dt
import json
import os

import boto3
from botocore.exceptions import ClientError


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def _publish(topic_arn: str, subject: str, message: str) -> None:
    boto3.client("sns").publish(TopicArn=topic_arn, Subject=subject[:100], Message=message)


def _read_latest(s3, bucket: str, key: str):
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise


def handler(event, context):
    bucket = os.environ["DATA_BUCKET"]
    prefix = os.environ.get("DATA_PREFIX", "")
    providers = [p.strip() for p in os.environ.get("PROVIDERS", "aws").split(",") if p.strip()]
    max_age = int(os.environ.get("MAX_SNAPSHOT_AGE_DAYS", "8"))
    topic = os.environ["ALERT_TOPIC_ARN"]
    environment = os.environ.get("ENVIRONMENT", "prod")

    s3 = boto3.client("s3")
    today = _today()
    alerts = []
    for provider in providers:
        key = f"{prefix}{provider}/manifests/latest.json"
        latest = _read_latest(s3, bucket, key)
        if latest is None:
            problem = f"no latest.json at s3://{bucket}/{key}: no succeeded snapshot has been published"
        else:
            age = (today - dt.date.fromisoformat(latest["snapshot_date"])).days
            if age <= max_age:
                continue
            problem = (
                f"newest succeeded snapshot is {latest['snapshot_date']}, {age} days old "
                f"(window: {max_age} days)"
            )
        _publish(
            topic,
            f"[cloud-pricing {environment}] MISSED RUN: {provider}",
            f"MISSED RUN for {provider}: {problem}.\n"
            "Prices from missed weeks cannot be downloaded later. Check the schedule, the "
            "latest run's logs, or the off-AWS writer if one is in use.",
        )
        alerts.append(provider)
    return {"checked": providers, "alerts": alerts}
