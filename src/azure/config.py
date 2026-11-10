"""
Azure settings (contracts/configuration.md; FR-004, FR-035). The Retail Prices API needs
no credential, so there are no secret fields.
"""

from dataclasses import dataclass

from src.pipeline.config import EnvReader, ProviderSettings

DEFAULT_PRICES_API_URL = "https://prices.azure.com/api/retail/prices"
# Savings plan prices need this version or later (research R1).
DEFAULT_PRICES_API_VERSION = "2023-01-01-preview"


@dataclass(frozen=True)
class AzureSettings(ProviderSettings):
    prices_api_url: str = DEFAULT_PRICES_API_URL
    prices_api_version: str = DEFAULT_PRICES_API_VERSION
    include_devtest_prices: bool = True

    @classmethod
    def from_env(cls, env: EnvReader) -> "AzureSettings":
        return cls(
            prices_api_url=env.as_url("AZURE_PRICES_API_URL", DEFAULT_PRICES_API_URL),
            prices_api_version=env.get("AZURE_PRICES_API_VERSION", DEFAULT_PRICES_API_VERSION)
            or DEFAULT_PRICES_API_VERSION,
            include_devtest_prices=env.as_bool("INCLUDE_AZURE_DEVTEST_PRICES", True),
        )
