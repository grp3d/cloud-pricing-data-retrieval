# Data contract

This is what consumers such as `cloud-pricing-app` may rely on: the storage layout, the
manifest and `latest.json`, and each provider's tables. The machine-readable schemas are
[`src/pipeline/schemas/manifest.schema.json`](../src/pipeline/schemas/manifest.schema.json)
(manifest `1.1`) and [`src/pipeline/schemas/latest.schema.json`](../src/pipeline/schemas/latest.schema.json).

## How to read a snapshot

1. **Start from `<provider>/manifests/latest.json`** and read the `manifest_path` it names. For
   history, read `<provider>/manifests/<date>/manifest.json`.
2. **Find data files only through the manifest** (`tables.<t>.regions.<r>.files[].path`). Never
   list or glob `parquet/` folders: after a re-run they can briefly hold files from more than one
   revision, and a glob would double-count rows.
3. **Reject** a manifest whose `manifest_version` MAJOR is unsupported, and any table name or
   table `schema_version` you don't support for that provider.
4. **Skip** manifests with `status` `purged` or `failed`. Use `partial` only if you handle
   missing locations deliberately.
5. **Optionally verify** each file's `bytes` and `sha256` before use.
6. **Ignore** `claims/` and `revisions/`. They are internal and history, not a data source.
7. **Re-read the manifest** if you hold it longer than `SUPERSEDED_FILE_GRACE_MINUTES` (5).
   Files of a superseded revision may be deleted after that.
8. **Treat "access denied" like "not found"** for a missing key (for example `latest.json`
   before the first snapshot). The read-only policy can't list every prefix, so S3 reports a
   missing key as access denied.
9. **Read each provider independently.** One provider being `partial`, `failed` or stale must
   not hide the others.

## Storage layout

The same layout is used in S3 and in a local directory. `<root>` is the path or prefix in
`PIPELINE_STORAGE_URI` (`file:///data` → `/data`, `s3://bucket/` → the bucket root).

```text
<root>/
├── aws/
│   ├── raw/<YYYY-MM-DD>/<region>/<run_id>/pricing-<ServiceCode>-<region>.json.zst
│   ├── parquet/<table>/snapshot_date=<YYYY-MM-DD>/region=<location>/part-<run_id>[-<n>].parquet
│   ├── manifests/
│   │   ├── latest.json                     # → newest succeeded snapshot
│   │   └── <YYYY-MM-DD>/
│   │       ├── manifest.json               # current revision
│   │       └── revisions/<NNNN>.json       # immutable history, 0001…
│   └── claims/<YYYY-MM-DD>.json            # transient run claim (internal)
├── azure/
│   ├── raw/<YYYY-MM-DD>/<location>/<run_id>/prices-p0001.json.zst …   # one file per API page
│   └── parquet/ manifests/ claims/         # as aws
└── gcp/
    ├── raw/<YYYY-MM-DD>/catalog/<run_id>/  # whole catalog once per run
    │   ├── services-p0001.json.zst         # v1 services list
    │   ├── skus-p0001.json.zst …           # v2beta SKU pages
    │   └── prices-p0001.json.zst …         # v2beta price pages
    └── parquet/ manifests/ claims/         # as aws
```

Raw data expires after `RAW_RETENTION_DAYS` (30). Weekly Parquet snapshots are kept for
`PARQUET_WEEKLY_RETENTION_MONTHS` (12), then one per month.

## Producer guarantees

- **Write order**: data files, then `revisions/<NNNN>.json`, then `manifest.json`, then
  `latest.json`.
- **Immutable data files**: never modified after they're written; either listed by a manifest
  or deleted.
- **No dangling paths**: no active manifest lists a path that doesn't exist. Purging rewrites
  the manifest before deleting anything.
- **`latest.json` never goes backwards**: it names only `succeeded` snapshots, and a date that
  retention won't purge.
- **One run per provider and date**: a second concurrent run for the same provider and date is
  refused (claim object). Different providers never block each other.

## `latest.json`

| Field | Notes |
|---|---|
| `manifest_version` | Same as the manifests. |
| `provider` | `aws`, `azure` or `gcp`. |
| `snapshot_date` | The newest date whose current manifest is `succeeded`. Never moves backwards. |
| `revision` | The revision current when the pointer was written. Accept the manifest if its revision is `≥` this. |
| `run_id` | |
| `manifest_path` | `<provider>/manifests/<D>/manifest.json` |
| `updated_at` | RFC 3339 UTC |

## Manifest (version 1.1)

