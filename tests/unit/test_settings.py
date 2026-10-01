"""
PipelineSettings.from_env() — defaults, overrides and validation
(contracts/configuration.md, FR-035, FR-047).
"""

import pytest

from src.aws_regions import DEFAULT_PRICING_REGIONS
from src.pipeline.config import PipelineSettings, SettingsError


def _settings(**env):
    return PipelineSettings.from_env(env)


def test_defaults_match_configuration_contract():
    s = _settings()
    assert s.storage_uri == "file://./pipeline"
    assert s.provider == "aws"
    assert s.environment == "local"
    assert s.regions == DEFAULT_PRICING_REGIONS
    assert s.max_raw_download_workers == 2
    assert s.transform_concurrency == 1
    assert s.pricing_download_retry_max_retries == 3
    assert s.pricing_download_retry_strategy == "exponential_backoff"
    assert s.pricing_download_retry_base_delay_seconds == 30
    assert s.run_timeout_minutes == 120
    assert s.run_claim_ttl_minutes == 180
    assert s.raw_retention_days == 30
    assert s.parquet_weekly_retention_months == 12
    assert s.superseded_file_grace_minutes == 5
    assert s.superseded_file_inline_wait_max_minutes == 5
    assert s.alert_topic_arn is None
    assert s.success_summary_enabled is False
    assert s.host_label == "local"
    assert s.git_sha == "local"
    assert s.image_tag == "local"
    assert s.max_clock_skew_seconds == 300


def test_default_storage_uri_follows_data_directory_root():
    s = _settings(DATA_DIRECTORY_ROOT="/srv/data")
    assert s.storage_uri == "file:///srv/data/pipeline"


def test_env_overrides():
    s = _settings(
        PIPELINE_STORAGE_URI="s3://bucket/prefix",
        PIPELINE_ENVIRONMENT="prod",
        PRICING_REGIONS="us-east-1, eu-west-1",
        PRICING_DOWNLOAD_RETRY_MAX_RETRIES="0",
        PRICING_DOWNLOAD_RETRY_STRATEGY="linear_backoff",
        PRICING_DOWNLOAD_RETRY_BASE_DELAY_SECONDS="1.5",
        SUPERSEDED_FILE_GRACE_MINUTES="0",
        ALERT_TOPIC_ARN="arn:aws:sns:us-east-1:123456789012:alerts",
        SUCCESS_SUMMARY_ENABLED="true",
        PIPELINE_HOST_LABEL="home-server",
        GIT_SHA="abc1234",
        IMAGE_TAG="abc1234",
    )
    assert s.storage_uri == "s3://bucket/prefix"
    assert s.environment == "prod"
    assert s.regions == ["us-east-1", "eu-west-1"]
    assert s.pricing_download_retry_max_retries == 0
    assert s.pricing_download_retry_strategy == "linear_backoff"
    assert s.pricing_download_retry_base_delay_seconds == 1.5
    assert s.superseded_file_grace_minutes == 0
    assert s.alert_topic_arn.endswith(":alerts")
    assert s.success_summary_enabled is True
    assert s.host_label == "home-server"
    assert s.git_sha == "abc1234"


@pytest.mark.parametrize(
    "env",
    [
        {"PRICING_DOWNLOAD_RETRY_STRATEGY": "bogus"},
        {"PRICING_DOWNLOAD_RETRY_MAX_RETRIES": "-1"},
        {"PRICING_DOWNLOAD_RETRY_BASE_DELAY_SECONDS": "-2"},
        {"SUPERSEDED_FILE_GRACE_MINUTES": "-1"},
        {"SUPERSEDED_FILE_INLINE_WAIT_MAX_MINUTES": "-1"},
        {"RUN_CLAIM_TTL_MINUTES": "120", "RUN_TIMEOUT_MINUTES": "120"},
        {"RAW_RETENTION_DAYS": "0"},
        {"PARQUET_WEEKLY_RETENTION_MONTHS": "0"},
        {"PIPELINE_STORAGE_URI": "gs://bucket"},
        {"PIPELINE_PROVIDER": "AWS!"},
        {"PIPELINE_HOST_LABEL": "Home Server"},
        {"MAX_RAW_DOWNLOAD_WORKERS": "0"},
        {"TRANSFORM_CONCURRENCY": "0"},
        {"RUN_TIMEOUT_MINUTES": "abc"},
        {"SUCCESS_SUMMARY_ENABLED": "maybe"},
        {"MAX_CLOCK_SKEW_SECONDS": "-1"},
    ],
)
def test_invalid_settings_raise(env):
    with pytest.raises(SettingsError):
        _settings(**env)


def test_redacted_hides_alert_topic():
    s = _settings(ALERT_TOPIC_ARN="arn:aws:sns:us-east-1:123456789012:alerts")
    red = s.redacted()
    assert red["alert_topic_arn"] == "***"
    assert red["pricing_download_retry_strategy"] == "exponential_backoff"
