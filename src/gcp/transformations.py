"""
GCP Cloud Billing catalog → `service`, `sku`, `sku_price`, `sku_price_tier`
(data-model.md § GCP tables, § Location; FR-013, FR-015, FR-017, FR-031).

The catalog is parsed once per run (`load`) and every location is built from it. Filing:
a regional or multi-regional SKU goes under each region it lists (lowercased); a global
SKU, or one with no location, under `global`. Tables follow the source: services, SKUs
with their taxonomies, each SKU's prices (one per consumption model) and each price's
tiers. Money and quantities are exact decimals, never floats.
"""

import datetime as dt
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Set, Tuple

import pyarrow as pa

from src.pipeline.transformations import LocationTransform, TransformResult, write_table

GLOBAL = "global"
DECIMAL = pa.decimal128(38, 9)
NANO = Decimal("1e-9")

SERVICE_SCHEMA = pa.schema(
    [
        ("snapshot_date", pa.date32()),
        ("service_id", pa.string()),
        ("service_name", pa.string()),
        ("display_name", pa.string()),
        ("business_entity_name", pa.string()),
    ]
)
SKU_SCHEMA = pa.schema(
    [
        ("snapshot_date", pa.date32()),
        ("location", pa.string()),
        ("sku_id", pa.string()),
        ("sku_name", pa.string()),
        ("service_id", pa.string()),
        ("display_name", pa.string()),
        ("product_taxonomy", pa.list_(pa.string())),
        ("geo_taxonomy_type", pa.string()),
        ("geo_regions", pa.list_(pa.string())),
        ("extra_fields", pa.string()),
    ]
)
SKU_PRICE_SCHEMA = pa.schema(
    [
        ("snapshot_date", pa.date32()),
        ("location", pa.string()),
        ("sku_id", pa.string()),
        ("consumption_model", pa.string()),
        ("consumption_model_description", pa.string()),
        ("value_type", pa.string()),
        ("currency_code", pa.string()),
        ("unit", pa.string()),
        ("unit_description", pa.string()),
        ("unit_quantity", DECIMAL),
        ("aggregation_level", pa.string()),
        ("aggregation_interval", pa.string()),
        ("extra_fields", pa.string()),
    ]
)
SKU_PRICE_TIER_SCHEMA = pa.schema(
    [
        ("snapshot_date", pa.date32()),
        ("location", pa.string()),
        ("sku_id", pa.string()),
        ("consumption_model", pa.string()),
        ("start_amount", DECIMAL),
        ("currency_code", pa.string()),
        ("list_price", DECIMAL),
    ]
)

_SKU_FIELDS = {"name", "skuId", "displayName", "service", "productTaxonomy", "geoTaxonomy"}
_PRICE_FIELDS = {"consumptionModel", "consumptionModelDescription", "valueType", "rate"}


def _strip(value: Optional[str], prefix: str) -> Optional[str]:
    if isinstance(value, str) and value.startswith(prefix):
        return value[len(prefix):]
    return value


def _decimal(value: Any) -> Tuple[Optional[Decimal], bool]:
    """(exact value, unparseable). Absent → (None, False)."""
    if value is None:
        return None, False
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None, True
    try:
        d = Decimal(str(value).strip())
    except InvalidOperation:
        return None, True
    if not d.is_finite() or d != d.quantize(NANO) or d.adjusted() >= 29:
        return None, True
    return d.quantize(NANO), False


