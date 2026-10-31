# Data Model: Azure and GCP Pricing Snapshots

**Feature**: 004-add-azure-gcp-pricing | **Date**: 2026-10-29

The entities from feature 003 (Snapshot, ManifestRevision, Active Manifest, LatestPointer, RunClaim, Run, RawObject; see `specs/003-pipeline-cloud-deployment/data-model.md`) are unchanged in behavior and now exist once per provider. This document covers what is new:

- the **Location** model
- the **ProviderSpec**
- the Azure and GCP **tables**
- the **manifest 1.1** additions
- the GCP **catalog counters**

## Location (FR-013)

A location is where a price applies, and it names the partition a row is filed under (`region=<location>` in the layout).

| Field | Rule |
|---|---|
| `location` (partition) | `^[a-z0-9]+(-[a-z0-9]+)*$`. One of: a configured region, a configured multi-region location, or `global`. |
| source location column(s) | The source's own value(s), unchanged. `null` where the source gives none. |

**Filing rules**

| Provider | Source value | Filed under |
|---|---|---|
| Azure | `armRegionName` = configured region or geography (after mapping) | that location |
| Azure | `armRegionName` = `""` or `Global` | `global` (counted in `unlocated_rows` only when `""`) |
| GCP | `geoTaxonomy` regional or multi-regional: each listed region (lowercased) that is configured | each of those locations (one `sku` row per location) |
| GCP | `geoTaxonomy.type` = `TYPE_GLOBAL` | `global` |
| GCP | no `geoTaxonomy` | `global`, counted in `unlocated_rows` and `catalog.skus_unlocated` |
| GCP | regions listed, none configured | not stored for this snapshot (counted in `catalog.skus_unconfigured_locations`) |

**GCP name mapping** (R6): lowercase only (`US` → `us`); the source value is kept in `geo_regions`.

**Azure name mapping** (R3): lowercase, then replace runs of spaces with `-`. For example, `North America` → `north-america`. The mapping can be reversed through the `arm_region_name` column on every row. If two different source values map to the same partition name, it's a transform error: the location fails with a reason, so the collision can't pass silently.

## ProviderSpec (`src/pipeline/providers.py`, R8)

| Field | AWS | Azure | GCP |
|---|---|---|---|
| `name` | `aws` | `azure` | `gcp` |
| `tables` → schema version | `service_dim`, `product_dim`, `product_attribute`, `region_dim`, `price_fact` → 1 | `price_item`, `price_item_savings_plan` → 1 | `service`, `sku`, `sku_price`, `sku_price_tier` → 1 |
| `default_locations` | current 7 AWS regions | R3 list | R6 list |
| `download_model` | `per_location` | `per_location` | `catalog` |
| `always_collected` | — | `global` | `global` |
| default pacing (rate/s, concurrency) | unchanged (AWS client) | 0.5, 1 | 1.0, 1 |
| `downloader` (import path; a `Downloader` or `CatalogDownloader` subclass) | `src.aws.downloader:AwsPricingDownloader` | `src.azure.downloader:AzurePricesDownloader` | `src.gcp.downloader:GcpCatalogDownloader` |
| `transform` (import path; a `LocationTransform` subclass) | `src.aws.transformations:AwsTransform` | `src.azure.transformations:AzureTransform` | `src.gcp.transformations:GcpTransform` |
| `settings` (import path; a `ProviderSettings` subclass) | — (base `ProviderSettings`) | `src.azure.config:AzureSettings` | `src.gcp.config:GcpSettings` (secret: `GCP_PRICING_API_KEY`) |
| `legacy_parquet_dir` (`upload-history`) | `pricing_aws/parquet` | — | — |

`DEFAULT_PROVIDER` is `aws`. Import paths are resolved only when used, so importing `providers.py` loads no provider package.

Validation: `PIPELINE_PROVIDER` must name a spec. A location that's configured but unknown to the source fails only that location (spec edge case): for Azure, an empty first listing; for GCP, no in-scope SKU lists it.

