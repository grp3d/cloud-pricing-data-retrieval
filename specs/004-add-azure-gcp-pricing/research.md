# Research: Azure and GCP Pricing Snapshots

**Feature**: 004-add-azure-gcp-pricing | **Date**: 2026-10-29

Azure findings come from live calls to the Retail Prices API on 2026-10-29 (no credential needed), paced at about one request every 2 seconds. GCP findings come from a full live download of both the v1 and v2beta catalogs on 2026-10-29/30 with the owner's key, paced at 1 request per second.

## R1. Azure source: Retail Prices API

**Decision**: Call `GET https://prices.azure.com/api/retail/prices?api-version=2023-01-01-preview&$filter=armRegionName eq '<location>'`, follow `NextPageLink` until it is absent, and store every page as received. Currency is `USD` (the default; `currencyCode` is configurable). Download unit = one location.

**Measured (2026-10-29)**:

| Filter | Items | Pages | Uncompressed | Time at ~0.5 req/s |
|---|---|---|---|---|
| `armRegionName eq 'eastus'` | 25,222 | 26 | 15.2 MB | 58 s |
| `armRegionName eq 'Global'` | 3,459 | 4 | 2.0 MB | 12 s |
| `armRegionName eq ''` | 3,967 | 4 | 2.4 MB | 2 s |

- Page size is fixed by the service at 1,000 items. Each page has `BillingCurrency`, `CustomerEntityId`, `CustomerEntityType`, `Items`, `NextPageLink` and `Count` (items on that page, not the total).
- `eastus` mix: `Consumption` 17,058, `DevTestConsumption` 4,574, `Reservation` 3,590. Spot and low-priority prices are `Consumption` items that are told apart only by `skuName`/`meterName` text (e.g. "Spot"); the source has no structured field for this, so none is invented.
- Item fields: `currencyCode`, `tierMinimumUnits`, `reservationTerm` (reservations only), `retailPrice`, `unitPrice`, `armRegionName`, `location`, `effectiveStartDate`, `meterId`, `meterName`, `productId`, `skuId`, `productName`, `skuName`, `serviceName`, `serviceId`, `serviceFamily`, `unitOfMeasure`, `type`, `isPrimaryMeterRegion`, `armSkuName`, and `savingsPlan` (a list of `{unitPrice, retailPrice, term}`; only with api-version `2023-01-01-preview` or later).
- Dev/Test prices are one type, `DevTestConsumption` (18% of `eastus` items). There is no separate dev or test type: on `eastus` the only types are `Consumption`, `DevTestConsumption` and `Reservation`. The filter `type ne 'DevTestConsumption'` works server-side, so the `INCLUDE_AZURE_DEVTEST_PRICES=false` setting (FR-035) removes those pages from the download instead of discarding them afterward.
- The API version is required for savings plan prices. The older GA version `2021-10-01-preview` omits them.

**Estimated full run** (7 default regions + `global` + 4 geographies): about 190 pages, 120 MB uncompressed (≈ 10 MB zstd), 6–8 minutes at the default pace.

**Alternatives considered**:

- **Downloading the whole catalog unfiltered** (≈ 5,000+ pages, all 60+ regions) and filtering locally was rejected: far more requests and data for locations nobody configured.
- **Combining several locations in one `$filter`** (`armRegionName eq 'a' or …`) was rejected. With a fixed 1,000-item page it saves only each location's final partial page (about 12 of 190 requests), and it would merge the per-location failure and re-run unit (FR-031's region equivalent).

## R2. Azure rate limits

**Decision**: Pace requests at 0.5 per second (one at a time) by default, honor `429` responses using the `Retry-After` header (falling back to `x-ms-ratelimit-retailPrices-retry-after`), and count throttled responses in the manifest.

