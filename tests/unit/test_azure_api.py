"""
Azure settings, paging and the Retail Prices client (research R1–R3; FR-002, FR-022,
FR-032, FR-034, FR-035; feature 004 T023). Test-first, offline through FakeHttpSource.
"""

import json
import os
from urllib.parse import parse_qs, urlsplit

import pytest

from src.azure.api import AzurePricesClient, filter_values, listing_url
from src.azure.config import AzureSettings
from src.azure.http_pages import AzurePagedSource
from src.pipeline.config import PipelineSettings, SettingsError
from src.pipeline.http_pages import SourceError
from src.pipeline.retry import RetryPolicy
from tests.helpers.fake_pricing import AZURE_FIXTURES, FakeHttpSource, http_error, http_timeout

API = "https://prices.azure.com/api/retail/prices"
EASTUS_P2 = (
    "https://prices.azure.com:443/api/retail/prices?api-version=2023-01-01-preview"
    "&$filter=armRegionName%20eq%20%27eastus%27&$skip=1000"
)


def _settings(**env):
    base = {
        "PIPELINE_PROVIDER": "azure",
        "PRICING_DOWNLOAD_RETRY_BASE_DELAY_SECONDS": "0",
        "PRICING_DOWNLOAD_RATE_LIMIT_PER_SECOND": "1000",
    }
    base.update(env)
    return PipelineSettings.from_env(base)


def _fx(name):
    return os.path.join(AZURE_FIXTURES, name)


def _filter_of(url):
    return parse_qs(urlsplit(url).query)["$filter"][0]


def eastus_routes(fake, settings=None):
    settings = settings or _settings()
    fake.route(listing_url(settings, "eastus"), _fx("eastus-p0001.json"))
    fake.route(EASTUS_P2, _fx("eastus-p0002.json"))
    return fake


# --- settings ------------------------------------------------------------------------


def test_settings_defaults():
    s = _settings().provider_settings
    assert isinstance(s, AzureSettings)
    assert s.prices_api_url == API
    assert s.prices_api_version == "2023-01-01-preview"
    assert s.include_devtest_prices is True
    assert AzureSettings.SECRET_FIELDS == ()


def test_settings_overrides():
    s = _settings(
        AZURE_PRICES_API_URL="https://example.test/prices/",
        AZURE_PRICES_API_VERSION="2024-01-01",
        INCLUDE_AZURE_DEVTEST_PRICES="false",
    ).provider_settings
    assert s.prices_api_url == "https://example.test/prices"
    assert s.prices_api_version == "2024-01-01"
    assert s.include_devtest_prices is False


@pytest.mark.parametrize("env", [{"INCLUDE_AZURE_DEVTEST_PRICES": "maybe"}, {"AZURE_PRICES_API_URL": "http://x"}])
def test_settings_rejected(env):
    with pytest.raises(SettingsError):
        _settings(**env)


# --- paged source --------------------------------------------------------------------


def _source(fake):
    return AzurePagedSource(fetch=fake, retry_policy=RetryPolicy(max_retries=3, strategy="fixed", base_delay_seconds=0),
                            sleep=lambda s: None)


def test_follows_next_page_link_without_credentials():
    fake = eastus_routes(FakeHttpSource())
    pages = list(_source(fake).pages(listing_url(_settings(), "eastus")))
    assert [p.number for p in pages] == [1, 2]
    assert all(headers == {} for _, headers in fake.requests)


def test_repeated_next_page_link_fails_instead_of_looping():
    looping = json.dumps({"Items": [], "NextPageLink": EASTUS_P2}).encode()
    fake = eastus_routes(FakeHttpSource()).route(EASTUS_P2, looping)
    src = _source(fake)
    with pytest.raises(SourceError) as err:
        list(src.pages(listing_url(_settings(), "eastus")))
    assert "links back" in str(err.value)
    assert fake.urls()[1:] == [EASTUS_P2] * 4


def test_failed_page_resumes_from_its_own_link():
    fake = eastus_routes(FakeHttpSource()).fail(EASTUS_P2, http_timeout(1))
    list(_source(fake).pages(listing_url(_settings(), "eastus")))
    assert fake.urls()[1:] == [EASTUS_P2, EASTUS_P2]


