"""
AWS price-list JSON → per-region Parquet files for the five pricing tables.

AWS-specific transformation (constitution Principle VI). Files are written into a local
staging directory with run-specific names (`part-<run_id>.parquet`); the runner uploads
them and records each file's size, sha256 and row count in the snapshot manifest
(feature 003, FR-043). Completion is signalled by the manifest, not marker files.
"""

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from src.pipeline.layout import TABLES

logger = logging.getLogger(__name__)

# Bump a table's version whenever its columns change incompatibly (FR-018).
TABLE_SCHEMA_VERSIONS: Dict[str, int] = {table: 1 for table in TABLES}


@dataclass
class TransformRequest:
    json_files: List[str]
    staging_dir: str
    region: str
    run_id: str
    snapshot_date: str = ""
    log_callback: Optional[Callable[[str], None]] = None

    def __post_init__(self) -> None:
        if not self.snapshot_date:
            self.snapshot_date = datetime.now().strftime("%Y-%m-%d")


@dataclass
class TableFileResult:
    table: str
    region: str
    local_path: str
    bytes: int
    sha256: str
    row_count: int


@dataclass
class TransformResult:
    staging_dir: str
    snapshot_date: str = ""
    files: List[TableFileResult] = field(default_factory=list)
    row_counts: Dict[str, int] = field(default_factory=dict)
    tables_written: List[str] = field(default_factory=list)
    tables_skipped: List[str] = field(default_factory=list)
    unparseable_prices: int = 0
    parse_failed_files: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    success: bool = False


def _parse_products(
    data: dict,
    region: str,
    snapshot_date: str,
    seen_services: set,
    seen_regions: set,
    service_rows: list,
    region_rows: list,
    product_rows: list,
    attribute_rows: list,
) -> None:
    offer_code = data.get("offerCode", "")
    publication_date = data.get("publicationDate", "")
    products = data.get("products", {})
    promoted = frozenset(
        {"servicecode", "servicename", "regionCode", "location", "locationType"}
    )

    for sku, product in products.items():
        attrs = product.get("attributes", {})
        product_family = product.get("productFamily", "")
        service_code = attrs.get("servicecode", offer_code)
        service_name = attrs.get("servicename", "")

        if service_code not in seen_services:
            seen_services.add(service_code)
            service_rows.append(
                {
                    "snapshot_date": snapshot_date,
                    "service_code": service_code,
                    "service_name": service_name,
                    "offer_code": offer_code,
                    "publication_date": publication_date,
                }
            )

        region_code = attrs.get("regionCode", region)
        location = attrs.get("location", "")
        location_type = attrs.get("locationType", "")
        region_key = (region_code, location)
        if region_key not in seen_regions and region_code:
            seen_regions.add(region_key)
            region_rows.append(
                {
                    "snapshot_date": snapshot_date,
                    "region_code": region_code,
                    "location": location,
                    "location_type": location_type,
                }
            )

        if region_code and region_code != region:
            # AWS's per-region price list file is not reliably scoped to just
            # this region (observed heavily on AmazonEC2); drop rows that
            # carry an explicit regionCode belonging to another region so
            # this region's partition only contains this region's products.
            continue

        product_rows.append(
            {
                "snapshot_date": snapshot_date,
                "sku": sku,
                "service_code": service_code,
                "region_code": region_code or region,
                "product_family": product_family,
                "attributes_json": json.dumps(attrs),
            }
        )

        for attr_name, attr_value in attrs.items():
            if attr_name in promoted:
                continue
            attribute_rows.append(
                {
                    "snapshot_date": snapshot_date,
                    "sku": sku,
                    "service_code": service_code,
                    "attribute_name": attr_name,
                    "attribute_value": str(attr_value),
                }
            )


