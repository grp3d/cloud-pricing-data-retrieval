"""
PipelineSettings.from_env() — defaults, overrides and validation
(contracts/configuration.md, FR-035, FR-047).
"""

import pytest

from src.aws.regions import DEFAULT_PRICING_REGIONS
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


# --- feature 004: providers, locations, pacing, provider settings (T017) ---------------

from dataclasses import dataclass, field as dc_field  # noqa: E402

from src.pipeline import providers  # noqa: E402
from src.pipeline.config import ProviderSettings  # noqa: E402


@dataclass(frozen=True)
class StubSettings(ProviderSettings):
    SECRET_FIELDS = ("token",)
    endpoint: str = "https://stub.test"
    token: str = dc_field(default=None, repr=False)

    @classmethod
    def from_env(cls, env):
        token = env.get("STUB_TOKEN")
        if not token:
            raise SettingsError("STUB_TOKEN is required")
        return cls(endpoint=env.get("STUB_ENDPOINT", "https://stub.test"), token=token)


@pytest.fixture
def stub_provider(monkeypatch):
    spec = providers.ProviderSpec(
        name="stub",
        tables={"thing": 1},
        default_locations=("here-1",),
        downloader="x:y",
        transform="x:y",
        always_collected=("global",),
        rate_limit_per_second=2.0,
        settings="tests.unit.test_settings:StubSettings",
    )
    monkeypatch.setitem(providers.PROVIDERS, "stub", spec)
    return spec


@pytest.mark.parametrize("name", ["aws", "azure", "gcp"])
def test_provider_must_be_known(name, monkeypatch):
    monkeypatch.setitem(providers.PROVIDERS, name, providers.ProviderSpec(
        name=name, tables={"t": 1}, default_locations=("r-1",), downloader="x:y", transform="x:y"))
    assert _settings(PIPELINE_PROVIDER=name).provider == name


def test_unknown_provider_rejected():
    with pytest.raises(SettingsError):
        _settings(PIPELINE_PROVIDER="oracle")


def test_default_locations_come_from_the_provider(stub_provider):
    s = _settings(PIPELINE_PROVIDER="stub", STUB_TOKEN="t")
    assert s.regions == ["here-1"]


@pytest.mark.parametrize("value", ["global", "here-1,global"])
def test_always_collected_location_may_not_be_listed(stub_provider, value):
    with pytest.raises(SettingsError):
        _settings(PIPELINE_PROVIDER="stub", STUB_TOKEN="t", PRICING_REGIONS=value)


@pytest.mark.parametrize("value", ["East US", "a_b"])
def test_malformed_locations_rejected(value):
    with pytest.raises(SettingsError):
        _settings(PRICING_REGIONS=value)


def test_pacing_and_currency_defaults(stub_provider):
    aws = _settings()
    assert aws.pricing_download_rate_limit_per_second is None
    assert aws.pricing_download_max_concurrent_requests == 1
    assert aws.pricing_currency == "USD"
    stub = _settings(PIPELINE_PROVIDER="stub", STUB_TOKEN="t")
    assert stub.pricing_download_rate_limit_per_second == 2.0


def test_pacing_overrides(stub_provider):
    s = _settings(
        PIPELINE_PROVIDER="stub",
        STUB_TOKEN="t",
        PRICING_DOWNLOAD_RATE_LIMIT_PER_SECOND="0.25",
        PRICING_DOWNLOAD_MAX_CONCURRENT_REQUESTS="2",
        PRICING_CURRENCY="EUR",
    )
    assert s.pricing_download_rate_limit_per_second == 0.25
    assert s.pricing_download_max_concurrent_requests == 2
    assert s.pricing_currency == "EUR"


@pytest.mark.parametrize(
    "env",
    [
        {"PRICING_DOWNLOAD_RATE_LIMIT_PER_SECOND": "0"},
        {"PRICING_DOWNLOAD_RATE_LIMIT_PER_SECOND": "-1"},
        {"PRICING_DOWNLOAD_MAX_CONCURRENT_REQUESTS": "0"},
        {"PRICING_CURRENCY": "usd"},
    ],
)
def test_invalid_pacing_rejected(env):
    with pytest.raises(SettingsError):
        _settings(**env)


def test_provider_settings_are_built_by_the_spec_class(stub_provider):
    s = _settings(PIPELINE_PROVIDER="stub", STUB_TOKEN="SECRET-123", STUB_ENDPOINT="https://other.test")
    assert type(s.provider_settings).__name__ == "StubSettings"
    assert s.provider_settings.endpoint == "https://other.test"


def test_provider_settings_error_propagates(stub_provider):
    with pytest.raises(SettingsError):
        _settings(PIPELINE_PROVIDER="stub")


def test_aws_uses_the_base_provider_settings():
    assert type(_settings().provider_settings) is ProviderSettings


def test_secrets_are_redacted_and_never_in_repr(stub_provider):
    s = _settings(PIPELINE_PROVIDER="stub", STUB_TOKEN="SECRET-123")
    red = s.redacted()
    assert red["provider_settings"]["token"] == "***"
    assert red["provider_settings"]["endpoint"] == "https://stub.test"
    assert "SECRET-123" not in repr(s)
    assert "SECRET-123" not in str(red)
    assert s.secret_values() == ["SECRET-123"]


def test_unset_secret_is_shown_as_null():
    @dataclass(frozen=True)
    class Optional_(ProviderSettings):
        SECRET_FIELDS = ("token",)
        token: str = dc_field(default=None, repr=False)

    assert Optional_().redacted() == {"token": None}
    assert Optional_().secret_values() == []