| Field | Notes |
|---|---|
| `manifest_version` | `"1.1"`. Reject an unsupported MAJOR. |
| `provider`, `snapshot_date` | |
| `run_id` | `^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$`, the run that wrote this revision. |
| `revision`, `previous_revision` | Strictly increasing from 1, no gaps. |
| `created_at` | When the revision was written; starts the superseded-file grace period. |
| `origin` | `pipeline` or `backfill` (`upload-history`). |
| `status` | `succeeded`, `partial`, `failed` or `purged` (see below). |
| `regions.requested` / `succeeded` / `failed[]` | Location names. `failed[]` has `{region, reason, attempts}`. |
| `run` | Provenance: `trigger`, `mode`, `host`, `started_at`, `ended_at`, `pipeline_version {git_sha, image_tag}`, and `region_results[]` for this run's locations. |
| `raw` | Map of raw unit → `{location, file_count, bytes, stored_at, purge_after}`. Azure has one entry per location, GCP one entry `catalog`. Null for backfills. |
| `tables.<t>` | `{schema_version, row_count, regions.<location> → {written_by_run, row_count, files[]}}`. Empty `{}` when `failed` or `purged`. |
| `files[]` | `{path, bytes, sha256, row_count}`. `path` is relative to the storage root. |
| `purged` | `{purged_at, reason}`, only when `status=purged`. |

