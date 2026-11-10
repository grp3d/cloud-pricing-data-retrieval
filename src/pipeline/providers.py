"""
Provider registry (research R8; constitution v1.3.0 Principle VI).

The only module in src/pipeline/ that names providers. Each `ProviderSpec` says what the
shared pipeline needs to know about a provider: its tables and schema versions, default
locations, download model and pacing, and where its classes live. Classes are given as
import paths and resolved only when used, so importing this module loads no provider
package, and provider code stays in src/<provider>/.
"""

import importlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

PER_LOCATION = "per_location"
CATALOG = "catalog"
CATALOG_UNIT = "catalog"  # raw unit name for the catalog download model


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    tables: Dict[str, int]  # table name -> schema version, in publishing order
    default_locations: Any  # a tuple of locations, or an import path to a list
    downloader: str  # import path of a Downloader / CatalogDownloader subclass
    transform: str  # import path of a LocationTransform subclass
    download_model: str = PER_LOCATION
    always_collected: Tuple[str, ...] = ()
    rate_limit_per_second: Optional[float] = None  # None: the client's own pacing
    max_concurrent_requests: int = 1
    settings: Optional[str] = None  # import path of a ProviderSettings subclass; None: the base
    legacy_parquet_dir: Optional[str] = None  # pre-003 local layout accepted by upload-history
    table_names: Tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "table_names", tuple(self.tables))

    def locations(self) -> List[str]:
        if isinstance(self.default_locations, str):
            return list(resolve(self.default_locations))
        return list(self.default_locations)

    def downloader_class(self):
        return resolve(self.downloader)

    def transform_class(self):
        return resolve(self.transform)

    def settings_class(self):
        if self.settings is None:
            from src.pipeline.config import ProviderSettings

            return ProviderSettings
        return resolve(self.settings)


def resolve(path: str):
    module, _, attr = path.partition(":")
    return getattr(importlib.import_module(module), attr)


DEFAULT_PROVIDER = "aws"

PROVIDERS: Dict[str, ProviderSpec] = {
    "aws": ProviderSpec(
        name="aws",
        tables={"service_dim": 1, "product_dim": 1, "product_attribute": 1, "region_dim": 1, "price_fact": 1},
        default_locations="src.aws.regions:DEFAULT_PRICING_REGIONS",
        downloader="src.aws.downloader:AwsPricingDownloader",
        transform="src.aws.transformations:AwsTransform",
        legacy_parquet_dir="pricing_aws/parquet",
    ),
    "azure": ProviderSpec(
        name="azure",
        tables={"price_item": 1, "price_item_savings_plan": 1},
        default_locations=(
            "eastus", "eastus2", "westus", "westus2", "westeurope", "uksouth", "japaneast",
            "north-america", "europe", "asia", "intercontinental",
        ),
        downloader="src.azure.downloader:AzurePricesDownloader",
        transform="src.azure.transformations:AzureTransform",
        always_collected=("global",),
        rate_limit_per_second=0.5,
        settings="src.azure.config:AzureSettings",
    ),
    "gcp": ProviderSpec(
        name="gcp",
        tables={"service": 1, "sku": 1, "sku_price": 1, "sku_price_tier": 1},
        default_locations=(
            "us-east1", "us-east4", "us-west1", "us-west2", "europe-west1", "europe-west2", "asia-northeast1",
        ),
        downloader="src.gcp.downloader:GcpCatalogDownloader",
        transform="src.gcp.transformations:GcpTransform",
        download_model=CATALOG,
        always_collected=("global",),
        rate_limit_per_second=1.0,
        settings="src.gcp.config:GcpSettings",
    ),
}


class UnknownProvider(ValueError):
    pass


def get_provider(name: str) -> ProviderSpec:
    try:
        return PROVIDERS[name]
    except KeyError:
        raise UnknownProvider(f"unknown provider {name!r}; expected one of {', '.join(sorted(PROVIDERS))}")


def tables_for(provider: str) -> Tuple[str, ...]:
    return get_provider(provider).table_names
