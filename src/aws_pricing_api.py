"""
AWS pricing data collection logic, decoupled from CLI and Dagster orchestration.
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, List, Optional

import boto3
import httpx
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, CredentialRetrievalError, NoCredentialsError

from src.pipeline.retry import RetriesExhausted, RetryPolicy, run_with_retry_async

logger = logging.getLogger(__name__)


def resolve_output_dir(output_dir: str) -> str:
    env_root = os.getenv("DATA_DIRECTORY_ROOT")
    base_root = env_root if env_root else "."
    if output_dir:
        return output_dir
    return base_root


class CredentialsError(Exception):
    """Raised when AWS credentials are missing or insufficient."""


class IncompleteDownload(Exception):
    """A price-list file arrived truncated or empty (retryable, FR-047)."""


# SDK-level retries: "adaptive" mode adds client-side rate limiting, which matters because all
# regions call the Price List API at once. Throttling that outlasts the SDK's own retries is
# then retried per the pricing_download_retry_* policy (FR-047, research R9).
_BOTO_CONFIG = BotoConfig(retries={"mode": "adaptive", "max_attempts": 5})

_RETRYABLE_API_ERRORS = {"ThrottlingException", "Throttling", "RequestLimitExceeded",
                         "TooManyRequestsException", "ServiceUnavailable", "InternalFailure"}


def is_retryable_download_error(error: BaseException) -> bool:
    """Retryable: timeouts, connection errors, HTTP 429/5xx, truncated/empty files, throttling."""
    if isinstance(error, (httpx.TimeoutException, httpx.TransportError, IncompleteDownload)):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        return status == 429 or status >= 500
    if isinstance(error, ClientError):
        return error.response.get("Error", {}).get("Code") in _RETRYABLE_API_ERRORS
    return False


def _describe_error(error: BaseException) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    return f"{type(error).__name__}: {error}"


@dataclass
class PricingJobRequest:
    """Parameters for a pricing collection run."""

    region: str
    output_dir: str = ""
    output_format: str = "json"
    service_codes: List[str] = field(default_factory=list)
    all_services: bool = False
    max_raw_download_workers: int = 2
    consolidate: bool = False
    truncate_columns: List[str] = field(default_factory=list)
    log_callback: Optional[Callable[[str], None]] = None
    # Per-file download retries (FR-047). None → defaults from PipelineSettings.
    retry_policy: Optional[RetryPolicy] = None
    retry_sleep: Optional[Callable[[float], Awaitable[None]]] = None


@dataclass
class PricingJobResult:
    """Result returned by run_pricing_job()."""

    downloaded_files: List[str] = field(default_factory=list)
    failed_services: List[str] = field(default_factory=list)
    consolidated_file: Optional[str] = None
    truncated_file: Optional[str] = None
    success: bool = False
    errors: List[str] = field(default_factory=list)
    # Feature 003: files that failed after retries ({service, reason, attempts}); services
    # with no price list in the region are counted separately and are not failures.
    failed_downloads: List[dict] = field(default_factory=list)
    services_without_price_list: int = 0
    max_attempts: int = 0


class PricingDataManager:
    """Handles AWS Pricing API interactions."""

    def __init__(
        self,
        region: str,
        output_dir: str = ".",
        log_callback: Optional[Callable[[str], None]] = None,
        retry_policy: Optional[RetryPolicy] = None,
        retry_sleep: Optional[Callable[[float], Awaitable[None]]] = None,
    ):
        self.region = region
        self.output_dir = output_dir
        self._log = log_callback or (lambda msg: logger.info(msg))
        self.retry_policy = retry_policy or RetryPolicy()
        self._retry_sleep = retry_sleep or asyncio.sleep

        try:
            self.pricing_client = self._initialize_client()
        except (NoCredentialsError, CredentialRetrievalError) as e:
            raise CredentialsError(
                "AWS credentials not found. Please configure:\n"
                "1. Environment variables (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY)\n"
                "2. AWS credentials file (~/.aws/credentials)\n"
                "3. IAM role attached to the running instance/container\n"
                f"\nError details: {e}"
            )

    def _initialize_client(self):
        sts = boto3.client("sts", config=_BOTO_CONFIG)
        sts.get_caller_identity()
        return boto3.client("pricing", region_name="us-east-1", config=_BOTO_CONFIG)

    def _warn(self, msg: str):
        self._log(f"WARNING: {msg}")
        logger.warning(msg)

    def _error(self, msg: str):
        self._log(f"ERROR: {msg}")
        logger.error(msg)

    def check_permissions(self):
        try:
            self.pricing_client.describe_services(MaxResults=1)
        except ClientError as e:
            if e.response["Error"]["Code"] == "AccessDeniedException":
                raise CredentialsError(
                    "Credentials lack 'pricing:DescribeServices' permission."
                )
            raise

    def get_all_service_codes(self) -> List[str]:
        service_codes = []
        paginator = self.pricing_client.get_paginator("describe_services")
        for page in paginator.paginate():
            service_codes.extend([s["ServiceCode"] for s in page["Services"]])
        return service_codes

    def validate_service_codes(self, service_codes: List[str]) -> None:
        valid_services = set(self.get_all_service_codes())
        invalid = [c for c in service_codes if c not in valid_services]
        if invalid:
            raise ValueError(
                f"Invalid service code(s): {', '.join(invalid)}. "
                "Use 'aws pricing describe-services' to list valid codes."
            )

    def validate_region(self) -> None:
        try:
            ec2 = boto3.client("ec2", region_name="us-east-1", config=_BOTO_CONFIG)
            valid_regions = [r["RegionName"] for r in ec2.describe_regions()["Regions"]]
            if self.region not in valid_regions:
                raise ValueError(
                    f"Invalid region '{self.region}'. "
                    f"Valid regions: {', '.join(sorted(valid_regions))}"
                )
        except ClientError:
            try:
                self.pricing_client.list_price_lists(
                    ServiceCode="AmazonEC2",
                    CurrencyCode="USD",
                    EffectiveDate=datetime.now().strftime("%Y-%m-%d %H:%M"),
                    RegionCode=self.region,
                    MaxResults=1,
                )
            except ClientError as e:
                if "region" in str(e).lower():
                    raise ValueError(f"Invalid region '{self.region}'.")

    def get_price_list_arn(self, service_code: str) -> Optional[str]:
        try:
            response = self.pricing_client.list_price_lists(
                ServiceCode=service_code,
                CurrencyCode="USD",
                EffectiveDate=datetime.now().strftime("%Y-%m-%d %H:%M"),
                RegionCode=self.region,
            )
            price_lists = response.get("PriceLists", [])
            return price_lists[0]["PriceListArn"] if price_lists else None
        except ClientError as e:
            if is_retryable_download_error(e):
                raise  # throttling: retried by the caller per the retry policy
            error_msg = e.response.get("Error", {}).get("Message", "")
            if "region" in error_msg.lower():
                raise ValueError(f"Invalid region '{self.region}'.")
            if "service" in error_msg.lower():
                raise ValueError(f"Invalid service code '{service_code}'.")
            raise ValueError(f"Error fetching price list for {service_code}: {e}")

    async def _download_once(self, price_list_arn: str, filename: str, file_format: str) -> None:
        """One download attempt; raises on any error, including a truncated file."""
        url_response = await asyncio.to_thread(
            self.pricing_client.get_price_list_file_url,
            PriceListArn=price_list_arn,
            FileFormat=file_format,
        )
        url = url_response["Url"]
        timeout = httpx.Timeout(30.0, connect=5.0)
        written = 0
        last_chunk = b""
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("GET", url) as r:
                r.raise_for_status()
                expected = getattr(r, "headers", {}).get("content-length")
                with open(filename, "wb") as f:
                    async for chunk in r.aiter_bytes(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            written += len(chunk)
                            last_chunk = chunk
        # Cheap integrity check (research R9): parsing ~480 MB twice to validate is too costly.
        if written == 0:
            raise IncompleteDownload("empty download")
        if expected is not None and int(expected) != written:
            raise IncompleteDownload(f"expected {expected} bytes, got {written}")
        if file_format == "json" and not last_chunk.rstrip().endswith(b"}"):
            raise IncompleteDownload("truncated JSON (no closing brace)")

    async def _download_pricing_data_async(
        self, price_list_arn: str, service_code: str, file_format: str
    ) -> tuple[Optional[str], int, Optional[str]]:
        """Download one price-list file with retries. Returns (path, attempts, error)."""
        filename = os.path.join(
            self.output_dir, f"pricing-{service_code}-{self.region}.{file_format}"
        )
        try:
            _, attempts = await run_with_retry_async(
                lambda: self._download_once(price_list_arn, filename, file_format),
                is_retryable_download_error,
                self.retry_policy,
                sleep=self._retry_sleep,
            )
        except RetriesExhausted as e:
            reason = _describe_error(e.last_error)
            self._error(f"Failed downloading {service_code} after {e.attempts} attempt(s): {reason}")
            if os.path.exists(filename):
                os.remove(filename)
            return None, e.attempts, reason
        self._log(f"Downloaded pricing for {service_code} → {filename}")
        return filename, attempts, None

    async def _process_single_service_async(
        self, service_code: str, output_format: str
    ) -> tuple[str, Optional[str], Optional[str], int, bool]:
        """Returns (service, path, error, attempts, no_price_list)."""
        try:
            arn, lookup_attempts = await run_with_retry_async(
                lambda: asyncio.to_thread(self.get_price_list_arn, service_code),
                is_retryable_download_error,
                self.retry_policy,
                sleep=self._retry_sleep,
            )
        except RetriesExhausted as e:
            reason = str(e.last_error) if isinstance(e.last_error, ValueError) else _describe_error(e.last_error)
            return service_code, None, f"{service_code}: {reason}", e.attempts, False
        if not arn:
            return (
                service_code,
                None,
                f"{service_code} (no pricing data available in {self.region})",
                0,
                True,
            )
        path, attempts, error = await self._download_pricing_data_async(
            arn, service_code, output_format
        )
        attempts = max(attempts, lookup_attempts)
        if path:
            return service_code, path, None, attempts, False
        return service_code, None, f"{service_code} (download failed: {error})", attempts, False

    async def _process_service_codes_async(
        self,
        service_codes: List[str],
        output_format: str,
        max_raw_download_workers: int = 2,
    ) -> tuple[List[str], List[str]]:
        downloaded_files: List[str] = []
        failed_services: List[str] = []
        self.failed_downloads: List[dict] = []
        self.services_without_price_list = 0
        self.max_attempts = 0

        worker_count = max(1, max_raw_download_workers)
        total = len(service_codes)
        self._log(
            f"Starting raw downloads for {total} service(s) with {worker_count} concurrent worker(s)"
        )

        semaphore = asyncio.Semaphore(worker_count)
        progress_lock = asyncio.Lock()
        completed = 0

        async def _bounded(code: str) -> tuple[str, Optional[str], Optional[str], int, bool]:
            nonlocal completed
            async with semaphore:
                result = await self._process_single_service_async(code, output_format)
            async with progress_lock:
                completed += 1
                self._log(f"[{completed}/{total}] Completed {code}")
            return result

        results = await asyncio.gather(*(_bounded(code) for code in service_codes))

        for code, path, error, attempts, no_price_list in results:
            self.max_attempts = max(self.max_attempts, attempts)
            if path:
                downloaded_files.append(path)
                continue
            if error:
                failed_services.append(error)
            if no_price_list:
                self.services_without_price_list += 1
            else:
                self.failed_downloads.append(
                    {"service": code, "reason": (error or "")[:500], "attempts": attempts}
                )

        if failed_services:
            self._warn(f"No pricing data found for: {', '.join(failed_services)}")

        return downloaded_files, failed_services

    def consolidate_files(
        self, files: List[str], output_format: str
    ) -> Optional[str]:
        import pandas as pd

        consolidated_data = []
        for file in files:
            try:
                df = pd.read_csv(file, skiprows=5, low_memory=False, on_bad_lines="skip")
                service_name = os.path.basename(file).split("-")[1]
                df["ServiceCode"] = service_name
                consolidated_data.append(df)
                self._log(f"Read pricing data from {service_name}")
            except Exception as e:
                self._warn(f"Error reading {file}: {e}")

        if not consolidated_data:
            return None

        try:
            df = pd.concat(consolidated_data, ignore_index=True)
            df = df.dropna(axis=1, how="all")
            output_file = os.path.join(
                self.output_dir, f"consolidated_pricing_{self.region}.{output_format}"
            )
            if output_format == "csv":
                df.to_csv(output_file, index=False)
            elif output_format == "json":
                df.to_json(output_file, orient="records")
            elif output_format == "excel":
                df.to_excel(output_file, index=False)
            self._log(f"Consolidated {len(consolidated_data)} files → {output_file}")
            return output_file
        except Exception as e:
            self._error(f"Consolidation error: {e}")
            return None

    def truncate_data(self, file_path: str, columns: List[str]) -> Optional[str]:
        import pandas as pd

        try:
            is_consolidated = "consolidated_pricing" in os.path.basename(file_path)
            df = (
                pd.read_csv(file_path, low_memory=False)
                if is_consolidated
                else pd.read_csv(
                    file_path, skiprows=5, low_memory=False, on_bad_lines="skip"
                )
            )
            available = [c for c in columns if c in df.columns]
            if not available:
                self._error(
                    f"None of the requested columns found. "
                    f"Available: {', '.join(df.columns[:10])}"
                )
                return None
            base, ext = os.path.splitext(file_path)
            output_file = f"{base}_truncated{ext}"
            df[available].to_csv(output_file, index=False)
            return output_file
        except Exception as e:
            self._error(f"Truncation error: {e}")
            return None


def run_pricing_job(request: PricingJobRequest) -> PricingJobResult:
    """Execute a pricing collection run."""
    if not (request.service_codes or request.all_services):
        raise ValueError("Must specify either service_codes or all_services=True.")
    if request.service_codes and request.all_services:
        raise ValueError("Cannot use both service_codes and all_services=True.")

    result = PricingJobResult()
    request.output_dir = resolve_output_dir(request.output_dir)
    os.makedirs(request.output_dir, exist_ok=True)

    retry_policy = request.retry_policy
    if retry_policy is None:
        from src.pipeline.config import PipelineSettings

        retry_policy = RetryPolicy.from_settings(PipelineSettings.from_env())

    manager = PricingDataManager(
        region=request.region,
        output_dir=request.output_dir,
        log_callback=request.log_callback,
        retry_policy=retry_policy,
        retry_sleep=request.retry_sleep,
    )
    manager.check_permissions()

    if request.all_services:
        service_codes = manager.get_all_service_codes()
        if not service_codes:
            result.errors.append("No services found via describe_services.")
            return result
    else:
        service_codes = request.service_codes
        manager.validate_region()
        manager.validate_service_codes(service_codes)

    try:
        downloaded, failed = asyncio.run(
            manager._process_service_codes_async(
                service_codes,
                request.output_format,
                request.max_raw_download_workers,
            )
        )
    except ValueError as e:
        result.errors.append(str(e))
        return result

    result.downloaded_files = downloaded
    result.failed_services = failed
    result.failed_downloads = manager.failed_downloads
    result.services_without_price_list = manager.services_without_price_list
    result.max_attempts = manager.max_attempts

    if not downloaded:
        result.errors.append("No pricing data was downloaded.")
        return result

    result.success = True

    if request.consolidate:
        consolidated = manager.consolidate_files(downloaded, request.output_format)
        result.consolidated_file = consolidated
        if consolidated and request.truncate_columns:
            result.truncated_file = manager.truncate_data(
                consolidated, request.truncate_columns
            )
    elif request.truncate_columns:
        for file_path in downloaded:
            truncated = manager.truncate_data(file_path, request.truncate_columns)
            if truncated:
                result.truncated_file = truncated

    return result


CREDENTIAL_HELP = """
AWS Credential Configuration Guide:

1. Environment Variables:
   export AWS_ACCESS_KEY_ID='your_access_key'
   export AWS_SECRET_ACCESS_KEY='your_secret_key'
   export AWS_DEFAULT_REGION='your_region'

2. AWS Credentials File (~/.aws/credentials):
   [default]
   aws_access_key_id = your_access_key
   aws_secret_access_key = your_secret_key

3. AWS CLI:
   Run: aws configure

4. IAM Roles (EC2 / ECS / Lambda):
   Attach a role with the following permissions:
   - pricing:DescribeServices
   - pricing:GetProducts
   - pricing:ListPriceLists
   - pricing:GetPriceListFileUrl
""".strip()