def _parse_terms(data: dict, snapshot_date: str, price_fact_rows: list) -> int:
    """Append price_fact rows; return how many prices were present but not numeric.

    Such prices are stored as null — never estimated or defaulted (constitution I).
    """
    unparseable = 0
    terms = data.get("terms", {})

    for term_type, sku_map in terms.items():
        if not isinstance(sku_map, dict):
            continue
        for offer_sku, offer_map in sku_map.items():
            if not isinstance(offer_map, dict):
                continue
            for _, term_detail in offer_map.items():
                term_attrs = term_detail.get("termAttributes", {})
                lease_contract_length = term_attrs.get("LeaseContractLength", "")
                purchase_option = term_attrs.get("PurchaseOption", "")

                for _, dim in term_detail.get("priceDimensions", {}).items():
                    price_per_unit = dim.get("pricePerUnit", {})
                    for currency, price_str in price_per_unit.items():
                        try:
                            price = float(price_str)
                        except (ValueError, TypeError):
                            price = None
                            unparseable += 1

                        price_fact_rows.append(
                            {
                                "snapshot_date": snapshot_date,
                                "sku": offer_sku,
                                "term": term_type,
                                "currency": currency,
                                "unit": dim.get("unit", ""),
                                "price": price,
                                "lease_contract_length": lease_contract_length,
                                "purchase_option": purchase_option,
                                "description": dim.get("description", ""),
                                "begin_range": dim.get("beginRange", ""),
                                "end_range": dim.get("endRange", ""),
                            }
                        )
    return unparseable


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_parquet_table(
    df: pd.DataFrame, table_name: str, request: TransformRequest
) -> TableFileResult:
    out_dir = os.path.join(
        request.staging_dir,
        table_name,
        f"snapshot_date={request.snapshot_date}",
        f"region={request.region}",
    )
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"part-{request.run_id}.parquet")
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), out_path, compression="snappy")
    return TableFileResult(
        table=table_name,
        region=request.region,
        local_path=out_path,
        bytes=os.path.getsize(out_path),
        sha256=_sha256(out_path),
        row_count=pq.ParquetFile(out_path).metadata.num_rows,
    )


def transform_pricing_to_parquet(request: TransformRequest) -> TransformResult:
    _log = request.log_callback or (lambda msg: logger.info(msg))
    result = TransformResult(staging_dir=request.staging_dir, snapshot_date=request.snapshot_date)

    service_rows: list = []
    region_rows: list = []
    product_rows: list = []
    attribute_rows: list = []
    price_fact_rows: list = []
    seen_services: set = set()
    seen_regions: set = set()

    _log(f"Snapshot date: {request.snapshot_date}")

    total = len(request.json_files)
    for idx, file_path in enumerate(request.json_files, 1):
        _log(f"[{idx}/{total}] Parsing {os.path.basename(file_path)}")
        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as e:
            msg = f"Skipping {file_path}: {e}"
            _log(f"WARNING: {msg}")
            result.errors.append(msg)
            result.parse_failed_files.append(file_path)
            continue

        _parse_products(
            data,
            request.region,
            request.snapshot_date,
            seen_services,
            seen_regions,
            service_rows,
            region_rows,
            product_rows,
            attribute_rows,
        )
        result.unparseable_prices += _parse_terms(data, request.snapshot_date, price_fact_rows)

    if result.unparseable_prices:
        _log(
            f"WARNING: {result.unparseable_prices} non-numeric price value(s) in "
            f"{request.region} stored as null"
        )

    table_data = {
        "service_dim": pd.DataFrame(service_rows),
        "region_dim": pd.DataFrame(region_rows),
        "product_dim": pd.DataFrame(product_rows),
        "product_attribute": pd.DataFrame(attribute_rows),
        "price_fact": pd.DataFrame(price_fact_rows),
    }

    for table_name, df in table_data.items():
        if df.empty:
            _log(f"No rows for {table_name}, skipping.")
            result.tables_skipped.append(table_name)
            result.row_counts[table_name] = 0
            continue
        try:
            written = _write_parquet_table(df, table_name, request)
        except Exception as e:
            msg = f"Failed writing {table_name}: {e}"
            _log(f"ERROR: {msg}")
            result.errors.append(msg)
            continue
        _log(f"Wrote {table_name}: {written.row_count:,} rows → {written.local_path}")
        result.files.append(written)
        result.row_counts[table_name] = written.row_count
        result.tables_written.append(table_name)

    result.success = bool(result.tables_written) and not result.parse_failed_files and not any(
        e.startswith("Failed writing") for e in result.errors
    )
    return result
