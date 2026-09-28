"""Missed-run watchdog Lambda (FR-027, research R11)."""

import datetime as dt
import json

import boto3
import pytest
from moto import mock_aws

from src.watchdog import freshness_check as fc

BUCKET = "test-data"


@pytest.fixture
def env(aws_env, monkeypatch):
    monkeypatch.setenv("DATA_BUCKET", BUCKET)
    monkeypatch.setenv("DATA_PREFIX", "")
    monkeypatch.setenv("PROVIDERS", "aws")
    monkeypatch.setenv("MAX_SNAPSHOT_AGE_DAYS", "8")
    monkeypatch.setenv("ALERT_TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:alerts")
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setattr(fc, "_today", lambda: dt.date(2026, 10, 14))
    published = []
    monkeypatch.setattr(fc, "_publish", lambda topic, subject, message: published.append((subject, message)))
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield published


def _put_latest(date, provider="aws", prefix=""):
    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket=BUCKET,
        Key=f"{prefix}{provider}/manifests/latest.json",
        Body=json.dumps({"snapshot_date": date, "provider": provider}),
    )


def test_missing_latest_alerts(env):
    result = fc.handler({}, None)
    assert result["alerts"] == ["aws"]
    assert env[0][0] == "[cloud-pricing prod] MISSED RUN: aws"
    assert "no latest.json" in env[0][1]


def test_stale_latest_alerts(env):
    _put_latest("2026-10-05")  # 9 days old, window 8
    fc.handler({}, None)
    assert len(env) == 1
    assert "9 days" in env[0][1]


def test_fresh_latest_is_quiet(env):
    _put_latest("2026-10-06")  # exactly 8 days: within the window
    result = fc.handler({}, None)
    assert result["alerts"] == []
    assert env == []


def test_each_provider_checked_independently(env, monkeypatch):
    monkeypatch.setenv("PROVIDERS", "aws,gcp")
    _put_latest("2026-10-12", provider="aws")
    result = fc.handler({}, None)
    assert result["alerts"] == ["gcp"]


def test_data_prefix_honored(env, monkeypatch):
    monkeypatch.setenv("DATA_PREFIX", "root/")
    _put_latest("2026-10-12", prefix="root/")
    assert fc.handler({}, None)["alerts"] == []
