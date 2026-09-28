"""Alert and summary messages (FR-026, FR-028, research R12, contracts/configuration.md)."""

import json

import boto3
import pytest

from src.pipeline.config import PipelineSettings
from src.pipeline.notify import (
    RETENTION_ERROR,
    RUN_CRASHED,
    RUN_FAILED,
    RUN_PARTIAL,
    RUN_REFUSED,
    RUN_SUCCEEDED,
    Notifier,
    alert_kinds_for,
)
from src.pipeline.runner import RunReport


def _report(**kw):
    base = dict(
        run_id="20261005T130000Z-111111",
        trigger="scheduled",
        mode="full",
        snapshot_date="2026-10-05",
        regions=["us-east-1", "eu-west-1"],
        outcome="completed",
        snapshot_status="succeeded",
        region_results=[
            {"region": "us-east-1", "outcome": "succeeded", "attempts": 1},
            {"region": "eu-west-1", "outcome": "succeeded", "attempts": 2},
        ],
        row_counts={"price_fact": 1234},
        duration_seconds=612.5,
    )
    base.update(kw)
    return RunReport(**base)


def _settings(**env):
    return PipelineSettings.from_env({"PIPELINE_ENVIRONMENT": "prod", **env})


@pytest.mark.parametrize(
    "report,summary,expected",
    [
        (_report(), False, []),
        (_report(), True, [RUN_SUCCEEDED]),
        (_report(snapshot_status="partial"), False, [RUN_PARTIAL]),
        (_report(snapshot_status="failed", row_counts={}), False, [RUN_FAILED]),
        (
            _report(region_results=[{"region": "us-east-1", "outcome": "failed", "attempts": 4}]),
            True,
            [RUN_PARTIAL],
        ),
        (_report(outcome="refused", snapshot_status=None, trigger="scheduled"), False, [RUN_REFUSED]),
        (_report(outcome="refused", snapshot_status=None, trigger="manual"), False, []),
        (_report(retention={"error": "boom"}), False, [RETENTION_ERROR]),
    ],
)
def test_alert_kinds(report, summary, expected):
    assert alert_kinds_for(report, _settings(SUCCESS_SUMMARY_ENABLED=str(summary))) == expected


def test_log_only_without_topic(caplog):
    notifier = Notifier.from_settings(_settings())
    with caplog.at_level("WARNING"):
        assert notifier.send(RUN_PARTIAL, _report(snapshot_status="partial")) is True
    assert "RUN PARTIAL" in caplog.text
    assert notifier.sent == [RUN_PARTIAL]


def test_subject_and_body_format():
    notifier = Notifier.from_settings(_settings())
    subject, body = notifier.format(RUN_PARTIAL, _report(snapshot_status="partial"))
    assert subject == "[cloud-pricing prod] RUN PARTIAL: aws 2026-10-05"
    assert "eu-west-1" in body and "attempts" in body
    assert json.loads(body.split("\n---\n", 1)[1])["run_report"]["run_id"] == "20261005T130000Z-111111"


def test_subject_truncated_to_sns_limit():
    notifier = Notifier.from_settings(_settings(PIPELINE_ENVIRONMENT="a" * 32))
    subject, _ = notifier.format(RUN_CRASHED, _report(snapshot_date="2026-10-05"), detail="x" * 200)
    assert len(subject) <= 100


def test_publishes_to_sns(aws_env):
    from moto import mock_aws

    with mock_aws():
        topic = boto3.client("sns", region_name="us-east-1").create_topic(Name="alerts")["TopicArn"]
        notifier = Notifier.from_settings(_settings(ALERT_TOPIC_ARN=topic))
        published = []
        real = notifier._client.publish

        def spy(**kwargs):
            published.append(kwargs)
            return real(**kwargs)

        notifier._client.publish = spy
        assert notifier.send(RUN_FAILED, _report(snapshot_status="failed")) is True
        assert published[0]["TopicArn"] == topic
        assert published[0]["Subject"].startswith("[cloud-pricing prod] RUN FAILED")


def test_publish_failure_is_logged_not_raised(caplog):
    notifier = Notifier.from_settings(_settings(ALERT_TOPIC_ARN="arn:aws:sns:us-east-1:123456789012:x"))

    class Boom:
        def publish(self, **kwargs):
            raise RuntimeError("sns down")

    notifier._client = Boom()
    with caplog.at_level("ERROR"):
        assert notifier.send(RUN_FAILED, _report(snapshot_status="failed")) is False
    assert "sns down" in caplog.text
