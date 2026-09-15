"""
Tests for the async rewrite of the per-service download core in aws_pricing_api.py
(research.md Decision 5). These pin run_pricing_job()'s external contract: same
PricingJobResult shape and same exceptions as before the async rewrite (FR-011, FR-012;
contracts/internal-interfaces.md).
"""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import NoCredentialsError

from src.aws_pricing_api import (
    CredentialsError,
    PricingJobRequest,
    PricingJobResult,
    run_pricing_job,
)


class _FakeStreamResponse:
    """Stands in for an httpx streaming response."""

    def __init__(self, content: bytes):
        self._content = content

    def raise_for_status(self):
        pass

    async def aiter_bytes(self, chunk_size=8192):
        yield self._content


class _FakeStreamContextManager:
    def __init__(self, content: bytes):
        self._content = content

    async def __aenter__(self):
        return _FakeStreamResponse(self._content)

    async def __aexit__(self, *exc_info):
        return False


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient — every streamed GET succeeds with fixed content."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def stream(self, method, url):
        return _FakeStreamContextManager(b'{"products": {}}')


def _make_pricing_client(service_codes, price_lists_by_service):
    """A MagicMock standing in for the boto3 'pricing' client."""
    client = MagicMock()
    client.describe_services.return_value = {"Services": []}

    paginator = MagicMock()
    paginator.paginate.return_value = [
        {"Services": [{"ServiceCode": code} for code in service_codes]}
    ]
    client.get_paginator.return_value = paginator

    def _list_price_lists(ServiceCode, **kwargs):
        arn = price_lists_by_service.get(ServiceCode)
        if arn:
            return {"PriceLists": [{"PriceListArn": arn}]}
        return {"PriceLists": []}

    client.list_price_lists.side_effect = _list_price_lists
    client.get_price_list_file_url.return_value = {
        "Url": "https://example.invalid/pricing.json"
    }
    return client


def _boto3_client_factory(pricing_client, sts_side_effect=None):
    def _client(service_name, **kwargs):
        if service_name == "sts":
            sts = MagicMock()
            if sts_side_effect:
                sts.get_caller_identity.side_effect = sts_side_effect
            else:
                sts.get_caller_identity.return_value = {"Account": "123456789012"}
            return sts
        if service_name == "pricing":
            return pricing_client
        raise AssertionError(f"Unexpected boto3 client requested: {service_name}")

    return _client


def test_run_pricing_job_returns_unchanged_result_shape(monkeypatch, tmp_path):
    """All services succeed -> PricingJobResult shape/semantics match pre-async-rewrite behavior."""
    pricing_client = _make_pricing_client(
        service_codes=["AmazonEC2", "AmazonS3"],
        price_lists_by_service={
            "AmazonEC2": "arn:aws:pricing:::price-list/AmazonEC2/us-east-1",
            "AmazonS3": "arn:aws:pricing:::price-list/AmazonS3/us-east-1",
        },
    )
    monkeypatch.setattr(
        "src.aws_pricing_api.boto3.client", _boto3_client_factory(pricing_client)
    )
    monkeypatch.setattr("src.aws_pricing_api.httpx.AsyncClient", _FakeAsyncClient)

    request = PricingJobRequest(
        region="us-east-1",
        output_dir=str(tmp_path),
        output_format="json",
        all_services=True,
        max_raw_download_workers=2,
    )

    result = run_pricing_job(request)

    assert isinstance(result, PricingJobResult)
    assert result.success is True
    assert len(result.downloaded_files) == 2
    assert result.failed_services == []
    assert result.errors == []
    for path in result.downloaded_files:
        assert path.endswith("-us-east-1.json")


def test_partial_failure_when_a_service_has_no_price_list(monkeypatch, tmp_path):
    """One service has no price list in-region -> reported in failed_services, run still succeeds."""
    pricing_client = _make_pricing_client(
        service_codes=["AmazonEC2", "AmazonGameLift"],
        price_lists_by_service={
            "AmazonEC2": "arn:aws:pricing:::price-list/AmazonEC2/us-east-1",
            # AmazonGameLift intentionally has no price list in this region
        },
    )
    monkeypatch.setattr(
        "src.aws_pricing_api.boto3.client", _boto3_client_factory(pricing_client)
    )
    monkeypatch.setattr("src.aws_pricing_api.httpx.AsyncClient", _FakeAsyncClient)

    request = PricingJobRequest(
        region="us-east-1",
        output_dir=str(tmp_path),
        output_format="json",
        all_services=True,
        max_raw_download_workers=1,
    )

    result = run_pricing_job(request)

    assert result.success is True
    assert len(result.downloaded_files) == 1
    assert len(result.failed_services) == 1
    assert "AmazonGameLift" in result.failed_services[0]


def test_no_downloads_marks_unsuccessful_without_raising(monkeypatch, tmp_path):
    """Every service fails to produce a price list -> success False + error appended, no exception."""
    pricing_client = _make_pricing_client(
        service_codes=["AmazonGameLift"],
        price_lists_by_service={},
    )
    monkeypatch.setattr(
        "src.aws_pricing_api.boto3.client", _boto3_client_factory(pricing_client)
    )
    monkeypatch.setattr("src.aws_pricing_api.httpx.AsyncClient", _FakeAsyncClient)

    request = PricingJobRequest(
        region="us-east-1",
        output_dir=str(tmp_path),
        output_format="json",
        all_services=True,
    )

    result = run_pricing_job(request)

    assert result.success is False
    assert result.downloaded_files == []
    assert "No pricing data was downloaded." in result.errors


def test_credentials_error_raised_when_sts_fails(monkeypatch, tmp_path):
    """Missing AWS credentials -> CredentialsError, same as before the async rewrite."""
    pricing_client = _make_pricing_client(service_codes=[], price_lists_by_service={})
    monkeypatch.setattr(
        "src.aws_pricing_api.boto3.client",
        _boto3_client_factory(pricing_client, sts_side_effect=NoCredentialsError()),
    )

    request = PricingJobRequest(
        region="us-east-1",
        output_dir=str(tmp_path),
        output_format="json",
        all_services=True,
    )

    with pytest.raises(CredentialsError):
        run_pricing_job(request)


def test_missing_service_codes_and_all_services_raises_value_error(tmp_path):
    """Unchanged validation: neither service_codes nor all_services given -> ValueError."""
    request = PricingJobRequest(region="us-east-1", output_dir=str(tmp_path))

    with pytest.raises(ValueError):
        run_pricing_job(request)
