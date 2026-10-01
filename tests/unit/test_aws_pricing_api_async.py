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


# ---------------------------------------------------------------------------
# Feature 003 (FR-047, research R9): configurable per-file retries, retryable vs
# permanent error classification, integrity check, and new result fields.
# ---------------------------------------------------------------------------

import httpx

from src.pipeline.retry import RetryPolicy

GOOD_BODY = b'{"products": {}}\n'


class _ScriptedStreamResponse:
    def __init__(self, body: bytes, status: int = 200, headers=None):
        self._body = body
        self.status_code = status
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://example.invalid/pricing.json")
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=request,
                response=httpx.Response(self.status_code, request=request),
            )

    async def aiter_bytes(self, chunk_size=8192):
        yield self._body


class _ScriptedStreamCM:
    def __init__(self, step):
        self._step = step

    async def __aenter__(self):
        if isinstance(self._step, Exception):
            raise self._step
        return self._step

    async def __aexit__(self, *exc_info):
        return False


def _scripted_client(steps):
    """An httpx.AsyncClient replacement that plays `steps` in order, one per GET."""
    state = {"calls": 0}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        def stream(self, method, url):
            step = steps[min(state["calls"], len(steps) - 1)]
            state["calls"] += 1
            return _ScriptedStreamCM(step)

    return _Client, state


def _one_service_setup(monkeypatch, steps, service="AmazonEC2"):
    pricing_client = _make_pricing_client(
        service_codes=[service],
        price_lists_by_service={service: f"arn:aws:pricing:::price-list/{service}/us-east-1"},
    )
    monkeypatch.setattr("src.aws_pricing_api.boto3.client", _boto3_client_factory(pricing_client))
    client_cls, state = _scripted_client(steps)
    monkeypatch.setattr("src.aws_pricing_api.httpx.AsyncClient", client_cls)
    return state


def _request(tmp_path, sleeps, max_retries=3, strategy="exponential_backoff", base=10):
    async def fake_sleep(seconds):
        sleeps.append(seconds)

    return PricingJobRequest(
        region="us-east-1",
        output_dir=str(tmp_path),
        output_format="json",
        all_services=True,
        retry_policy=RetryPolicy(max_retries, strategy, base),
        retry_sleep=fake_sleep,
    )


def test_timeout_is_retried_with_policy_delays(monkeypatch, tmp_path):
    sleeps = []
    state = _one_service_setup(
        monkeypatch,
        [httpx.ReadTimeout("slow"), httpx.ConnectError("reset"), _ScriptedStreamResponse(GOOD_BODY)],
    )
    result = run_pricing_job(_request(tmp_path, sleeps))
    assert result.success is True
    assert result.failed_downloads == []
    assert result.max_attempts == 3
    assert state["calls"] == 3
    assert sleeps == [10, 20]


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retryable_http_statuses(monkeypatch, tmp_path, status):
    sleeps = []
    state = _one_service_setup(
        monkeypatch, [_ScriptedStreamResponse(b"", status=status), _ScriptedStreamResponse(GOOD_BODY)]
    )
    result = run_pricing_job(_request(tmp_path, sleeps, strategy="fixed", base=1))
    assert result.failed_downloads == []
    assert state["calls"] == 2
    assert sleeps == [1]


def test_permanent_http_error_is_not_retried(monkeypatch, tmp_path):
    sleeps = []
    state = _one_service_setup(monkeypatch, [_ScriptedStreamResponse(b"", status=403)])
    result = run_pricing_job(_request(tmp_path, sleeps))
    assert state["calls"] == 1
    assert sleeps == []
    assert len(result.failed_downloads) == 1
    failure = result.failed_downloads[0]
    assert failure["service"] == "AmazonEC2"
    assert failure["attempts"] == 1
    assert "403" in failure["reason"]


@pytest.mark.parametrize(
    "bad",
    [
        _ScriptedStreamResponse(b""),  # empty body
        _ScriptedStreamResponse(b'{"products": {'),  # truncated JSON (no trailing brace)
        _ScriptedStreamResponse(GOOD_BODY, headers={"content-length": "9999"}),  # length mismatch
    ],
)
def test_truncated_or_empty_download_is_retried_until_exhausted(monkeypatch, tmp_path, bad):
    sleeps = []
    state = _one_service_setup(monkeypatch, [bad])
    result = run_pricing_job(_request(tmp_path, sleeps, max_retries=2, strategy="linear_backoff", base=1))
    assert state["calls"] == 3
    assert sleeps == [1, 2]
    assert result.failed_downloads[0]["attempts"] == 3
    assert result.max_attempts == 3
    assert result.downloaded_files == []


