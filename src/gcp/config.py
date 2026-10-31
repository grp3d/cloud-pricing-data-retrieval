"""
GCP settings (contracts/configuration.md; FR-004, FR-006, FR-008). The API key is a
secret: required for GCP runs, kept out of `repr`, shown as `***` by `settings`, and
scrubbed from logs, errors and alerts.
"""

from dataclasses import dataclass, field
from typing import Tuple

from src.pipeline.config import EnvReader, ProviderSettings, SettingsError

DEFAULT_BILLING_API_URL = "https://cloudbilling.googleapis.com"
DEFAULT_BILLING_API_VERSION = "v2beta"
DEFAULT_BUSINESS_ENTITIES = ("businessEntities/GCP",)
MAX_PAGE_SIZE = 5000


@dataclass(frozen=True)
class GcpSettings(ProviderSettings):
    SECRET_FIELDS = ("api_key",)

    billing_api_url: str = DEFAULT_BILLING_API_URL
    billing_api_version: str = DEFAULT_BILLING_API_VERSION
    page_size: int = MAX_PAGE_SIZE
    business_entities: Tuple[str, ...] = DEFAULT_BUSINESS_ENTITIES
    api_key: str = field(default=None, repr=False)

    @classmethod
    def from_env(cls, env: EnvReader) -> "GcpSettings":
        key = env.get("GCP_PRICING_API_KEY")
        if not key:
            raise SettingsError("GCP_PRICING_API_KEY is required when PIPELINE_PROVIDER=gcp")
        entities = tuple(
            e.strip() for e in env.get("GCP_BILLING_BUSINESS_ENTITIES", ",".join(DEFAULT_BUSINESS_ENTITIES)).split(",")
            if e.strip()
        )
        return cls(
            billing_api_url=env.as_url("GCP_BILLING_API_URL", DEFAULT_BILLING_API_URL),
            billing_api_version=env.get("GCP_BILLING_API_VERSION", DEFAULT_BILLING_API_VERSION)
            or DEFAULT_BILLING_API_VERSION,
            page_size=env.as_int("GCP_BILLING_PAGE_SIZE", MAX_PAGE_SIZE, 1, MAX_PAGE_SIZE),
            business_entities=entities or DEFAULT_BUSINESS_ENTITIES,
            api_key=key,
        )
