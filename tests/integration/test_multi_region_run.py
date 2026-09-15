"""
Integration test for per-region failure isolation (FR-005, FR-007).

Simulates a multi-region scheduled tick by calling run_pricing_job() once per
region — exactly what each region's own Dagster run does after the
pricing_weekly_schedule change (research.md Decision 1) — with one region's
downloads mocked to fail throughout. Asserts the other regions' results are
entirely unaffected.
"""

from unittest.mock import MagicMock

import httpx

from src.aws_pricing_api import PricingJobRequest, run_pricing_job

REGIONS_UNDER_TEST = ["us-east-1", "eu-west-2", "ap-northeast-1"]
FAILING_REGION = "eu-west-2"


class _FailingStreamContextManager:
    async def __aenter__(self):
        raise httpx.ConnectError("simulated network failure", request=None)

    async def __aexit__(self, *exc_info):
        return False


class _FailingAsyncClient:
    """Every streamed GET fails — used for the region under induced failure."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def stream(self, method, url):
        return _FailingStreamContextManager()


class _SucceedingStreamResponse:
    def raise_for_status(self):
        pass

    async def aiter_bytes(self, chunk_size=8192):
        yield b'{"products": {}}'


class _SucceedingStreamContextManager:
    async def __aenter__(self):
        return _SucceedingStreamResponse()

    async def __aexit__(self, *exc_info):
        return False


class _SucceedingAsyncClient:
    """Every streamed GET succeeds — used for the unaffected regions."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def stream(self, method, url):
        return _SucceedingStreamContextManager()


def _make_pricing_client():
    client = MagicMock()
    client.describe_services.return_value = {"Services": []}

    paginator = MagicMock()
    paginator.paginate.return_value = [
        {"Services": [{"ServiceCode": "AmazonEC2"}]}
    ]
    client.get_paginator.return_value = paginator

    client.list_price_lists.return_value = {
        "PriceLists": [{"PriceListArn": "arn:aws:pricing:::price-list/AmazonEC2/x"}]
    }
    client.get_price_list_file_url.return_value = {
        "Url": "https://example.invalid/pricing.json"
    }
    return client


def _boto3_client_factory(pricing_client):
    def _client(service_name, **kwargs):
        if service_name == "sts":
            sts = MagicMock()
            sts.get_caller_identity.return_value = {"Account": "123456789012"}
            return sts
        if service_name == "pricing":
            return pricing_client
        raise AssertionError(f"Unexpected boto3 client requested: {service_name}")

    return _client


def test_one_region_failing_does_not_affect_the_others(monkeypatch, tmp_path):
    results = {}

    for region in REGIONS_UNDER_TEST:
        pricing_client = _make_pricing_client()
        monkeypatch.setattr(
            "src.aws_pricing_api.boto3.client", _boto3_client_factory(pricing_client)
        )
        fake_client = (
            _FailingAsyncClient if region == FAILING_REGION else _SucceedingAsyncClient
        )
        monkeypatch.setattr("src.aws_pricing_api.httpx.AsyncClient", fake_client)

        request = PricingJobRequest(
            region=region,
            output_dir=str(tmp_path / region),
            output_format="json",
            all_services=True,
            max_raw_download_workers=2,
        )
        results[region] = run_pricing_job(request)

    for region in REGIONS_UNDER_TEST:
        if region == FAILING_REGION:
            continue
        result = results[region]
        assert result.success is True, f"{region} should have succeeded"
        assert len(result.downloaded_files) == 1
        assert result.errors == []

    failing_result = results[FAILING_REGION]
    assert failing_result.success is False
    assert failing_result.downloaded_files == []
    assert "No pricing data was downloaded." in failing_result.errors