**Optional fields** (ignore them if you don't need them; AWS manifests omit them):

- `run.region_results[]`: `items_received`, `duplicates_removed`, `unlocated_rows`,
  `throttled_responses`, `requests_made`.
- `excluded_price_types` (Azure): price types deliberately not collected, e.g.
  `["DevTestConsumption"]`.
- `catalog` (GCP): `api_version`, `requests_made`, `throttled_responses`, `skus_received`,
  `skus_out_of_scope`, `skus_unconfigured_locations`, `skus_unlocated`, `skus_without_price`,
  `prices_without_sku`.

**Status**

| Condition | Status |
|---|---|
| Every requested location has tables | `succeeded` |
| Some locations have tables, at least one failed | `partial` |
| No location has tables (including an incomplete GCP catalog download) | `failed` |
| Retention purged it | `purged` |

For Azure and GCP, `global` is always a requested location.

## Locations

- `region=<location>` partitions and `regions.*` hold **locations**: a region, a multi-region
  location (Azure geographies such as `north-america`), or `global`. Names match
  `^[a-z0-9]+(-[a-z0-9]+)*$`.
- **`global`** holds prices with no location. To price something in location *L*, read *L* and
  `global`. Some services (Azure support plans, GCP licensing) publish only global prices.
- **Azure** names are lowercased with spaces replaced by `-`; each row keeps the source value in
  `arm_region_name`.
- **GCP** multi- and dual-region SKUs are filed under each configured member region, marked
  `geo_taxonomy_type = 'TYPE_MULTI_REGIONAL'`. Count distinct `sku_id`, not rows, when counting
  SKUs across partitions.

## Tables

Tables are provider-specific and deliberately not shaped alike. Normalizing across providers
is the consumer's job. All tables are schema version `1`.

**Prices**: a price the source omits, publishes in another currency than `PRICING_CURRENCY`
(USD), or that isn't a valid number is stored as **null** and counted in
`unparseable_prices`. Treat null as "not published", never as zero. AWS and Azure prices are
`float64`; GCP prices are `decimal128(38, 9)`.

### AWS

| Table | Columns |
|---|---|
| `service_dim` | `snapshot_date`, `service_code`, `service_name`, `offer_code`, `publication_date` |
| `region_dim` | `snapshot_date`, `region_code`, `location`, `location_type` |
| `product_dim` | `snapshot_date`, `sku`, `service_code`, `region_code`, `product_family`, `attributes_json` |
| `product_attribute` | `snapshot_date`, `sku`, `service_code`, `attribute_name`, `attribute_value` |
| `price_fact` | `snapshot_date`, `sku`, `term`, `currency`, `unit`, `price`, `lease_contract_length`, `purchase_option`, `description`, `begin_range`, `end_range` |

### Azure (Retail Prices API)

**`price_item`**: one row per item returned for the location. Identical items within a location
are kept once (`duplicates_removed`).

| Column | Type | Notes |
|---|---|---|
| `price_item_key` | string | Join key to `price_item_savings_plan`. |
| `snapshot_date`, `location` | date32, string | |
| `arm_region_name`, `location_display_name` | string | Source location values. |
| `service_family`, `service_name`, `service_id` | string | |
| `product_id`, `product_name` | string | |
| `sku_id`, `sku_name`, `arm_sku_name` | string | e.g. `Standard_D4s_v5` |
| `meter_id`, `meter_name` | string | |
| `price_type` | string | `Consumption`, `DevTestConsumption`, `Reservation`, … |
| `reservation_term` | string, nullable | e.g. `1 Year` |
| `unit_of_measure` | string | e.g. `1 Hour`, `1 GB/Month` |
| `tier_minimum_units` | float64, nullable | Tier lower bound (one row per tier). |
| `currency_code` | string | |
| `unit_price`, `retail_price` | float64, nullable | |
| `effective_start_date`, `effective_end_date` | timestamp[ms, UTC] | End is nullable. |
| `is_primary_meter_region` | bool, nullable | |
| `extra_fields` | string (JSON), nullable | Source fields not listed above, verbatim. |

**`price_item_savings_plan`**: one row per savings-plan entry: `price_item_key`, `snapshot_date`,
`location`, `term` (e.g. `3 Years`), `unit_price`, `retail_price`.

### GCP (Cloud Billing Catalog API, v2beta)

| Table | Grain | Columns |
|---|---|---|
| `service` | one row per in-scope service, **`global` partition only** (other locations list it with no files) | `snapshot_date`, `service_id`, `service_name`, `display_name`, `business_entity_name` |
| `sku` | one row per SKU per filed location | `snapshot_date`, `location`, `sku_id`, `sku_name`, `service_id`, `display_name`, `product_taxonomy` (list), `geo_taxonomy_type`, `geo_regions` (list), `extra_fields` |
| `sku_price` | one row per price of a SKU (on-demand and commitments), per location | `snapshot_date`, `location`, `sku_id`, `consumption_model`, `consumption_model_description` (e.g. `Default`, `Compute Flexible CUD - 1 Year`), `value_type`, `currency_code`, `unit`, `unit_description`, `unit_quantity`, `aggregation_level`, `aggregation_interval`, `extra_fields` |
| `sku_price_tier` | one row per tier of a price | `snapshot_date`, `location`, `sku_id`, `consumption_model`, `start_amount`, `currency_code`, `list_price` |

## Lookup examples

Starting points for a per-provider "price this component" mapping, checked against each
provider's own price API on 2026-10-31.

| Component | AWS | Azure (`price_item`) | GCP |
|---|---|---|---|
| VM, Linux, on-demand, per hour | `price_fact` ⨝ `product_dim` by instance type and region | `arm_sku_name`, `price_type='Consumption'`, `service_name='Virtual Machines'`, `product_name` without `Windows`, `sku_name` without `Spot`/`Low Priority` | Core SKU × vCPUs + RAM SKU × GiB: `sku` with taxonomy `VMs On Demand` + `N2` and `display_name` starting `N2 Instance Core` / `N2 Instance Ram`; `sku_price` `Default`; `sku_price_tier` `start_amount = 0` |
| Block storage, per GB-month | EBS volume type | `service_name='Storage'`, product e.g. `Premium SSD Managed Disks`, meter e.g. `P10 LRS Disk` (per disk per month) | taxonomy `Compute > Persistent Disk > SSD > Capacity`; filter `display_name` to tell zonal from regional |
| Object storage, per GB-month | S3 storage class | `product_name='General Block Blob v2'`, meter e.g. `Hot LRS Data Stored` | taxonomy `Storage > GCS > Storage > Standard > …`, `display_name` e.g. `Standard Storage US Multi-region` |
| Data transfer out | `price_fact` transfer types | In the **region**: `service_name='Bandwidth'`, `product_name='Rtn Preference: MGN'`, meter `Standard Data Transfer Out` (tiered) | taxonomy `Network > Egress > GCE > Premium` / `Standard` (tiered, GiB) |
| Managed PostgreSQL, per hour | RDS instance class | `service_name='Azure Database for PostgreSQL'`, Flexible Server product, `sku_name` e.g. `2 vCore` | taxonomy `Databases > Cloud SQL > Postgres > Compute`, per core and per GB |

The full derivations, including why each text filter is needed, are in
[`specs/004-add-azure-gcp-pricing/data-model.md`](../specs/004-add-azure-gcp-pricing/data-model.md).

## Access

The bucket blocks public access and is encrypted at rest (SSE-S3).

| Reader | Credentials |
|---|---|
| In AWS (e.g. the app's ECS task) | Attach the managed policy `cloud-pricing-data-read-<env>` to the role. |
| Outside AWS | Assume `cloud-pricing-data-reader-<env>` through IAM Roles Anywhere; see [`infra/data/README.md`](../infra/data/README.md). |

## Versioning

- A new optional field is a MINOR manifest version (`1.0` → `1.1`); consumers accept any `1.x`.
- Removing or changing the meaning of a field is a MAJOR version.
- Changing a table's columns bumps that table's `schema_version`.

Changes are listed in the [changelog](../CHANGELOG.md).
