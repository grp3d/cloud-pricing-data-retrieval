"""
AWS pricing data collection logic, decoupled from CLI and Dagster orchestration.
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional

import boto3
import requests
from botocore.exceptions import ClientError, CredentialRetrievalError, NoCredentialsError

logger = logging.getLogger(__name__)


def resolve_output_dir(output_dir: str) -> str:
    env_root = os.getenv("DATA_DIRECTORY_ROOT")
    base_root = env_root if env_root else "."
    if output_dir:
        return output_dir
    return base_root


class CredentialsError(Exception):
    """Raised when AWS credentials are missing or insufficient."""


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


@dataclass
class PricingJobResult:
    """Result returned by run_pricing_job()."""

    downloaded_files: List[str] = field(default_factory=list)
    failed_services: List[str] = field(default_factory=list)
    consolidated_file: Optional[str] = None
    truncated_file: Optional[str] = None
    success: bool = False
    errors: List[str] = field(default_factory=list)


class PricingDataManager:
    """Handles AWS Pricing API interactions."""

    def __init__(
        self,
        region: str,
        output_dir: str = ".",
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        self.region = region
        self.output_dir = output_dir
        self._log = log_callback or (lambda msg: logger.info(msg))

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
        sts = boto3.client("sts")
        sts.get_caller_identity()
        return boto3.client("pricing", region_name="us-east-1")

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
            ec2 = boto3.client("ec2", region_name="us-east-1")
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
            error_msg = e.response.get("Error", {}).get("Message", "")
            if "region" in error_msg.lower():
                raise ValueError(f"Invalid region '{self.region}'.")
            if "service" in error_msg.lower():
                raise ValueError(f"Invalid service code '{service_code}'.")
            raise ValueError(f"Error fetching price list for {service_code}: {e}")

    def download_pricing_data(
        self, price_list_arn: str, service_code: str, file_format: str
    ) -> Optional[str]:
        try:
            url_response = self.pricing_client.get_price_list_file_url(
                PriceListArn=price_list_arn, FileFormat=file_format
            )
            url = url_response["Url"]
            filename = os.path.join(
                self.output_dir, f"pricing-{service_code}-{self.region}.{file_format}"
            )

            for attempt in range(3):
                try:
                    r = requests.get(url, stream=True, timeout=(5, 30))
                    r.raise_for_status()
                    with open(filename, "wb") as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                    self._log(f"Downloaded pricing for {service_code} → {filename}")
                    return filename
                except requests.Timeout:
                    if attempt == 2:
                        self._error(
                            f"Timeout downloading {service_code} after 3 attempts"
                        )
                        return None
                except requests.RequestException as e:
                    self._error(f"Request error downloading {service_code}: {e}")
                    return None
        except Exception as e:
            self._error(f"Unexpected error processing {service_code}: {e}")
            return None

    def _process_single_service(
        self, service_code: str, output_format: str
    ) -> tuple[str, Optional[str], Optional[str]]:
        try:
            arn = self.get_price_list_arn(service_code)
            if not arn:
                return (
                    service_code,
                    None,
                    f"{service_code} (no pricing data available in {self.region})",
                )

            path = self.download_pricing_data(arn, service_code, output_format)
            if path:
                return service_code, path, None
            return service_code, None, f"{service_code} (download failed)"
        except ValueError as e:
            return service_code, None, str(e)

    def process_service_codes(
        self,
        service_codes: List[str],
        output_format: str,
        max_raw_download_workers: int = 2,
    ) -> tuple[List[str], List[str]]:
        downloaded_files: List[str] = []
        failed_services: List[str] = []

        worker_count = max(1, max_raw_download_workers)
        total = len(service_codes)
        self._log(
            f"Starting raw downloads for {total} service(s) with {worker_count} worker(s)"
        )

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(
                    self._process_single_service, code, output_format
                ): code
                for code in service_codes
            }

            for i, future in enumerate(as_completed(future_map), 1):
                code = future_map[future]
                self._log(f"[{i}/{total}] Completed {code}")
                _, path, error = future.result()
                if path:
                    downloaded_files.append(path)
                elif error:
                    failed_services.append(error)

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

    manager = PricingDataManager(
        region=request.region,
        output_dir=request.output_dir,
        log_callback=request.log_callback,
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
        downloaded, failed = manager.process_service_codes(
            service_codes,
            request.output_format,
            request.max_raw_download_workers,
        )
    except ValueError as e:
        result.errors.append(str(e))
        return result

    result.downloaded_files = downloaded
    result.failed_services = failed

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