## Azure tables (FR-014)

Partition: `azure/parquet/<table>/snapshot_date=<D>/region=<location>/part-<run_id>.parquet`.

### `price_item` (schema version 1): one row per item the source returned for the location

| Column | Type | Source | Notes |
|---|---|---|---|
| `price_item_key` | string | derived | sha256 (hex, first 32 chars) of the canonical JSON of the item without `savingsPlan`. It's a join key only. |
| `snapshot_date` | date32 | run | |
| `location` | string | partition | |
| `arm_region_name` | string | `armRegionName` | `""` is kept as `""` |
| `location_display_name` | string | `location` | |
| `service_family` | string | `serviceFamily` | |
| `service_name` | string | `serviceName` | |
| `service_id` | string | `serviceId` | |
| `product_id` | string | `productId` | |
| `product_name` | string | `productName` | |
| `sku_id` | string | `skuId` | |
| `sku_name` | string | `skuName` | |
| `arm_sku_name` | string | `armSkuName` | e.g. `Standard_D4s_v5`; `""` kept |
| `meter_id` | string | `meterId` | |
| `meter_name` | string | `meterName` | |
| `price_type` | string | `type` | `Consumption`, `DevTestConsumption`, `Reservation`, … as published |
| `reservation_term` | string, nullable | `reservationTerm` | e.g. `1 Year`; null when absent |
| `unit_of_measure` | string | `unitOfMeasure` | e.g. `1 Hour`, `1 GB/Month` |
| `tier_minimum_units` | float64, nullable | `tierMinimumUnits` | tier lower bound; null + counted if unparseable |
| `currency_code` | string | `currencyCode` | |
| `unit_price` | float64, nullable | `unitPrice` | null + counted if unparseable |
| `retail_price` | float64, nullable | `retailPrice` | null + counted if unparseable |
| `effective_start_date` | timestamp[ms, UTC] | `effectiveStartDate` | |
| `effective_end_date` | timestamp[ms, UTC], nullable | `effectiveEndDate` | present on some items |
| `is_primary_meter_region` | bool, nullable | `isPrimaryMeterRegion` | null when absent |
| `extra_fields` | string (JSON), nullable | other keys | any field not listed above, kept verbatim so new source fields aren't lost |

**Duplicates**: rows with identical content (the same `price_item_key`) within one location are kept once, and the number removed is recorded in `duplicates_removed`. Rows that share identifying fields but differ in price are all kept.

### `price_item_savings_plan` (schema version 1): one row per `savingsPlan` entry

| Column | Type | Source |
|---|---|---|
| `price_item_key` | string | joins to `price_item` |
| `snapshot_date` | date32 | run |
| `location` | string | partition |
| `term` | string | `savingsPlan[].term` (e.g. `1 Year`, `3 Years`) |
| `unit_price` | float64, nullable | `savingsPlan[].unitPrice` |
| `retail_price` | float64, nullable | `savingsPlan[].retailPrice` |

**Lookup example** (contract): the Linux pay-as-you-go hourly price of `Standard_D4s_v5` in `eastus` is the `price_item` row with:

- `location = 'eastus'`
- `arm_sku_name = 'Standard_D4s_v5'`
- `price_type = 'Consumption'`
- `service_name = 'Virtual Machines'`
- `product_name` not containing `Windows`, and `sku_name` not containing `Spot` or `Low Priority`

The Windows, Spot and Low Priority distinctions exist only in the source's text, so the contract documents them as text filters.

## GCP tables (FR-015)

Partition: `gcp/parquet/<table>/snapshot_date=<D>/region=<location>/part-<run_id>.parquet`. Source: the v2beta catalog (research R4), plus the v1 services list for `service`.

### `service` (schema version 1): one row per in-scope service; `global` partition only

The manifest lists `service` for every location; for locations other than `global` the entry has no files and `row_count` 0.