**Rationale**: Responses carry `x-ms-ratelimit-remaining-retailPrices-requests: 9` and `x-ms-ratelimit-retailPrices-retry-after: 60`. The remaining count stayed at 9 across 34 consecutive requests at about 0.5 requests per second, so the limit is a short window rather than 10 per minute. Microsoft doesn't publish the limit. At 0.5 requests per second there was no throttling, so that is the default. The pacing settings let the owner slow down further without a code change.

## R3. Azure locations

**Decision**: Use `armRegionName` as the source location. Map it to a partition name by lowercasing it and replacing spaces with hyphens (`Middle East And Africa` → `middle-east-and-africa`). `""` and `Global` both go to `global`. Every row keeps `arm_region_name` and `location` (the display name) exactly as the source published them.

**Findings**: Besides regions (`eastus`, `westus3`, …) and edge zones (`attatlanta1`, `sgxsingapore1`), data transfer (`Bandwidth`) prices use geographies: `North America`, `Europe`, `Asia`, `India`, `Intercontinental`, `Middle East And Africa`, `Oceania`, `South America`. The spec's "Zone 1" example is outdated; the current catalog uses these geography names.

**Default Azure locations**: `eastus`, `eastus2`, `westus`, `westus2`, `westeurope`, `uksouth`, `japaneast` (regions) and `north-america`, `europe`, `asia`, `intercontinental` (geographies covering them). `global` is always collected and needs no configuration. The global location makes two calls, one for `armRegionName eq ''` and one for `armRegionName eq 'Global'`, and both are stored under `global`.

## R4. GCP source: Cloud Billing Catalog API, v2beta

**Decision**: Download the catalog with three listings, all with `pageSize=5000` and the key in the `X-goog-api-key` header:

1. `GET /v1/services`: the service list with each service's `businessEntityName`, used for the `service` table and to keep only `businessEntities/GCP` services (configurable). v2beta has no equivalent field.
2. `GET /v2beta/skus`: every SKU (`skuId`, `displayName`, `service`, `productTaxonomy`, `geoTaxonomy`).
3. `GET /v2beta/skus/-/prices?currencyCode=USD`: every SKU's prices (`skuPrices[]`, one per consumption model, each with `rate.tiers[]`, `unitInfo` and `aggregationInfo`).

The key never goes in the URL: URLs show up in exception messages, logs and HTTP error reports, so a key in the query string would leak (FR-006). The download unit is the whole **catalog**, fetched once per run.

**Measured (2026-10-29/30)**:

| | v1 (per service) | v2beta (catalog-wide) |
|---|---|---|
| Requests | 1,760: 1 services page, then 1,748 SKU lists (only Compute Engine at 7 pages, Cloud SQL at 5 and Vertex AI at 2 need more than one) | 43: 1 services page, 21 SKU pages, 21 price pages |
| Time at 1 request/s | ~30 min | ~2 min (price pages take ~5 s each to serve) |
| SKUs | 104,128 | 104,224, of which 98 are Maps or Search (out of scope) |
| Raw, gzip | 12 MB | 8.8 MB |
| Services | 1,779 listed: 1,748 GCP, 29 Maps, 2 Search | (same v1 call) |

- **Prices**: v2beta returns 162,510 prices for 104,224 SKUs: every SKU has a `Default` price, and many also have commitment prices (Compute Flexible CUD 1 and 3 years, Cloud SQL, Memorystore, Firestore and GKE CUDs, Gemini Enterprise savings plans). v1 has none of these flexible commitment prices; it has only resource-based commitments as separate SKUs.
- **Tiers**: 96% of prices have one tier; the most seen is 6. Tier `startAmount.value` and `unitInfo.unitQuantity.value` are decimal strings; `listPrice` is `units` + `nanos`.
- **Value type**: 319 prices have `valueType: unspecified` and no `rate`; they are stored with null rate fields.
- **Effective time**: v1's per-price `effectiveTime` was the catalog publication time, not a price-change date (99.8% of prices carried the download day's 07:00 UTC). v2beta has no such field; `snapshot_date` records the same information.
- **Categories**: v1's fixed `category` fields (`resourceGroup`, `usageType`) are replaced in v2beta by `productTaxonomy`, an ordered list such as `GCP > Compute > GCE > VMs On Demand > Cores: Per Core > N2`.
- **Consistency**: 2 SKUs were in v1 but not v2beta; SKUs and prices are listed separately, so a SKU can lack a price if the catalog changes between the two listings. Both cases are counted (FR-031).