def _integer(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def list_price(money: Any) -> Tuple[Optional[Decimal], bool]:
    """Exactly `units + nanos / 10^9` (proto3 JSON: absent fields are zero)."""
    if not isinstance(money, dict):
        return None, True
    units = _integer(money.get("units", 0))
    nanos = _integer(money.get("nanos", 0))
    if units is None or nanos is None or abs(nanos) >= 10**9:
        return None, True
    return (Decimal(units) + Decimal(nanos) * NANO).quantize(NANO), False


def _regions(sku: dict) -> Optional[List[str]]:
    geo = sku.get("geoTaxonomy") or {}
    if geo.get("regionalMetadata"):
        region = (geo["regionalMetadata"].get("region") or {}).get("region")
        return [region] if region else []
    if geo.get("multiRegionalMetadata"):
        return [r.get("region") for r in geo["multiRegionalMetadata"].get("regions") or [] if r.get("region")]
    return None


@dataclass
class Catalog:
    services: Dict[str, dict] = field(default_factory=dict)  # by name (services/<id>)
    skus: Dict[str, dict] = field(default_factory=dict)  # by skuId, source order
    prices: Dict[str, dict] = field(default_factory=dict)  # by skuId
    filing: Dict[str, List[str]] = field(default_factory=dict)  # location -> in-scope skuIds
    unlocated: Set[str] = field(default_factory=set)
    out_of_scope: int = 0
    errors: List[str] = field(default_factory=list)

    def locations_with_skus(self) -> Set[str]:
        return set(self.filing)


class GcpTransform(LocationTransform):
    TABLE_SCHEMA_VERSIONS = {"service": 1, "sku": 1, "sku_price": 1, "sku_price_tier": 1}

    def load(self, raw_files: List[str]) -> Catalog:
        catalog = Catalog()
        listings = {"services": [], "skus": [], "prices": []}
        for path in sorted(raw_files):
            name = path.rsplit("/", 1)[-1]
            kind = name.split("-p", 1)[0]
            if kind not in listings:
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    listings[kind] += json.load(fh).get(kind) or []
            except (OSError, ValueError) as e:
                catalog.errors.append(f"unreadable catalog page {name}: {e}"[:300])
        catalog.services = {s.get("name"): s for s in listings["services"]}
        catalog.skus = {s.get("skuId"): s for s in listings["skus"] if s.get("skuId")}
        catalog.prices = {_strip(p.get("name"), "skus/").split("/", 1)[0]: p for p in listings["prices"] if p.get("name")}

        in_scope = set(self.settings.provider_settings.business_entities)
        for sku_id, sku in catalog.skus.items():
            service = catalog.services.get(sku.get("service")) or {}
            if service.get("businessEntityName") not in in_scope:
                catalog.out_of_scope += 1
                continue
            geo_type = (sku.get("geoTaxonomy") or {}).get("type")
            regions = _regions(sku)
            if geo_type == "TYPE_GLOBAL":
                targets = [GLOBAL]
            elif not regions:
                targets = [GLOBAL]
                catalog.unlocated.add(sku_id)
            else:
                targets = list(dict.fromkeys(r.lower() for r in regions))
            for location in targets:
                catalog.filing.setdefault(location, []).append(sku_id)
        return catalog

    def _stored(self, catalog: Catalog) -> Set[str]:
        configured = set(self.configured_locations) | {GLOBAL}
        return {s for loc, ids in catalog.filing.items() if loc in configured for s in ids}

    def run_counters(self, catalog: Catalog) -> Dict[str, int]:
        filed = {s for ids in catalog.filing.values() for s in ids}
        stored = self._stored(catalog)
        return {
            "skus_received": len(catalog.skus),
            "skus_out_of_scope": catalog.out_of_scope,
            "skus_unconfigured_locations": len(filed - stored),
            "skus_unlocated": len(catalog.unlocated),
            "skus_without_price": sum(1 for s in stored if s not in catalog.prices),
            "prices_without_sku": sum(1 for s in catalog.prices if s not in catalog.skus),
        }

    def transform_location(self, catalog: Catalog, location, staging_dir, run_id, snapshot_date) -> TransformResult:
        result = TransformResult()
        if catalog.errors:
            result.errors = list(catalog.errors)
            result.ok = False
            return result
        sku_ids = catalog.filing.get(location, [])
        if location != GLOBAL and not sku_ids:
            result.errors.append(f"location {location!r} is unknown to the source (no SKU lists it)")
            result.ok = False
            return result

        day = dt.date.fromisoformat(snapshot_date)
        services, skus, prices, tiers = [], [], [], []
        unparseable = 0
        if location == GLOBAL:
            in_scope = set(self.settings.provider_settings.business_entities)
            for s in catalog.services.values():
                if s.get("businessEntityName") in in_scope:
                    services.append(
                        {
                            "snapshot_date": day,
                            "service_id": s.get("serviceId"),
                            "service_name": s.get("name"),
                            "display_name": s.get("displayName"),
                            "business_entity_name": s.get("businessEntityName"),
                        }
                    )
        for sku_id in sku_ids:
            sku = catalog.skus[sku_id]
            categories = (sku.get("productTaxonomy") or {}).get("taxonomyCategories") or []
            extra = {k: v for k, v in sku.items() if k not in _SKU_FIELDS}
            skus.append(
                {
                    "snapshot_date": day,
                    "location": location,
                    "sku_id": sku_id,
                    "sku_name": sku.get("name"),
                    "service_id": _strip(sku.get("service"), "services/"),
                    "display_name": sku.get("displayName"),
                    "product_taxonomy": [c.get("category") for c in categories] or None,
                    "geo_taxonomy_type": (sku.get("geoTaxonomy") or {}).get("type"),
                    "geo_regions": _regions(sku) or None,
                    "extra_fields": json.dumps(extra, sort_keys=True) if extra else None,
                }
            )
            price = catalog.prices.get(sku_id)
            for entry in (price or {}).get("skuPrices") or []:
                model = _strip(entry.get("consumptionModel"), "consumptionModels/")
                rate = entry.get("rate") if entry.get("valueType") == "rate" else None
                unit_info = (rate or {}).get("unitInfo") or {}
                aggregation = (rate or {}).get("aggregationInfo") or {}
                quantity, bad = _decimal((unit_info.get("unitQuantity") or {}).get("value"))
                unparseable += bad
                extra = {k: v for k, v in entry.items() if k not in _PRICE_FIELDS}
                prices.append(
                    {
                        "snapshot_date": day,
                        "location": location,
                        "sku_id": sku_id,
                        "consumption_model": model,
                        "consumption_model_description": entry.get("consumptionModelDescription"),
                        "value_type": entry.get("valueType"),
                        "currency_code": price.get("currencyCode"),
                        "unit": unit_info.get("unit"),
                        "unit_description": unit_info.get("unitDescription"),
                        "unit_quantity": quantity,
                        "aggregation_level": aggregation.get("level"),
                        "aggregation_interval": aggregation.get("interval"),
                        "extra_fields": json.dumps(extra, sort_keys=True) if extra else None,
                    }
                )
                for tier in (rate or {}).get("tiers") or []:
                    start, bad_start = _decimal((tier.get("startAmount") or {}).get("value", "0"))
                    amount, bad_price = list_price(tier.get("listPrice"))
                    unparseable += bad_start + bad_price
                    tiers.append(
                        {
                            "snapshot_date": day,
                            "location": location,
                            "sku_id": sku_id,
                            "consumption_model": model,
                            "start_amount": start,
                            "currency_code": (tier.get("listPrice") or {}).get("currencyCode"),
                            "list_price": amount,
                        }
                    )

        result.unparseable_prices = unparseable
        result.counters = {
            "items_received": len(sku_ids),
            "unlocated_rows": sum(1 for s in sku_ids if s in catalog.unlocated),
        }
        result.files = [
            write_table("sku", location, pa.Table.from_pylist(skus, schema=SKU_SCHEMA), staging_dir, run_id),
            write_table("sku_price", location, pa.Table.from_pylist(prices, schema=SKU_PRICE_SCHEMA), staging_dir, run_id),
            write_table(
                "sku_price_tier", location, pa.Table.from_pylist(tiers, schema=SKU_PRICE_TIER_SCHEMA), staging_dir, run_id
            ),
        ]
        if location == GLOBAL:
            result.files.insert(
                0, write_table("service", location, pa.Table.from_pylist(services, schema=SERVICE_SCHEMA), staging_dir, run_id)
            )
        return result
