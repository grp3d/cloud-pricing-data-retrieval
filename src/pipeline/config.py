"""
Pipeline settings, read from environment variables (contracts/configuration.md, FR-035).

Every value has a safe default. Settings that serve one purpose are named for it —
the download retry settings are `pricing_download_retry_*` so no other process
shares them by accident (FR-047, constitution Principle III).
"""

import os
import re
from dataclasses import asdict, dataclass, field
from typing import List, Mapping, Optional

from src.aws_regions import resolve_regions

RETRY_STRATEGIES = ("fixed", "linear_backoff", "exponential_backoff")

_PROVIDER_RE = re.compile(r"[a-z0-9]+")
_HOST_LABEL_RE = re.compile(r"[a-z0-9-]{1,63}")
_ENVIRONMENT_RE = re.compile(r"[a-z0-9-]{1,32}")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class SettingsError(ValueError):
    """Invalid pipeline configuration. The CLI maps this to exit code 2."""


@dataclass(frozen=True)
class PipelineSettings:
    storage_uri: str
    provider: str = "aws"
    environment: str = "local"
    regions: List[str] = field(default_factory=list)
    max_raw_download_workers: int = 2
    transform_concurrency: int = 1
    pricing_download_retry_max_retries: int = 3
    pricing_download_retry_strategy: str = "exponential_backoff"
    pricing_download_retry_base_delay_seconds: float = 30
    run_timeout_minutes: int = 120
    run_claim_ttl_minutes: int = 180
    raw_retention_days: int = 30
    parquet_weekly_retention_months: int = 12
    superseded_file_grace_minutes: float = 5
    superseded_file_inline_wait_max_minutes: float = 5
    alert_topic_arn: Optional[str] = None
    success_summary_enabled: bool = False
    host_label: str = "local"
    git_sha: str = "local"
    image_tag: str = "local"
    max_clock_skew_seconds: int = 300

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = None) -> "PipelineSettings":
        env = os.environ if environ is None else environ

        def get(name: str, default: str = "") -> str:
            return str(env.get(name, default)).strip()

        def as_int(name: str, default: int, minimum: int) -> int:
            raw = get(name, str(default))
            try:
                value = int(raw)
            except ValueError:
                raise SettingsError(f"{name} must be an integer, got {raw!r}")
            if value < minimum:
                raise SettingsError(f"{name} must be >= {minimum}, got {value}")
            return value

        def as_float(name: str, default: float, minimum: float) -> float:
            raw = get(name, str(default))
            try:
                value = float(raw)
            except ValueError:
                raise SettingsError(f"{name} must be a number, got {raw!r}")
            if value < minimum:
                raise SettingsError(f"{name} must be >= {minimum}, got {value}")
            return value

        def as_bool(name: str, default: bool) -> bool:
            raw = get(name, "true" if default else "false").lower()
            if raw in _TRUE:
                return True
            if raw in _FALSE:
                return False
            raise SettingsError(f"{name} must be true or false, got {raw!r}")

        data_root = get("DATA_DIRECTORY_ROOT")
        default_uri = f"file://{data_root.rstrip('/')}/pipeline" if data_root else "file://./pipeline"
        storage_uri = get("PIPELINE_STORAGE_URI") or default_uri
        if not storage_uri.startswith(("file://", "s3://")):
            raise SettingsError(
                f"PIPELINE_STORAGE_URI must start with file:// or s3://, got {storage_uri!r}"
            )

        provider = get("PIPELINE_PROVIDER", "aws")
        if not _PROVIDER_RE.fullmatch(provider):
            raise SettingsError(f"PIPELINE_PROVIDER must match [a-z0-9]+, got {provider!r}")

        environment = get("PIPELINE_ENVIRONMENT", "local")
        if not _ENVIRONMENT_RE.fullmatch(environment):
            raise SettingsError(f"PIPELINE_ENVIRONMENT must match [a-z0-9-]+, got {environment!r}")

        host_label = get("PIPELINE_HOST_LABEL", "local")
        if not _HOST_LABEL_RE.fullmatch(host_label):
            raise SettingsError(
                f"PIPELINE_HOST_LABEL must match [a-z0-9-]{{1,63}}, got {host_label!r}"
            )

        strategy = get("PRICING_DOWNLOAD_RETRY_STRATEGY", "exponential_backoff")
        if strategy not in RETRY_STRATEGIES:
            raise SettingsError(
                f"PRICING_DOWNLOAD_RETRY_STRATEGY must be one of {', '.join(RETRY_STRATEGIES)}, "
                f"got {strategy!r}"
            )

        regions_raw = get("PRICING_REGIONS")
        try:
            regions = (
                [r.strip() for r in regions_raw.split(",") if r.strip()]
                if regions_raw
                else resolve_regions()
            )
        except ValueError as e:
            raise SettingsError(str(e))

        run_timeout = as_int("RUN_TIMEOUT_MINUTES", 120, 1)
        claim_ttl = as_int("RUN_CLAIM_TTL_MINUTES", 180, 1)
        if claim_ttl <= run_timeout:
            raise SettingsError(
                f"RUN_CLAIM_TTL_MINUTES ({claim_ttl}) must be greater than "
                f"RUN_TIMEOUT_MINUTES ({run_timeout})"
            )

        return cls(
            storage_uri=storage_uri,
            provider=provider,
            environment=environment,
            regions=regions,
            max_raw_download_workers=as_int("MAX_RAW_DOWNLOAD_WORKERS", 2, 1),
            transform_concurrency=as_int("TRANSFORM_CONCURRENCY", 1, 1),
            pricing_download_retry_max_retries=as_int("PRICING_DOWNLOAD_RETRY_MAX_RETRIES", 3, 0),
            pricing_download_retry_strategy=strategy,
            pricing_download_retry_base_delay_seconds=as_float(
                "PRICING_DOWNLOAD_RETRY_BASE_DELAY_SECONDS", 30, 0
            ),
            run_timeout_minutes=run_timeout,
            run_claim_ttl_minutes=claim_ttl,
            raw_retention_days=as_int("RAW_RETENTION_DAYS", 30, 1),
            parquet_weekly_retention_months=as_int("PARQUET_WEEKLY_RETENTION_MONTHS", 12, 1),
            superseded_file_grace_minutes=as_float("SUPERSEDED_FILE_GRACE_MINUTES", 5, 0),
            superseded_file_inline_wait_max_minutes=as_float(
                "SUPERSEDED_FILE_INLINE_WAIT_MAX_MINUTES", 5, 0
            ),
            alert_topic_arn=get("ALERT_TOPIC_ARN") or None,
            success_summary_enabled=as_bool("SUCCESS_SUMMARY_ENABLED", False),
            host_label=host_label,
            git_sha=get("GIT_SHA", "local") or "local",
            image_tag=get("IMAGE_TAG", "local") or "local",
            max_clock_skew_seconds=as_int("MAX_CLOCK_SKEW_SECONDS", 300, 0),
        )

    def redacted(self) -> dict:
        """Effective settings for the `settings` command, with secrets masked."""
        data = asdict(self)
        if data.get("alert_topic_arn"):
            data["alert_topic_arn"] = "***"
        return data
