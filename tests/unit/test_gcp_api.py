"""
GCP settings, paging and the catalog client (research R4, R5; FR-003, FR-006, FR-022,
FR-031, FR-034; feature 004 T030). Test-first, offline through FakeHttpSource.
"""

import os

import pytest

from src.gcp.api import GcpCatalogClient, listing_urls
from src.gcp.config import GcpSettings
from src.gcp.http_pages import GcpPagedSource
from src.pipeline.config import PipelineSettings, SettingsError
from src.pipeline.http_pages import PermanentSourceError, SourceError
from src.pipeline.retry import RetryPolicy
from tests.helpers.fake_pricing import GCP_FIXTURES, FakeHttpSource, http_error, http_timeout

KEY = "TEST-KEY-7f3a9c"
API = "https://cloudbilling.googleapis.com"


def _settings(**env):
    base = {
        "PIPELINE_PROVIDER": "gcp",
        "GCP_PRICING_API_KEY": KEY,
        "PRICING_DOWNLOAD_RETRY_BASE_DELAY_SECONDS": "0",
        "PRICING_DOWNLOAD_RATE_LIMIT_PER_SECOND": "1000",
    }
    base.update(env)
    return PipelineSettings.from_env(base)


def _fx(name):
    return os.path.join(GCP_FIXTURES, name)


def catalog_routes(settings, fake=None):
    fake = fake or FakeHttpSource()
    urls = listing_urls(settings)
    fake.route(urls["services"], _fx("services-p0001.json"))
    fake.route(urls["skus"], _fx("skus-p0001.json"))
    fake.route(urls["skus"] + "&pageToken=fixture-skus-p0002", _fx("skus-p0002.json"))
    fake.route(urls["prices"], _fx("prices-p0001.json"))
    fake.route(urls["prices"] + "&pageToken=fixture-prices-p0002", _fx("prices-p0002.json"))
    return fake


# --- settings ------------------------------------------------------------------------


def test_settings_defaults_and_secret():
    s = _settings().provider_settings
    assert isinstance(s, GcpSettings)
    assert s.billing_api_url == API
    assert s.billing_api_version == "v2beta"
    assert s.page_size == 5000
    assert s.business_entities == ("businessEntities/GCP",)
    assert GcpSettings.SECRET_FIELDS == ("api_key",)
    assert KEY not in repr(s)
    assert s.redacted()["api_key"] == "***"


def test_settings_overrides():
    s = _settings(
        GCP_BILLING_API_URL="https://example.test/",
        GCP_BILLING_API_VERSION="v2",
        GCP_BILLING_PAGE_SIZE="100",
        GCP_BILLING_BUSINESS_ENTITIES="businessEntities/GCP, businessEntities/Maps",
    ).provider_settings
    assert s.billing_api_url == "https://example.test"
    assert s.billing_api_version == "v2"
    assert s.page_size == 100
    assert s.business_entities == ("businessEntities/GCP", "businessEntities/Maps")


@pytest.mark.parametrize(
    "env",
    [{"GCP_PRICING_API_KEY": ""}, {"GCP_BILLING_PAGE_SIZE": "0"}, {"GCP_BILLING_PAGE_SIZE": "5001"},
     {"GCP_BILLING_API_URL": "http://insecure.test"}],
)
def test_settings_rejected(env):
    with pytest.raises(SettingsError):
        _settings(**env)


def test_key_is_ignored_for_other_providers():
    s = PipelineSettings.from_env({"PIPELINE_PROVIDER": "azure", "GCP_PRICING_API_KEY": KEY})
    assert KEY not in repr(s) and KEY not in str(s.redacted())
    assert s.secret_values() == []


# --- paged source --------------------------------------------------------------------


def _source(fake):
    return GcpPagedSource(KEY, fetch=fake, retry_policy=RetryPolicy(max_retries=3, strategy="fixed", base_delay_seconds=0),
                          sleep=lambda s: None)


def test_follows_page_tokens_with_the_key_only_in_a_header():
    settings = _settings()
    fake = catalog_routes(settings)
    pages = list(_source(fake).pages(listing_urls(settings)["skus"]))
    assert [p.number for p in pages] == [1, 2]
    for url, headers in fake.requests:
        assert headers == {"X-goog-api-key": KEY}
        assert KEY not in url