| Column | Type | Source (`/v1/services`) |
|---|---|---|
| `snapshot_date` | date32 | run |
| `service_id` | string | `serviceId` |
| `service_name` | string | `name` (`services/<id>`) |
| `display_name` | string | `displayName` |
| `business_entity_name` | string | `businessEntityName` |

### `sku` (schema version 1): one row per SKU per location it is filed under

| Column | Type | Source (`/v2beta/skus`) |
|---|---|---|
| `snapshot_date` | date32 | run |
| `location` | string | partition |
| `sku_id` | string | `skuId` |
| `sku_name` | string | `name` (`skus/<id>`) |
| `service_id` | string | `service` without the `services/` prefix |
| `display_name` | string | `displayName` (e.g. `N2 Instance Core running in Americas`) |
| `product_taxonomy` | list<string>, nullable | `productTaxonomy.taxonomyCategories[].category`, in source order (e.g. `GCP, Compute, GCE, VMs On Demand, Cores: Per Core, N2`); null when the source gives none |
| `geo_taxonomy_type` | string, nullable | `geoTaxonomy.type` (`TYPE_REGIONAL`, `TYPE_MULTI_REGIONAL`, `TYPE_GLOBAL`); null when absent |
| `geo_regions` | list<string>, nullable | the region(s) in `regionalMetadata` or `multiRegionalMetadata`, as published |
| `extra_fields` | string (JSON), nullable | unlisted fields, verbatim |

### `sku_price` (schema version 1): one row per `skuPrices` entry, per filed location

| Column | Type | Source (`/v2beta/skus/-/prices`) |
|---|---|---|
| `snapshot_date`, `location`, `sku_id` | | as above (`sku_id` from `name` `skus/<id>/price`) |
| `consumption_model` | string | `consumptionModel` without the `consumptionModels/` prefix |
| `consumption_model_description` | string | e.g. `Default`, `Compute Flexible CUD - 1 Year` |
| `value_type` | string | `valueType` (`rate`, `unspecified`) |
| `currency_code` | string | `currencyCode` |
| `unit` | string, nullable | `rate.unitInfo.unit` (e.g. `h`, `GiBy.mo`) |
| `unit_description` | string, nullable | `rate.unitInfo.unitDescription` |
| `unit_quantity` | decimal128(38, 9), nullable | `rate.unitInfo.unitQuantity.value` (e.g. `1`, `1000000`); null + counted if malformed |
| `aggregation_level` | string, nullable | `rate.aggregationInfo.level` |
| `aggregation_interval` | string, nullable | `rate.aggregationInfo.interval` |
| `extra_fields` | string (JSON), nullable | unlisted fields, verbatim |

Rate fields are null when `value_type` isn't `rate` (319 prices on 2026-10-30).

### `sku_price_tier` (schema version 1): one row per tier of a price, per filed location

