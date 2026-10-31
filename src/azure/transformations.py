"""
Azure Retail Prices pages → `price_item` and `price_item_savings_plan`
(data-model.md § Azure tables; FR-013, FR-014, FR-017).

The tables follow the source: one row per published item, split only where the source
has a one-to-many relationship (an item's savings plan prices). Values are stored as
published; a price that isn't a number becomes null and is counted, never estimated.
"""

import datetime as dt
import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple

import pyarrow as pa

from src.pipeline.transformations import LocationTransform, TransformResult, write_table

GLOBAL = "global"

PRICE_ITEM_SCHEMA = pa.schema(
    [
        ("price_item_key", pa.string()),
        ("snapshot_date", pa.date32()),
        ("location", pa.string()),
        ("arm_region_name", pa.string()),
        ("location_display_name", pa.string()),
        ("service_family", pa.string()),
        ("service_name", pa.string()),
        ("service_id", pa.string()),
        ("product_id", pa.string()),
        ("product_name", pa.string()),
        ("sku_id", pa.string()),
        ("sku_name", pa.string()),
        ("arm_sku_name", pa.string()),
        ("meter_id", pa.string()),
        ("meter_name", pa.string()),
        ("price_type", pa.string()),
        ("reservation_term", pa.string()),
        ("unit_of_measure", pa.string()),
        ("tier_minimum_units", pa.float64()),
        ("currency_code", pa.string()),
        ("unit_price", pa.float64()),
        ("retail_price", pa.float64()),
        ("effective_start_date", pa.timestamp("ms", tz="UTC")),
        ("effective_end_date", pa.timestamp("ms", tz="UTC")),
        ("is_primary_meter_region", pa.bool_()),
        ("extra_fields", pa.string()),
    ]
)

SAVINGS_PLAN_SCHEMA = pa.schema(
    [
        ("price_item_key", pa.string()),
        ("snapshot_date", pa.date32()),
        ("location", pa.string()),
        ("term", pa.string()),
        ("unit_price", pa.float64()),
        ("retail_price", pa.float64()),
    ]
)

# Source field → column, for the fields price_item stores directly.
_TEXT_FIELDS = {
    "armRegionName": "arm_region_name",
    "location": "location_display_name",
    "serviceFamily": "service_family",
    "serviceName": "service_name",
    "serviceId": "service_id",
    "productId": "product_id",
    "productName": "product_name",
    "skuId": "sku_id",
    "skuName": "sku_name",
    "armSkuName": "arm_sku_name",
    "meterId": "meter_id",
    "meterName": "meter_name",
    "type": "price_type",
    "reservationTerm": "reservation_term",
    "unitOfMeasure": "unit_of_measure",
    "currencyCode": "currency_code",
}
_KNOWN_FIELDS = set(_TEXT_FIELDS) | {
    "tierMinimumUnits",
    "unitPrice",
    "retailPrice",
    "effectiveStartDate",
    "effectiveEndDate",
    "isPrimaryMeterRegion",
    "savingsPlan",
}


def partition_name(arm_region_name: str) -> str:
    """Source location → partition name: lowercase, runs of spaces → `-`; `""`/`Global` → global."""
    if arm_region_name in ("", "Global"):
        return GLOBAL
    return re.sub(r"\s+", "-", arm_region_name.strip().lower())


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def price_item_key(item: dict) -> str:
    return hashlib.sha256(_canonical({k: v for k, v in item.items() if k != "savingsPlan"}).encode()).hexdigest()[:32]


def _number(value: Any) -> Tuple[Optional[float], bool]:
    """(value, unparseable). Absent stays null without counting; non-numbers are counted."""
    if value is None:
        return None, False
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, True
    return float(value), False


def _timestamp(value: Any) -> Optional[dt.datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


class AzureTransform(LocationTransform):
    TABLE_SCHEMA_VERSIONS = {"price_item": 1, "price_item_savings_plan": 1}

    def transform_location(self, loaded, location, staging_dir, run_id, snapshot_date) -> TransformResult:
        result = TransformResult()
        items: List[dict] = []
        for path in loaded:
            try:
                with open(path, encoding="utf-8") as fh:
                    items += json.load(fh).get("Items") or []
            except (OSError, ValueError) as e:
                result.errors.append(f"unreadable page {path.rsplit('/', 1)[-1]}: {e}"[:300])
        if result.errors:
            result.ok = False
            return result

        day = dt.date.fromisoformat(snapshot_date)
        seen = set()
        sources: Dict[str, set] = {}
        rows, plans = [], []
        duplicates = unlocated = unparseable = 0
        for item in items:
            fingerprint = _canonical(item)
            if fingerprint in seen:
                duplicates += 1
                continue
            seen.add(fingerprint)
            source_location = item.get("armRegionName")
            if source_location is None:
                source_location = ""
            mapped = partition_name(source_location)
            if mapped != location:
                result.errors.append(f"item for {source_location!r} returned in the {location!r} listing")
                continue
            if source_location not in ("", "Global"):
                sources.setdefault(mapped, set()).add(source_location)
            if source_location == "":
                unlocated += 1

            key = price_item_key(item)
            row = {"price_item_key": key, "snapshot_date": day, "location": location}
            for field, column in _TEXT_FIELDS.items():
                value = item.get(field)
                row[column] = value if isinstance(value, str) or value is None else str(value)
            for field, column in (("tierMinimumUnits", "tier_minimum_units"), ("unitPrice", "unit_price"),
                                  ("retailPrice", "retail_price")):
                row[column], bad = _number(item.get(field))
                unparseable += bad
            row["effective_start_date"] = _timestamp(item.get("effectiveStartDate"))
            row["effective_end_date"] = _timestamp(item.get("effectiveEndDate"))
            primary = item.get("isPrimaryMeterRegion")
            row["is_primary_meter_region"] = primary if isinstance(primary, bool) else None
            extra = {k: v for k, v in item.items() if k not in _KNOWN_FIELDS}
            row["extra_fields"] = _canonical(extra) if extra else None
            rows.append(row)

            for plan in item.get("savingsPlan") or []:
                unit_price, bad_unit = _number(plan.get("unitPrice"))
                retail_price, bad_retail = _number(plan.get("retailPrice"))
                unparseable += bad_unit + bad_retail
                plans.append(
                    {
                        "price_item_key": key,
                        "snapshot_date": day,
                        "location": location,
                        "term": plan.get("term"),
                        "unit_price": unit_price,
                        "retail_price": retail_price,
                    }
                )

        for mapped, values in sources.items():
            if len(values) > 1:
                result.errors.append(
                    f"source locations {', '.join(repr(v) for v in sorted(values))} all map to partition {mapped!r}"
                )
        result.counters = {"items_received": len(items), "duplicates_removed": duplicates, "unlocated_rows": unlocated}
        result.unparseable_prices = unparseable
        if result.errors:
            result.ok = False
            return result

        result.files = [
            write_table("price_item", location, pa.Table.from_pylist(rows, schema=PRICE_ITEM_SCHEMA), staging_dir, run_id),
            write_table(
                "price_item_savings_plan",
                location,
                pa.Table.from_pylist(plans, schema=SAVINGS_PLAN_SCHEMA),
                staging_dir,
                run_id,
            ),
        ]
        return result