def test_failed_page_resumes_from_its_own_token():
    settings = _settings()
    second = listing_urls(settings)["skus"] + "&pageToken=fixture-skus-p0002"
    fake = catalog_routes(settings).fail(second, http_timeout(1))
    list(_source(fake).pages(listing_urls(settings)["skus"]))
    assert [u.endswith("pageToken=fixture-skus-p0002") for u in fake.urls()] == [False, True, True]


def test_repeated_page_token_fails_instead_of_looping():
    settings = _settings()
    second = listing_urls(settings)["skus"] + "&pageToken=fixture-skus-p0002"
    fake = catalog_routes(settings).route(second, b'{"skus": [], "nextPageToken": "fixture-skus-p0002"}')
    src = _source(fake)
    with pytest.raises(SourceError) as err:
        list(src.pages(listing_urls(settings)["skus"]))
    assert "links back" in str(err.value)
    assert src.stats.requests_made == 1 + 4


@pytest.mark.parametrize("reason", ["API_KEY_INVALID", "PERMISSION_DENIED", "API_KEY_SERVICE_BLOCKED"])
def test_rejected_key_is_permanent_and_redacted(reason):
    settings = _settings()
    body = f'{{"error": {{"status": "{reason}", "message": "key {KEY} rejected"}}}}'.encode()
    fake = catalog_routes(settings).fail(listing_urls(settings)["services"], http_error(400, body=body))
    source = _source(fake)
    with pytest.raises(PermanentSourceError) as err:
        list(source.pages(listing_urls(settings)["services"]))
    assert reason in str(err.value)
    assert KEY not in str(err.value)
    assert source.stats.requests_made == 1


def test_expired_page_token_restarts_the_listing_once():
    settings = _settings()
    second = listing_urls(settings)["skus"] + "&pageToken=fixture-skus-p0002"
    expired = b'{"error": {"status": "INVALID_ARGUMENT", "message": "Invalid page token."}}'
    fake = catalog_routes(settings).fail(second, http_error(400, body=expired, times=1))
    source = _source(fake)
    assert [p.number for p in source.pages(listing_urls(settings)["skus"])] == [1, 1, 2]
    assert source.stats.restarts == 1


# --- client --------------------------------------------------------------------------


def test_listing_urls():
    urls = listing_urls(_settings())
    assert urls["services"] == f"{API}/v1/services?pageSize=5000"
    assert urls["skus"] == f"{API}/v2beta/skus?pageSize=5000"
    assert urls["prices"] == f"{API}/v2beta/skus/-/prices?pageSize=5000&currencyCode=USD"


def test_downloads_the_three_listings(tmp_path):
    settings = _settings()
    fake = catalog_routes(settings)
    out = GcpCatalogClient(settings, fetch=fake, sleep=lambda s: None).download_catalog(str(tmp_path))
    assert out.error is None
    assert [os.path.basename(f) for f in out.files] == [
        "services-p0001.json", "skus-p0001.json", "skus-p0002.json", "prices-p0001.json", "prices-p0002.json",
    ]
    assert out.api_version == "v2beta"
    assert out.counters == {"requests_made": 5, "throttled_responses": 0}
    with open(out.files[1], "rb") as fh, open(_fx("skus-p0001.json"), "rb") as want:
        assert fh.read() == want.read()


def test_throttling_is_counted(tmp_path):
    settings = _settings()
    fake = catalog_routes(settings).fail(listing_urls(settings)["prices"], http_error(429, headers={"Retry-After": "0"}, times=1))
    out = GcpCatalogClient(settings, fetch=fake, sleep=lambda s: None).download_catalog(str(tmp_path))
    assert out.error is None
    assert out.counters == {"requests_made": 6, "throttled_responses": 1}


def test_page_that_keeps_failing_fails_the_catalog(tmp_path):
    settings = _settings()
    second = listing_urls(settings)["prices"] + "&pageToken=fixture-prices-p0002"
    fake = catalog_routes(settings).fail(second, http_error(503))
    out = GcpCatalogClient(settings, fetch=fake, sleep=lambda s: None).download_catalog(str(tmp_path))
    assert out.error and "503" in out.error
    assert not out.succeeded
