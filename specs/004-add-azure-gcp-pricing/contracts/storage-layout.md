# Contract: Storage Layout (feature 004 changes)

This extends `specs/003-pipeline-cloud-deployment/contracts/storage-layout.md`. Its consumer rules, producer guarantees and access model apply unchanged to every provider. Only the differences are listed here.

```text
<root>/
├── aws/      (unchanged)
├── azure/
│   ├── raw/<YYYY-MM-DD>/<location>/<run_id>/
│   │   ├── prices-p0001.json.zst …               # one file per API page, as received
│   │   └── (global only) prices-empty-p0001.json.zst …, prices-global-p0001.json.zst …
│   ├── parquet/<table>/snapshot_date=<YYYY-MM-DD>/region=<location>/part-<run_id>[-<n>].parquet
│   ├── manifests/   (as AWS)
│   └── claims/      (as AWS)
└── gcp/
    ├── raw/<YYYY-MM-DD>/catalog/<run_id>/
    │   ├── services-p0001.json.zst               # v1 services list
    │   ├── skus-p0001.json.zst …                 # v2beta SKU pages
    │   └── prices-p0001.json.zst …               # v2beta price pages
    ├── parquet/<table>/snapshot_date=<YYYY-MM-DD>/region=<location>/part-<run_id>[-<n>].parquet
    ├── manifests/   (as AWS)
    └── claims/      (as AWS)
```

## Tables per provider

| Provider | Tables (schema version) | Columns |
|---|---|---|
| `aws` | `service_dim`, `product_dim`, `product_attribute`, `region_dim`, `price_fact` (1) | unchanged |
| `azure` | `price_item`, `price_item_savings_plan` (1) | data-model.md § Azure tables |
| `gcp` | `service`, `sku`, `sku_price`, `sku_price_tier` (1) | data-model.md § GCP tables |

Consumers MUST reject a table name, or a table `schema_version`, that they don't support for that provider.

## Locations

- The `region=<location>` partition and the manifest's `regions.*` lists hold **locations**: a region, a multi-region location, or `global`. Names match `^[a-z0-9]+(-[a-z0-9]+)*$`.
- Azure source names are mapped to partition names: lowercase, with spaces replaced by `-`. Each row keeps the source value in `arm_region_name`.
- **`global`** holds prices the source marks as global or gives no location for. Azure and GCP always collect `global`, and it is a requested location in every run.
- **Pricing in a location**: to price something in location *L*, read the files for *L* and for `global`. Some services (e.g., Azure support plans, GCP licensing) publish only global prices.
- **SKUs that list several locations**: a GCP SKU that lists several configured locations appears once in each of their partitions. Multi-region and dual-region SKUs list their member regions, so they are filed under those regions; `geo_taxonomy_type = 'TYPE_MULTI_REGIONAL'` tells them apart from regional SKUs. Count distinct `sku_id` values across partitions, not rows, when counting SKUs.
- **GCP `service`** is written to the `global` partition only.

## Lookup examples

These are the reference lookups the app's per-provider mapping can start from. Full filters are in data-model.md.

| Component | AWS (existing) | Azure | GCP |
|---|---|---|---|
| VM, Linux, on-demand, per hour | `price_fact` ⨝ `product_dim` by instance type and region | `price_item`: `arm_sku_name`, `price_type='Consumption'`, location | `sku` + `sku_price` (`Default`) + `sku_price_tier`: core SKU × vCPUs + RAM SKU × GiB; taxonomy `VMs On Demand`, `N2` |
| Block storage, per GB-month | EBS volume type | `price_item`: `service_name='Storage'`, product `Managed Disks` | `sku`: taxonomy `Compute > Persistent Disk > SSD > Capacity` / `Standard > Capacity` |
| Object storage, per GB-month | S3 storage class | `price_item`: `product_name` like `Blob Storage`, meter `Data Stored` | `sku`: taxonomy `Storage > GCS > Storage > Standard` (regional, or multi-regional by `geo_taxonomy_type`) |
| Data transfer out | `price_fact` transfer types | `price_item`: `service_name='Bandwidth'`, geography location (`north-america`, …) | `sku`: taxonomy `Network > Egress > GCE > Premium` / `Standard` |
| Managed relational DB, per hour | RDS instance class | `price_item`: `service_name` `Azure Database for PostgreSQL` | `sku`: taxonomy `Databases > Cloud SQL > Postgres > Compute` |

Tiered prices: Azure has one `price_item` row per tier (`tier_minimum_units`). GCP has one `sku_price_tier` row per tier (`start_amount`). GCP commitment prices (CUDs, savings plans) are further `sku_price` rows of the same SKU, told apart by `consumption_model_description`.