def test_no_price_list_is_counted_not_failed(monkeypatch, tmp_path):
    pricing_client = _make_pricing_client(
        service_codes=["AmazonEC2", "AmazonGameLift"],
        price_lists_by_service={"AmazonEC2": "arn:aws:pricing:::price-list/AmazonEC2/us-east-1"},
    )
    monkeypatch.setattr("src.aws_pricing_api.boto3.client", _boto3_client_factory(pricing_client))
    client_cls, _ = _scripted_client([_ScriptedStreamResponse(GOOD_BODY)])
    monkeypatch.setattr("src.aws_pricing_api.httpx.AsyncClient", client_cls)
    result = run_pricing_job(_request(tmp_path, []))
    assert result.services_without_price_list == 1
    assert result.failed_downloads == []
    # Backwards compatible for the ad hoc CLI (FR-037): still listed in failed_services.
    assert any("AmazonGameLift" in s for s in result.failed_services)


def test_default_retry_policy_when_none_given(monkeypatch, tmp_path):
    """Without an explicit policy the job still retries (defaults from PipelineSettings)."""
    for name in ("MAX_RETRIES", "STRATEGY", "BASE_DELAY_SECONDS"):
        monkeypatch.delenv(f"PRICING_DOWNLOAD_RETRY_{name}", raising=False)
    _one_service_setup(monkeypatch, [httpx.ReadTimeout("slow"), _ScriptedStreamResponse(GOOD_BODY)])
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    request = PricingJobRequest(
        region="us-east-1", output_dir=str(tmp_path), all_services=True, retry_sleep=fake_sleep
    )
    result = run_pricing_job(request)
    assert result.failed_downloads == []
    assert sleeps == [30]


def test_price_list_lookup_throttling_is_retried_with_policy(monkeypatch, tmp_path):
    """ListPriceLists throttling (seen for real with 7 regions in parallel) must be retried
    per the FR-047 policy, not turned into a permanent per-service failure."""
    from botocore.exceptions import ClientError

    pricing_client = _make_pricing_client(
        service_codes=["AmazonEC2"],
        price_lists_by_service={"AmazonEC2": "arn:aws:pricing:::price-list/AmazonEC2/us-east-1"},
    )
    real = pricing_client.list_price_lists.side_effect
    calls = {"n": 0}

    def throttled(**kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}, "ListPriceLists"
            )
        return real(**kwargs)

    pricing_client.list_price_lists.side_effect = throttled
    monkeypatch.setattr("src.aws_pricing_api.boto3.client", _boto3_client_factory(pricing_client))
    client_cls, _ = _scripted_client([_ScriptedStreamResponse(GOOD_BODY)])
    monkeypatch.setattr("src.aws_pricing_api.httpx.AsyncClient", client_cls)
    sleeps = []
    result = run_pricing_job(_request(tmp_path, sleeps, strategy="fixed", base=2))
    assert result.failed_downloads == []
    assert len(result.downloaded_files) == 1
    assert sleeps == [2, 2]
    assert result.max_attempts == 3


def test_price_list_lookup_throttling_exhausted_reports_attempts(monkeypatch, tmp_path):
    from botocore.exceptions import ClientError

    pricing_client = _make_pricing_client(
        service_codes=["AmazonEC2"],
        price_lists_by_service={"AmazonEC2": "arn:aws:pricing:::price-list/AmazonEC2/us-east-1"},
    )
    pricing_client.list_price_lists.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}, "ListPriceLists"
    )
    monkeypatch.setattr("src.aws_pricing_api.boto3.client", _boto3_client_factory(pricing_client))
    result = run_pricing_job(_request(tmp_path, [], max_retries=2, strategy="fixed", base=0))
    (failure,) = result.failed_downloads
    assert failure["attempts"] == 3
    assert "Throttling" in failure["reason"]