**Beta-API risk**: accepted by the owner (clarification 2026-10-30). Raw responses are stored, and the GCP tables carry their own schema versions. If v2beta changes considerably, the table schema version is bumped and stored data is converted from the old schema to the new one at that time.

**Alternatives considered**: **v1** (stable, GA) was rejected: about 40 times the requests and no flexible commitment prices.

## R5. GCP rate limits

**Decision**: Pace at 1 request per second, one at a time, by default. Retry `429` and `RESOURCE_EXHAUSTED` responses, and timeouts, per the retry settings, resuming from the failed page token, and count them. A `400`/`403` response with `API_KEY_INVALID`, `PERMISSION_DENIED` or `API_KEY_SERVICE_BLOCKED` fails the run at once without retrying, and the reason never includes the key (spec US2 scenario 3).

**Measured**: about 2,600 requests across the measurements at 1 request per second produced no `429` responses. One request timed out after 120 seconds and was retried successfully. 43 requests a week is far inside any per-project quota.

## R6. GCP locations

**Decision**: Use `geoTaxonomy` as the source location:

| `geoTaxonomy.type` | Source regions | SKUs (2026-10-30) |
|---|---|---|
| `TYPE_REGIONAL` | `regionalMetadata.region.region` (one) | 85,944 |
| `TYPE_MULTI_REGIONAL` | `multiRegionalMetadata.regions[].region` (several) | 2,282 |
| `TYPE_GLOBAL` | none | 15,550 |
| absent | none | 448 |

- A SKU is filed under every **configured** location among its regions, after lowercasing (the source uses both `US` and `us`).
- `TYPE_GLOBAL` is filed under `global`. A SKU with no `geoTaxonomy` is filed under `global` and counted as unlocated.
- The type and the full region list are kept on the SKU row.

**Multi-regions**: v2beta describes most multi-region and dual-region SKUs by their member regions. A Nearline dual-region SKU lists `us-central1, us-east1`, not `nam4`, so it is filed under those regions, and its `geo_taxonomy_type = 'TYPE_MULTI_REGIONAL'` tells it apart from the regional SKU. Named multi-regions are rare: `us`/`US` on 5 SKUs and `nam4` on 1. They remain configurable but are not defaults.

**Default GCP locations**: `us-east1`, `us-east4`, `us-west1`, `us-west2`, `europe-west1`, `europe-west2`, `asia-northeast1`, plus `global` (always). The catalog lists 90 distinct region values.

## R7. Table design per provider (FR-014, FR-015, FR-030)

**Decision**: Model each provider's tables on the source's own structure, splitting only where the source has a one-to-many relationship (see data-model.md):

- **Azure** has two tables. `price_item` holds one row per item. `price_item_savings_plan` holds one row per entry in an item's `savingsPlan` list. Azure items have no identifier, so the two are joined by `price_item_key`, a documented hash of the item's identifying fields. It's a join key, not a pricing value.
- **GCP** has four tables:
  - `service`: one row per service, in `global` only.
  - `sku`: one row per SKU per filed location.
  - `sku_price`: one row per `skuPrices` entry (consumption model).
  - `sku_price_tier`: one row per tier of a price.

  They are joined on `sku_id` and `consumption_model`.

**Price types**:

- **Azure** prices are stored as `float64`, the same as AWS's `price`. The source publishes JSON numbers with at most 8 decimal places, and `float64` holds them exactly as parsed.
- **GCP** prices are stored as `decimal128(38, 9)` built exactly from `units` + `nanos`, which is the source's own exact representation. Tier start amounts and unit quantities, published as decimal strings, are stored the same way. Converting to float would be a lossy transformation (Principle I).