@pytest.mark.parametrize(
    "headers,expected",
    [({"Retry-After": "12"}, 12.0), ({"x-ms-ratelimit-retailPrices-retry-after": "60"}, 60.0), ({}, None)],
)
def test_retry_after_headers(headers, expected):
    assert AzurePagedSource().retry_after_seconds(headers) == expected


# --- client --------------------------------------------------------------------------


def test_first_url_and_filter():
    url = listing_url(_settings(), "eastus")
    assert url.startswith(API + "?")
    assert parse_qs(urlsplit(url).query)["api-version"] == ["2023-01-01-preview"]
    assert _filter_of(url) == "armRegionName eq 'eastus'"


def test_devtest_exclusion_filter():
    url = listing_url(_settings(INCLUDE_AZURE_DEVTEST_PRICES="false"), "eastus")
    assert _filter_of(url) == "armRegionName eq 'eastus' and type ne 'DevTestConsumption'"


@pytest.mark.parametrize(
    "location,values",
    [("eastus", ["eastus"]), ("north-america", ["North America"]), ("middle-east-and-africa", ["Middle East And Africa"]),
     ("global", ["", "Global"])],
)
def test_filter_values(location, values):
    assert filter_values(location) == values


def test_downloads_every_page_of_a_location(tmp_path):
    fake = eastus_routes(FakeHttpSource())
    out = AzurePricesClient(_settings(), fetch=fake, sleep=lambda s: None).download_location("eastus", str(tmp_path))
    assert out.error is None
    assert [os.path.basename(f) for f in out.files] == ["prices-p0001.json", "prices-p0002.json"]
    assert out.requests_made == 2 and out.throttled_responses == 0
    with open(out.files[0], "rb") as fh, open(_fx("eastus-p0001.json"), "rb") as want:
        assert fh.read() == want.read()  # stored as received


def test_global_makes_two_listings(tmp_path):
    settings = _settings()
    fake = FakeHttpSource()
    fake.route(listing_url(settings, "global", source_value=""), _fx("global-empty-p0001.json"))
    fake.route(listing_url(settings, "global", source_value="Global"), _fx("global-global-p0001.json"))
    out = AzurePricesClient(settings, fetch=fake, sleep=lambda s: None).download_location("global", str(tmp_path))
    assert out.error is None
    assert [os.path.basename(f) for f in out.files] == ["prices-empty-p0001.json", "prices-global-p0001.json"]
    assert [_filter_of(u) for u in fake.urls()] == ["armRegionName eq ''", "armRegionName eq 'Global'"]


def test_geography_uses_the_source_spelling(tmp_path):
    settings = _settings()
    fake = FakeHttpSource().route(listing_url(settings, "north-america"), _fx("north-america-p0001.json"))
    out = AzurePricesClient(settings, fetch=fake, sleep=lambda s: None).download_location("north-america", str(tmp_path))
    assert out.error is None
    assert _filter_of(fake.urls()[0]) == "armRegionName eq 'North America'"


def test_throttling_is_counted(tmp_path):
    settings = _settings()
    fake = eastus_routes(FakeHttpSource(), settings).fail(
        listing_url(settings, "eastus"), http_error(429, headers={"Retry-After": "0"}, times=2)
    )
    out = AzurePricesClient(settings, fetch=fake, sleep=lambda s: None).download_location("eastus", str(tmp_path))
    assert out.error is None
    assert out.throttled_responses == 2
    assert out.requests_made == 4


def test_unknown_location_fails_after_the_first_empty_listing(tmp_path):
    settings = _settings()
    empty = json.dumps({"Items": [], "NextPageLink": None, "Count": 0}).encode()
    fake = FakeHttpSource().route(listing_url(settings, "nowhere1"), empty)
    out = AzurePricesClient(settings, fetch=fake, sleep=lambda s: None).download_location("nowhere1", str(tmp_path))
    assert out.error and "unknown to the source" in out.error
    assert out.requests_made == 1


def test_page_that_keeps_failing_fails_the_location(tmp_path):
    settings = _settings()
    fake = eastus_routes(FakeHttpSource(), settings).fail(EASTUS_P2, http_error(503))
    out = AzurePricesClient(settings, fetch=fake, sleep=lambda s: None).download_location("eastus", str(tmp_path))
    assert out.error and "503" in out.error
