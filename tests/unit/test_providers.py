"""
Provider registry (data-model.md § ProviderSpec, research R8; feature 004 T011).
"""

import subprocess
import sys

import pytest

from src.pipeline import providers
from src.pipeline.providers import DEFAULT_PROVIDER, PROVIDERS, get_provider


def test_known_providers():
    assert sorted(PROVIDERS) == ["aws", "azure", "gcp"]
    assert DEFAULT_PROVIDER == "aws"


@pytest.mark.parametrize(
    "name,tables",
    [
        ("aws", ["service_dim", "product_dim", "product_attribute", "region_dim", "price_fact"]),
        ("azure", ["price_item", "price_item_savings_plan"]),
        ("gcp", ["service", "sku", "sku_price", "sku_price_tier"]),
    ],
)
def test_tables_and_schema_versions(name, tables):
    spec = get_provider(name)
    assert list(spec.table_names) == tables
    assert set(spec.tables.values()) == {1}


def test_download_models_and_always_collected():
    assert [PROVIDERS[p].download_model for p in ("aws", "azure", "gcp")] == ["per_location", "per_location", "catalog"]
    assert PROVIDERS["aws"].always_collected == ()
    assert PROVIDERS["azure"].always_collected == ("global",)
    assert PROVIDERS["gcp"].always_collected == ("global",)


def test_default_pacing():
    assert PROVIDERS["aws"].rate_limit_per_second is None
    assert (PROVIDERS["azure"].rate_limit_per_second, PROVIDERS["azure"].max_concurrent_requests) == (0.5, 1)
    assert (PROVIDERS["gcp"].rate_limit_per_second, PROVIDERS["gcp"].max_concurrent_requests) == (1.0, 1)


def test_settings_classes_and_legacy_dir():
    assert PROVIDERS["aws"].settings is None
    assert PROVIDERS["azure"].settings == "src.azure.config:AzureSettings"
    assert PROVIDERS["gcp"].settings == "src.gcp.config:GcpSettings"
    assert PROVIDERS["aws"].legacy_parquet_dir == "pricing_aws/parquet"
    assert PROVIDERS["azure"].legacy_parquet_dir is None
    assert PROVIDERS["gcp"].legacy_parquet_dir is None


def test_default_locations():
    from src.aws.regions import DEFAULT_PRICING_REGIONS

    assert PROVIDERS["aws"].locations() == DEFAULT_PRICING_REGIONS
    assert PROVIDERS["azure"].locations() == [
        "eastus", "eastus2", "westus", "westus2", "westeurope", "uksouth", "japaneast",
        "north-america", "europe", "asia", "intercontinental",
    ]
    assert PROVIDERS["gcp"].locations() == [
        "us-east1", "us-east4", "us-west1", "us-west2", "europe-west1", "europe-west2", "asia-northeast1",
    ]


def test_aws_classes_resolve_to_the_shared_base_classes():
    from src.aws.transformations import AwsTransform
    from src.pipeline.config import ProviderSettings
    from src.pipeline.downloader import Downloader
    from src.pipeline.transformations import LocationTransform

    spec = PROVIDERS["aws"]
    assert issubclass(spec.downloader_class(), Downloader)
    assert issubclass(spec.transform_class(), LocationTransform)
    assert spec.settings_class() is ProviderSettings
    assert AwsTransform.TABLE_SCHEMA_VERSIONS == spec.tables


def test_importing_the_registry_loads_no_provider_package():
    code = (
        "import sys, src.pipeline.providers as p; "
        "loaded = [m for m in sys.modules if m.split('.')[:2] in (['src','aws'],['src','azure'],['src','gcp'])]; "
        "print(','.join(loaded))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""


def test_unknown_provider():
    with pytest.raises(ValueError):
        get_provider("x")
    with pytest.raises(ValueError):
        providers.tables_for("x")


def test_azure_classes_resolve_to_the_shared_base_classes():
    from src.azure.config import AzureSettings
    from src.pipeline.downloader import Downloader
    from src.pipeline.transformations import LocationTransform

    spec = PROVIDERS["azure"]
    assert issubclass(spec.downloader_class(), Downloader)
    assert issubclass(spec.transform_class(), LocationTransform)
    assert spec.settings_class() is AzureSettings
    assert spec.transform_class().TABLE_SCHEMA_VERSIONS == spec.tables


def test_gcp_classes_resolve_to_the_shared_base_classes():
    from src.gcp.config import GcpSettings
    from src.pipeline.downloader import CatalogDownloader
    from src.pipeline.transformations import LocationTransform

    spec = PROVIDERS["gcp"]
    assert issubclass(spec.downloader_class(), CatalogDownloader)
    assert issubclass(spec.transform_class(), LocationTransform)
    assert spec.settings_class() is GcpSettings
    assert spec.transform_class().TABLE_SCHEMA_VERSIONS == spec.tables