| Column | Type | Source |
|---|---|---|
| `snapshot_date`, `location`, `sku_id`, `consumption_model` | | joins to `sku_price` |
| `start_amount` | decimal128(38, 9), nullable | `rate.tiers[].startAmount.value` (tier lower bound, in units of `unit_quantity` × `unit`; an absent value is `0`, as in the source's JSON encoding); null + counted if malformed |
| `currency_code` | string | `listPrice.currencyCode` |
| `list_price` | decimal128(38, 9), nullable | exactly `units + nanos / 10^9`; null + counted if malformed |

**Lookup example** (contract): the on-demand price of an `n2-standard-4` in `us-east1` is built from two SKUs:

- the `sku` rows with `location = 'us-east1'` whose `product_taxonomy` contains `VMs On Demand` and `N2`, and whose `display_name` starts with `N2 Instance Core` (taxonomy `Cores: Per Core`) or `N2 Instance Ram` (taxonomy `Memory: Per GB`). The taxonomy alone also matches sole-tenancy and custom-machine SKUs, so the display name is needed; both SKUs are `TYPE_MULTI_REGIONAL`, listing `us-central1`, `us-east1` and `us-west1`
- each SKU's `sku_price` row with `consumption_model_description = 'Default'`, and its `sku_price_tier` row with `start_amount = 0`

The price is 4 × (core price per hour) + 16 × (RAM price per GiB-hour). The 1- and 3-year flexible CUD prices come from the same SKUs' `Compute Flexible CUD - 1 Year` / `- 3 Year` prices. GCP publishes machine prices only per vCPU and per GiB, so this arithmetic belongs to the consumer and is documented, not precomputed. The values above were checked against the 2026-10-30 catalog.

## Manifest 1.1 additions (FR-010–FR-012, FR-031, FR-032, FR-034, FR-035)

All additions are optional. AWS manifests omit them.

```text
manifest_version: "1.1"
regions.requested / succeeded / failed[].region    location names (wider pattern)
tables.<name>                                      provider's own table names
run.region_results[] (per location):
    items_received        int   items the source returned for this location (Azure) or SKUs filed to it (GCP)
    duplicates_removed    int
    unlocated_rows        int   rows filed under global because the source gave no location
    throttled_responses   int   (Azure; GCP reports for the catalog)
    requests_made         int   requests sent for this location, retries included (Azure; FR-034)
excluded_price_types (Azure): [string]   e.g. ["DevTestConsumption"] when INCLUDE_AZURE_DEVTEST_PRICES=false (FR-035)
catalog (GCP):
    api_version: "v2beta"
    requests_made: int            whole catalog, retries included (FR-034)
    throttled_responses: int
    skus_received: int
    skus_out_of_scope: int        services outside the configured business entities (e.g. Maps)
    skus_unconfigured_locations: int
    skus_unlocated: int           no geoTaxonomy; filed under global
    skus_without_price: int       stored with no price rows
    prices_without_sku: int       not stored
raw.<unit>                                         Azure: one entry per location; GCP: one entry "catalog"
```

## Snapshot status (extends feature 003 `derive_status`)

| Condition | Status |
|---|---|
| Every requested location has tables | `succeeded` |
| At least one location has tables, and a location failed | `partial` |
| No location has tables (including a GCP catalog that couldn't be downloaded completely) | `failed` |
| Retention purged it | `purged` |

`global` counts as a requested location for every Azure and GCP run. `latest.json` moves only on `succeeded`, as before.

## Settings (contracts/configuration.md)

Shared settings are read by `src/pipeline/config.py`. Provider-specific settings are read by the provider's settings class (`src/azure/config.py`, `src/gcp/config.py`), loaded through the spec; a provider's secrets are the fields its class lists in `SECRET_FIELDS`, which `settings` shows as `***` and the logs redact.

| Setting | Default | Notes |
|---|---|---|
| `PIPELINE_PROVIDER` | `aws` | `aws`, `azure` or `gcp` |
| `PRICING_REGIONS` | the provider's default locations | It accepts locations of any kind. The name is kept so existing configuration keeps working. |
| `PRICING_DOWNLOAD_RATE_LIMIT_PER_SECOND` | per provider (Azure 0.5, GCP 1.0) | |
| `PRICING_DOWNLOAD_MAX_CONCURRENT_REQUESTS` | 1 | |
| `AZURE_PRICES_API_URL` | `https://prices.azure.com/api/retail/prices` | |
| `AZURE_PRICES_API_VERSION` | `2023-01-01-preview` | |
| `INCLUDE_AZURE_DEVTEST_PRICES` | `true` | `false` drops `DevTestConsumption` items at the source (FR-035) |
| `PRICING_CURRENCY` | `USD` | Used by Azure and GCP; AWS publishes USD only. |
| `GCP_BILLING_API_URL` | `https://cloudbilling.googleapis.com` | |
| `GCP_BILLING_API_VERSION` | `v2beta` | SKU and price listings; the services list always uses `v1` |
| `GCP_BILLING_PAGE_SIZE` | 5000 | |
| `GCP_BILLING_BUSINESS_ENTITIES` | `businessEntities/GCP` | comma-separated |
| `GCP_PRICING_API_KEY` | none (required for `gcp`) | secret; masked by `settings`; never logged |
