# Note for `cloud-pricing-app`: manifest 1.1, Azure and GCP

**From**: cloud-pricing-data-retrieval, feature 004 | **Applies from**: the release that ships feature 004

## What changes for the app's existing AWS reading

Nothing in behavior:

- AWS manifests now say `"manifest_version": "1.1"`. The app already accepts any `1.x`, because it rejects only on MAJOR.
- AWS tables, schema versions, paths and `aws/manifests/latest.json` are unchanged.
- If the app validates manifests against the bundled 1.0 JSON schema, switch to the 1.1 schema in this folder (`manifest.schema.json`). AWS manifests are valid under both.

## What's new

- **Two providers**: `azure/` and `gcp/`, each with its own `manifests/latest.json`, using the same rules as `aws/`.
- **Provider-specific tables**:
  - Azure: `price_item`, `price_item_savings_plan`
  - GCP: `service`, `sku`, `sku_price`, `sku_price_tier`

  They are deliberately not shaped like AWS's tables. A "price this component" lookup needs a per-provider mapping; see `storage-layout.md` § Lookup examples.
- **Locations**: `regions.*` and `region=` partitions now hold locations: regions, multi-region locations (`north-america` and other Azure geographies) and `global`. GCP multi-region SKUs are filed under their member regions and marked `TYPE_MULTI_REGIONAL`. When pricing in location *L*, also read `global`.
- **Optional manifest fields**:
  - `catalog`: GCP's download counters (requests, SKUs without price, and so on).
  - `excluded_price_types`: Azure price types deliberately not collected (Dev/Test, when excluded).
  - per-location counters in `run.region_results[]`

  Ignore them if they aren't needed.
- **GCP prices** come from Google's v2beta catalog API and include commitment prices (CUDs, savings plans) per SKU. They are `decimal128(38, 9)`. Azure and AWS prices are `float64`.
- **Nulls**: a price the source omits, or published in a currency other than the configured one (USD), and a price, tier bound or quantity that isn't a valid number, are stored as null and counted in `unparseable_prices` for that location. Prices are never converted. Treat null as "not published", never as zero.
- **GCP `service`**: rows exist only in the `global` partition; other locations list the table with no files.

## Suggested app work (separate feature)

1. Read `latest.json` for each provider independently. One provider being `partial`, `failed` or stale must not hide the others.
2. Give architecture building one common set of methods (for example, "price this VM size in this location"), each backed by provider-specific retrieval and transformation code that reads that provider's own tables. Normalizing across providers happens there, in the app, not in these tables.
3. Reject an unknown table name or `schema_version` per provider.