**Alternatives considered**:

- **AWS's table roles** (`service_dim`, `product_dim`, `product_attribute`, `region_dim`, `price_fact`) were rejected by the owner in clarification Q1: they would reshape data the source doesn't organize that way.
- **Nested Parquet columns**, with `savingsPlan` as a list of structs, were rejected because FR-014 requires a split wherever there is a one-to-many relationship, and flat tables are easier for the app to query.

## R8. Provider plug-in point in the shared pipeline (Principle VI, VII)

**Decision**: Add a small module, `src/pipeline/providers.py`, holding a plain dict from provider name to a `ProviderSpec` dataclass. Each spec carries:

- the table list and table schema versions
- the default locations
- the downloader factory
- the transform function
- the download model: `per_location` (AWS, Azure) or `catalog` (GCP, one download shared by every location)

`layout`, `manifest`, `runner`, `retention` and `verify` read the table list from the spec instead of the AWS constants. Per constitution v1.3.0 (Principle VI, code organization), provider code lives in one package per provider: the AWS modules move to `src/aws/`, and the new code goes in `src/azure/` and `src/gcp/`. Where providers vary, the shared module has a base class and each provider extends it in a file of the same name (`src/pipeline/http_pages.py` → `src/azure/http_pages.py`, `src/gcp/http_pages.py`; likewise `downloader.py`, `transformations.py` and `config.py`, whose provider-specific settings live in the provider's own settings class). The spec refers to provider classes by import path, so `providers.py` is the only shared module that names them.

**Rationale**: This is the smallest change that stops shared modules importing `src.aws_pricing_transformations`. It is not a plugin system: there is no discovery, no entry points and no dynamic loading, just three dict entries. That keeps it within Principle VII.

**Alternatives considered**: Branching on `if provider == …` throughout the runner was rejected because it spreads provider knowledge into shared modules, which Principle VI forbids.

## R9. GCP catalog download and outcome (FR-031)

**Decision**: A GCP run has two phases:

1. **Catalog phase**: download the services list, the SKU pages and the price pages (R4), paced and retried from the failed page. Raw pages are stored under `gcp/raw/<date>/catalog/<run_id>/`.
2. **Location phase**: for each configured location plus `global`, build that location's tables from the downloaded catalog.

If the catalog can't be downloaded completely, the snapshot is `failed` with the reason: a partial catalog can't produce a correct location, because any location may be missing SKUs or prices. A location can still fail on its own in the location phase (for example, a transform error), which makes the snapshot `partial` as for any provider. The manifest's optional `catalog` block carries the counters (R10).

**Alternatives considered**:

- **Per-service download and failure** (`partial` when a service fails; `run --services failed`), from clarification Q2 of 2026-10-29, was dropped with the move to v2beta. v2beta has no per-service listing, and a complete re-run takes about 43 requests and 2 minutes, so the per-service bookkeeping, the `sources` manifest block and the extra CLI option would be complexity without benefit (Principle VII).
- **SKU pages and price pages as separate units** (publish SKUs without prices as `partial`) was rejected for the same reason.

## R10. Manifest contract change → `manifest_version` 1.1 (FR-010–FR-012)

**Decision**: Bump `manifest_version` from `1.0` to `1.1`. This is a MINOR change: it only relaxes patterns and adds optional fields. AWS manifests carry `1.1` from this release on, with no other change to their content.

- **`region` pattern**: `^[a-z]{2}(-[a-z]+)+-[0-9]+$` → `^[a-z0-9]+(-[a-z0-9]+)*$`, so locations such as `eastus`, `us`, `nam4`, `north-america` and `global` are accepted. The same change applies to the raw `location` and parquet `path` patterns.
- **`tables` names**: the schema keys move from the five AWS names to a `^[a-z_]+$` pattern. The allowed names per provider are listed in `contracts/storage-layout.md`.
- **New optional `catalog`** block (GCP): API version, requests made, throttled responses and the SKU counters (received, out of scope, in unconfigured locations, unlocated, without price; prices without SKU).
- **New optional `excluded_price_types`** (Azure, FR-035).
- **New optional per-location counters** in `run.region_results[]`:
  - `throttled_responses`
  - `duplicates_removed`
  - `unlocated_rows`
  - `items_received`

**Rationale**: The current schema would reject every Azure and GCP manifest. The app already accepts any `1.x` (it rejects on MAJOR), so no app change is needed for AWS. The note for `cloud-pricing-app` is in `contracts/app-note.md`.

## R11. Request pacing (FR-032)

**Decision**: Each provider client wraps its HTTP calls in a token-bucket limiter, configured by:

- `pricing_download_rate_limit_per_second`
- `pricing_download_max_concurrent_requests`

Each run is for one provider, so one pair of settings is enough. The defaults differ by provider (Azure 0.5 and 1; GCP 1.0 and 1; AWS keeps its current behavior). Throttled responses are counted per location (Azure) or for the catalog (GCP).

**Minimizing requests (FR-034)**:

- **Page size**: Azure's is fixed at 1,000; GCP uses the maximum, 5,000.
- **Resume, don't restart**: a failed page is retried from its own `NextPageLink` (Azure) or `pageToken` (GCP). The location or catalog restarts from page 1 only if the source rejects that link or token (for example, an expired token).
- **No re-download**: transform-only runs read stored raw data.
- **GCP catalog-wide listings**: v2beta needs 43 requests a run instead of v1's 1,760 (R4).
- **Counted**: `requests_made` (retries included) is recorded per Azure location and for the GCP catalog, so a jump in the weekly count is visible.

**Alternatives considered**: Per-provider-prefixed setting names (`AZURE_RATE_LIMIT…`) were rejected. The setting has one purpose, and the provider is already a dimension of the run. Its defaults come from the `ProviderSpec`.

## R12. GCP API key in the cloud: SSM Parameter Store (FR-007)

**Decision**:

- **Parameter**: `/cloud-pricing/<env>/gcp-pricing-api-key`, type `SecureString`, encrypted with the AWS-managed key `aws/ssm`. Standard tier, so free.
- **Created by the operator, not OpenTofu**: the operator creates and rotates it with a documented `aws ssm put-parameter` command (quickstart C1). OpenTofu never manages the parameter resource; it only refers to the parameter's ARN by name.
- **Injected at task start**: the GCP task definition lists the parameter in its container `secrets`. ECS reads it at task start and sets `GCP_PRICING_API_KEY` in the container. The pipeline code reads the same environment variable locally and in the cloud, and contains no SSM code.
- **Access**: only a GCP-specific task execution role, `cloud-pricing-task-execution-gcp-<env>`, may call `ssm:GetParameters` on that one parameter. No `kms:Decrypt` grant is needed, because the key is the AWS-managed one.

**Rationale**:

- **OpenTofu reads values back**: an `aws_ssm_parameter` resource reads its value back, decrypted, on every refresh and stores it in state, even with `ignore_changes`. That would defeat FR-007.
- **No write-only alternative yet**: write-only attributes (`value_wo`) need OpenTofu ≥ 1.11. The project pins 1.10.6, and upgrading isn't justified for this.
- **Rotation**: a new value is picked up at the next task start with no redeploy (US4 scenario 5).
- **Missing parameter**: if the parameter doesn't exist, ECS fails the task with `TaskFailedToStart`. The existing `TASK CRASHED` rule already matches that `stopCode`, so the owner is alerted.

**Alternatives considered**:

- **Secrets Manager**: costs $0.40 a month for the same benefit.
- **Reading the parameter from inside the pipeline**: rejected because it would put AWS-specific code in the core (Principle III) and break local parity.

## R13. Infrastructure: one task definition and schedule per provider

**Decision**: Replace `pricing_regions`, `schedule_expression` and `schedule_enabled` with a `provider_runs` map variable. Each entry has `locations`, `schedule_expression`, `schedule_enabled`, `task_cpu`, `task_memory` and `secrets`. From it, the infrastructure creates per provider (`for_each`):

- an `aws_ecs_task_definition`, carrying `PIPELINE_PROVIDER`, `PRICING_REGIONS` and the pacing settings
- an `aws_scheduler_schedule`, named `cloud-pricing-pipeline-<provider>-<env>`

**Migration**: `moved` blocks keep the existing AWS schedule and task definition, which become `["aws"]`, so the prod AWS schedule is not destroyed and re-created.

**Related changes**:

- **Data stack**: `pricing_providers` becomes `["aws","azure","gcp"]`, which adds the raw lifecycle rules for `azure/raw/` and `gcp/raw/` (30 days), and reader and writer access.
- **Watchdog**: it checks only providers whose schedule is enabled, so a provider that is still disabled (FR-033) doesn't raise false missed-run alerts.

**Default schedule**: Monday 13:00 UTC for all three. The runs are independent and their claims are per provider, so there's no reason to stagger them.

**Task sizing**: Azure and GCP use 1 vCPU and 2 GiB. An Azure location is a few MB of JSON; the GCP catalog is about 9 MB compressed for 104k SKUs and 162k prices, held in memory once and filed into every location. Both are far below AWS's 3.9 GB transform peak. This is confirmed by Q1 and Q2, which record peak memory.

**CI roles**: the apply role needs no new SSM permissions, because OpenTofu only writes IAM policy documents that reference the parameter ARN. The new execution role is named `cloud-pricing-task-execution-gcp-<env>`. That name falls under the apply role's existing grants on `role/cloud-pricing-*-<env>` (`infra/bootstrap/github_oidc.tf:170`).

The **run role** is different: it may pass only `cloud-pricing-task-<env>` and `cloud-pricing-task-execution-<env>` (`github_oidc.tf:284-287`). So `run-pipeline` couldn't start a GCP task until the bootstrap stack adds the new role to that `iam:PassRole` list. The pipeline stack's scheduler role needs the same addition. Both changes are checked with the IAM policy simulator, as for feature 003.

## R14. Cost estimate (SC-007)

| Item | Monthly |
|---|---|
| Fargate ARM, 1 vCPU / 2 GiB, Azure ~8 min + GCP ~4 min, 4.3 runs | ≈ $0.04 |
| S3 storage: raw ≤ 30 days (≈ 20 MB/week zstd), tables (≈ 30 MB/week Parquet), 12 months + monthly | ≈ $0.05 growing to ≈ $0.10 |
| S3 requests (PUT/LIST/GET) | < $0.02 |
| CloudWatch Logs (≈ 5 MB/month) | < $0.01 |
| SSM Parameter Store standard tier, `aws/ssm` key | $0 |
| Data transfer in from Azure/GCP | $0 |
| **Total added** | **≈ $0.15–0.20** (SC-007 limit: $3) |

## R15. Testing approach

- **Offline fixtures**: Azure and GCP clients are tested against recorded, trimmed page fixtures in `tests/fixtures/pricing/azure/` and `tests/fixtures/pricing/gcp/`. Fixtures are recorded from the live APIs, with any key header stripped, and nothing in the test suite touches the network (Principle IV).
- **Fake downloaders**: fake downloaders, following the existing `tests/helpers/fake_pricing.py`, drive runner tests. They cover throttling, broken pagination with resume, a GCP catalog page failing, SKU and price lists that disagree, a rejected key, and `global` failing.
- **Test-first** (required by Principle IV): both transforms, the location filing rules, `derive_status` for a failed catalog, the manifest 1.1 fields, page-level resume, and `latest.json` and retention for new providers.
- **Contract tests**: written manifests for all three providers are validated against the 1.1 schema.
- **No-leak test**: runs a GCP fixture run and searches every written object, captured logs and alert payloads for the key value (SC-006).
