---

description: "Task list for feature 004: Azure and GCP Pricing Snapshots"
---

# Tasks: Azure and GCP Pricing Snapshots

**Input**: Design documents from `specs/004-add-azure-gcp-pricing/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: REQUIRED. Constitution v1.3.0 Principle IV makes tests mandatory for every behavior change, and **test-first** for:
- both transforms (Azure, GCP) and the location filing rules
- manifest 1.1 building and status derivation
- request pacing and page-level resume

For those, the test task comes first and MUST fail before the implementation task starts. Tests for wiring, the CLI, OpenTofu and workflows MAY be written afterwards. Every test runs offline (no network, no key).

**AWS safety**: the existing suite (273 tests at the start of this feature) MUST pass after every task. AWS table files, schema versions and `aws/manifests/latest.json` behavior MUST NOT change; the only AWS manifest difference is `manifest_version: "1.1"` (SC-008).

**Code organization** (constitution v1.3.0 Principle VI, plan.md § Structure Decision):
- Shared code in `src/pipeline/` names no provider. Only `src/pipeline/providers.py` refers to provider packages, by import path.
- Provider code lives in `src/aws/`, `src/azure/`, `src/gcp/`. A file that extends a shared module has that module's name (`src/pipeline/http_pages.py` → `src/<provider>/http_pages.py`). No empty subclass files.
- Provider packages import `src/pipeline/`, never each other. `tests/unit/test_code_organization.py` enforces this from the Phase 2 checkpoint on.

**Organization**: tasks are grouped by user story, US1–US5 from spec.md. US1 (Azure) and US2 (GCP) are both P1 and ship together (FR-033); US1 comes first because it uses the existing per-location download model, and US2 adds the catalog model.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on incomplete tasks)
- **[Story]**: the user story this task belongs to (US1–US5)
- Paths are relative to the repository root. "R4" points to `research.md`, "FR-0xx" to `spec.md`, `data-model.md §…` and `contracts/…` to files under `specs/004-add-azure-gcp-pricing/`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: contract copies, recorded fixtures and test fakes.

- [X] T001 Replace `src/pipeline/schemas/manifest.schema.json` with a byte-for-byte copy of `specs/004-add-azure-gcp-pricing/contracts/manifest.schema.json` (1.1); `latest.schema.json` is unchanged. In `tests/unit/test_contract_schemas.py`, point `SPEC_CONTRACTS` at `specs/004-add-azure-gcp-pricing/contracts`. Existing AWS manifests must still validate (1.1 only relaxes patterns and adds optional fields).
- [X] T002 [P] Record trimmed Azure fixtures under `tests/fixtures/pricing/azure/` from the live Retail Prices API (api-version `2023-01-01-preview`, no credential). Keep the real page shape (`BillingCurrency`, `CustomerEntityId`, `CustomerEntityType`, `Items`, `NextPageLink`, `Count`) and rewrite `NextPageLink` to fixture names. Include:
  - `eastus-p0001.json`, `eastus-p0002.json`: two pages (last page has `NextPageLink: null`) with ~10 items each, covering `Consumption` (Linux and Windows `Standard_D4s_v5`, a Spot and a Low Priority `skuName`), `DevTestConsumption`, `Reservation` (with `reservationTerm`), one item with a `savingsPlan` list of 2 entries, one tiered item (two `tierMinimumUnits` values), one item with an `effectiveEndDate`, and one byte-identical duplicate across the two pages
  - `global-empty-p0001.json` (`armRegionName eq ''`) and `global-global-p0001.json` (`armRegionName eq 'Global'`)
  - `north-america-p0001.json`: `Bandwidth` items with `armRegionName: "North America"`
  - one item with an unknown extra field (for `extra_fields`) and one with a non-numeric `unitPrice` (edited in by hand, marked in a `README.md` next to the fixtures)
- [X] T003 [P] Record trimmed GCP fixtures under `tests/fixtures/pricing/gcp/` from the live catalog (key from `.env`, sent only in `X-goog-api-key`; the responses contain no key). Keep the real response shapes (research R4) and split each listing into 2 pages with `nextPageToken` rewritten to fixture tokens:
  - `services-p0001.json`: `/v1/services` with 3 services: Compute Engine (`businessEntities/GCP`), Cloud Storage (`businessEntities/GCP`), one `businessEntities/Maps` service
  - `skus-p0001.json`, `skus-p0002.json`: `/v2beta/skus` with the `N2 Instance Core running in Americas` and `N2 Instance Ram running in Americas` SKUs (`TYPE_MULTI_REGIONAL`: `us-central1`, `us-east1`, `us-west1`), one `TYPE_REGIONAL` SKU in `us-east1`, one in `europe-west1`, one `TYPE_REGIONAL` SKU with region `US` (upper case), one `TYPE_GLOBAL` SKU, one SKU with no `geoTaxonomy`, one with no `productTaxonomy.taxonomyCategories`, one Maps SKU, and one SKU with no price
  - `prices-p0001.json`, `prices-p0002.json`: `/v2beta/skus/-/prices?currencyCode=USD` for those SKUs: the N2 core with `Default`, `Compute Flexible CUD - 1 Year` and `- 3 Year`; one tiered price (3 tiers); one `valueType: unspecified` price with no `rate`; one price whose SKU is not in the SKU list; one `unitQuantity` of `1000000`
  - one hand-edited malformed `listPrice` (non-integer `nanos`), marked in a `README.md` next to the fixtures
- [X] T004 [P] Extend `tests/helpers/fake_pricing.py` with `FakeHttpSource`: serves fixture pages by URL (matching path and query, ignoring `pageToken`/`$skip` order), records every request (URL and headers), and can be configured per URL to `fail_times(n)` with a timeout, a 5xx, or a 429 with `Retry-After`, or to fail permanently with a 400/403 body (e.g. `API_KEY_INVALID`). Clients under test take it through an injectable `fetch(url, headers, timeout) -> (status, headers, body)` callable.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: move AWS into its own package, make the shared pipeline provider-neutral (R8, constitution VI), and add pacing and the paging base class with page-level retry (R11, FR-032, FR-034). After this phase the AWS suite passes unchanged and the runner can host a second provider.

**⚠️ CRITICAL**: no user story work can begin until this phase is complete.

- [X] T005 Move the AWS code into `src/aws/` with `git mv`, behavior unchanged: `src/aws_pricing_api.py` → `src/aws/api.py`, `src/aws_pricing_transformations.py` → `src/aws/transformations.py`, `src/aws_regions.py` → `src/aws/regions.py`, `src/aws_pricing_cli.py` → `src/aws/cli.py`; add `src/aws/__init__.py`; move `AwsPricingDownloader` from `src/pipeline/downloader.py` to `src/aws/downloader.py`. Update imports in `src/pipeline/runner.py`, `src/pipeline/config.py`, `src/pipeline/manifest.py`, `src/aws/cli.py`, `src/dagster_app/resources.py`, and in the 7 test files that import AWS modules (`tests/unit/test_aws_pricing_api_async.py`, `test_aws_pricing_cli.py`, `test_aws_pricing_transformations.py`, `test_aws_regions.py`, `test_pricing_resources.py`, `test_pricing_schedule.py`, `test_settings.py`); tests change only in import paths and `mock.patch` target strings. Update `README.md` to `python -m src.aws.cli`. Leave no shims at the old paths. The full suite passes with the same count (273).
- [X] T006 [P] Write `tests/unit/test_code_organization.py` (test-first; MUST fail until T019 removes the AWS imports from `src/pipeline/`). Using `ast` over `src/`:
  - no module under `src/pipeline/` except `providers.py` imports `src.aws`, `src.azure` or `src.gcp` (including `from src import aws`)
  - no identifier or string constant under `src/pipeline/` except in `providers.py` has a token equal to `aws`, `azure` or `gcp`, case-insensitive (split names on `_`, `-`, `/`, `.` and camelCase boundaries, so `RawSnapshot` passes and `AwsPricingDownloader` or `"pricing_aws"` fails); docstrings and comments are ignored
  - no module in `src/<provider>/` imports another provider's package
  - no top-level `src/aws_*.py`, `src/azure_*.py` or `src/gcp_*.py` exists
  - each check reports the file and line of every violation
- [X] T007 [P] Write `tests/unit/test_rate_limit.py` (test-first, MUST fail): with an injected clock and sleep, `RateLimiter(rate_per_second=0.5, max_concurrent=1)` spaces 5 acquisitions ≥ 2 s apart; `max_concurrent=2` never has more than 2 holders; `penalize(seconds)` (a `Retry-After`) delays the next acquisition by at least that long.
- [X] T008 Implement `src/pipeline/rate_limit.py`: `RateLimiter` (token bucket + semaphore, injectable `now`/`sleep`) used as a context manager around each request; `penalize(seconds)`. No third-party dependency.
- [X] T009 [P] Write `tests/unit/test_http_pages.py` (test-first, MUST fail) for the base class `PagedHttpSource`, using a minimal subclass defined in the test file and `FakeHttpSource`: `pages()` yields every page in order, following `next_url(body, url)` until it returns `None`; a page that times out or returns 5xx/429 is retried **from that page's own URL**, never from page 1 (FR-034); a 429 waits `retry_after_seconds(headers)` (base reads `Retry-After`; a subclass override is honored) via `RateLimiter.penalize`; retries follow `RetryPolicy` (`pricing_download_retry_*`); a response for which `is_permanent_error(status, body)` is true raises `PermanentSourceError` with no retry (base: any 4xx except 429); a response for which `resume_rejected(status, body)` is true (base: never) restarts the listing from `first_url()` once, counted in `stats.restarts`, and a second rejection in the same listing raises `SourceError` (FR-034: "unless the source rejects the resume point"); headers come from `request_headers()`; `stats` counts `requests_made` (retries included) and `throttled_responses`; every error message passes through `redact(text)`, and messages and `repr` never contain request header values (assert with a header value of `SECRET-123`).
- [X] T010 Implement `src/pipeline/http_pages.py`: base class `PagedHttpSource(fetch=None, limiter=None, retry_policy=…)` with the page loop, retry from the failed page and counting in the base, and small overridable methods `first_url()`, `next_url(body, url)`, `request_headers()`, `is_permanent_error(status, body)`, `resume_rejected(status, body)`, `retry_after_seconds(headers)`, `redact(text)`; plus `PageFetchStats`, `PermanentSourceError`, `SourceError`. Uses `urllib.request` by default and the injectable `fetch` callable. Exceptions carry the URL **without** headers, redacted, clipped to 500 characters. No provider names (T006).
- [X] T011 [P] Write `tests/unit/test_providers.py`: `PROVIDERS` has exactly `aws`, `azure`, `gcp`; `DEFAULT_PROVIDER == "aws"`; each `ProviderSpec` matches data-model.md § ProviderSpec — tables and schema versions (`aws`: `service_dim, product_dim, product_attribute, region_dim, price_fact` → 1; `azure`: `price_item, price_item_savings_plan` → 1; `gcp`: `service, sku, sku_price, sku_price_tier` → 1), `download_model` (`per_location`, `per_location`, `catalog`), `always_collected` (`[]`, `["global"]`, `["global"]`), default pacing (AWS none; Azure 0.5/s, 1; GCP 1.0/s, 1), `settings` (AWS none, so the base `ProviderSettings`; Azure and GCP their own class paths), `legacy_parquet_dir` (only AWS: `pricing_aws/parquet`); the AWS spec's tables and versions equal `src.aws.transformations.AwsTransform.TABLE_SCHEMA_VERSIONS`, its default locations equal `src.aws.regions`' defaults, and its `downloader` and `transform` resolve to subclasses of `src.pipeline.downloader.Downloader` and `src.pipeline.transformations.LocationTransform`; importing `src.pipeline.providers` imports no provider package (check `sys.modules` in a subprocess); `get_provider("x")` raises.
- [X] T012 Implement `src/pipeline/providers.py`: frozen `ProviderSpec` dataclass, `DEFAULT_PROVIDER` and the `PROVIDERS` dict (R8). Classes and AWS default locations are import path strings resolved lazily: `downloader` (`"src.aws.downloader:AwsPricingDownloader"`, `"src.azure.downloader:AzurePricesDownloader"`, `"src.gcp.downloader:GcpCatalogDownloader"`), `transform` (`"src.aws.transformations:AwsTransform"`, `"src.azure.transformations:AzureTransform"`, `"src.gcp.transformations:GcpTransform"`) and `settings` (`None` for AWS, `"src.azure.config:AzureSettings"`, `"src.gcp.config:GcpSettings"`), so importing `providers` doesn't import provider packages. This is the only `src/pipeline/` module allowed to name providers.
- [X] T013 [P] Extend `tests/unit/test_layout.py`: `validate_region` accepts `eastus`, `us-east-1`, `us-east1`, `north-america`, `us`, `eu`, `global` and rejects `East US`, `-x`, `a_b`, `""` (pattern `^[a-z0-9]+(-[a-z0-9]+)*$`); `parquet_file_key("azure", "price_item", …)` and `("gcp", "sku_price_tier", …)` work; a table not in that provider's list raises; `catalog_raw_prefix("gcp", date, run_id)` is `gcp/raw/<date>/catalog/<run_id>/`.
- [X] T014 Update `src/pipeline/layout.py`: `_REGION_RE` → `[a-z0-9]+(-[a-z0-9]+)*`; replace the module-level `TABLES` check with `tables_for(provider)` from `providers.py` (keep `TABLES` for existing callers until T018 removes it; `src/aws/transformations.py` defines its own AWS table tuple); `_table(provider, value)`; add `catalog_raw_prefix()` and `catalog_raw_file_key()`.
- [X] T015 [P] Write `tests/unit/test_manifest_v1_1.py` (test-first, MUST fail): built manifests carry `manifest_version: "1.1"` for every provider; `tables` keys and `schema_version`s come from the provider's spec; `run.region_results[]` serialize `items_received`, `duplicates_removed`, `unlocated_rows`, `throttled_responses`, `requests_made` only when set (AWS output unchanged apart from the version); optional top-level `catalog` (GCP: `api_version`, `requests_made`, `throttled_responses`, `skus_received`, `skus_out_of_scope`, `skus_unconfigured_locations`, `skus_unlocated`, `skus_without_price`, `prices_without_sku`) and `excluded_price_types` (Azure) round-trip through `Manifest.from_json`; `derive_status` is unchanged (`global` counts as a requested location); manifests for all three providers validate against `src/pipeline/schemas/manifest.schema.json` (use `tests/helpers/contracts.py`).
- [X] T016 Update `src/pipeline/manifest.py`: `MANIFEST_VERSION = "1.1"`; take tables and schema versions from `providers.get_provider(provider)` instead of `src.aws.transformations.TABLE_SCHEMA_VERSIONS`; add the optional counters to `RegionResult`/`RegionOutcome`, and optional `catalog: Optional[CatalogInfo]` and `excluded_price_types: Optional[List[str]]` to `Manifest` and `build_revision()`; omit unset optional fields when serializing.
- [X] T017 [P] Extend `tests/unit/test_settings.py` (shared settings only; provider-specific settings are tested in T023 and T030): `PIPELINE_PROVIDER` must name a spec in `providers.PROVIDERS` (else `SettingsError`); `PRICING_REGIONS` defaults to the provider's default locations (data-model.md § Settings; Azure: `eastus, eastus2, westus, westus2, westeurope, uksouth, japaneast, north-america, europe, asia, intercontinental`; GCP: `us-east1, us-east4, us-west1, us-west2, europe-west1, europe-west2, asia-northeast1`) and rejects `global` when listed explicitly; `PRICING_DOWNLOAD_RATE_LIMIT_PER_SECOND` (> 0; provider default), `PRICING_DOWNLOAD_MAX_CONCURRENT_REQUESTS` (≥ 1; default 1), `PRICING_CURRENCY` (`USD`); `settings.provider_settings` is built by the spec's `settings` class (a stub `ProviderSettings` subclass registered for the test), and a `SettingsError` it raises propagates (exit 2 before any claim); `redacted()` shows every field the provider settings class lists in `SECRET_FIELDS` as `***` when set and `null` when not, and `repr(settings)` contains no secret value (test with a stub secret `SECRET-123`). Existing AWS defaults are unchanged.
- [X] T018 Update `src/pipeline/config.py` to pass T017: add the base `ProviderSettings` (frozen dataclass; `from_env(get)` classmethod; `SECRET_FIELDS: tuple = ()`; `redacted()`; secret fields declared with `field(repr=False)`), used directly when the spec has no `settings` class (AWS). `PipelineSettings.from_env` loads `provider_settings` from the spec's class and collects its secret values for log and error redaction. The default provider is `providers.DEFAULT_PROVIDER`, and default locations and location validation come from the provider's spec (no `src.aws.regions` import). No provider-specific variable is read in this module (T006). Then switch every remaining `layout.TABLES` caller to `layout.tables_for(provider)` and delete `layout.TABLES` and `manifest.TABLES`: `src/pipeline/retention.py` (`_parquet_dates`, `_date_files`), `src/pipeline/verify.py`, `src/pipeline/raw_access.py`, `src/pipeline/backfill.py`. In `backfill.py`, `LEGACY_DIR` becomes the spec's `legacy_parquet_dir`; in `__main__.py`, the `upload-history` help text says "legacy layout" instead of naming `pricing_aws/`.
- [X] T019 Generalize `src/pipeline/runner.py` for provider specs (R8, R9):
  - the default downloader and the transform come from the provider's spec; `runner.py` imports nothing from `src/aws/`
  - the requested locations are the configured ones plus the spec's `always_collected` (`global`)
  - `download_model == "catalog"`: download once into the run's work dir before the per-location loop, store raw under `catalog_raw_prefix`, then build each location from the shared download; if the catalog download fails, every location fails with the catalog's reason (the snapshot becomes `failed`, FR-031)
  - the table loop uses the provider's table list; per-location counters and the catalog/excluded-type info flow into `build_revision()`
  - new `src/pipeline/transformations.py`: the common `TransformResult` (`files`, `errors`, `unparseable_prices`, counters) and the abstract base class `LocationTransform(settings, configured_locations)` with `TABLE_SCHEMA_VERSIONS`, `load(raw_files)` (base: returns the files unchanged; called once per location, or once per run for the catalog model), the abstract `transform_location(loaded, location, staging_dir, run_id, snapshot_date) -> TransformResult`, and `run_counters(loaded)` (base: `{}`)
  - in `src/aws/transformations.py`, add `AwsTransform(LocationTransform)`, whose `transform_location` calls the existing `transform_pricing_to_parquet` unchanged, and move `TABLE_SCHEMA_VERSIONS` onto it (AWS output byte-identical)
  - `src/pipeline/downloader.py`: `Downloader` (per location: `download_region`, `failure_reason`) becomes an abstract base class, and a new abstract base class `CatalogDownloader` (`download_catalog(dest_dir) -> CatalogDownload`: file list, counters, error) serves the catalog model; `AwsPricingDownloader` subclasses `Downloader`, and test fakes in `tests/helpers/fake_pricing.py` subclass the matching base
- [X] T020 Update `src/pipeline/__main__.py`: `upload-history` exits 2 for a provider whose spec has no `legacy_parquet_dir` (today, every provider but AWS); `settings` prints the redacted provider defaults; error output passes through the same redaction as logs. Add a `tests/unit/test_cli_provider.py` covering these.
- [X] T021 Run the full suite (`venv/bin/pytest`): all existing tests pass with no test edits other than T001, T005's import paths, T019's change to `tests/helpers/fake_pricing.py` (fakes subclass the new base classes) and the new tests above, and `tests/unit/test_code_organization.py` now passes. Record the count in the checkpoint note below.

**Checkpoint**: foundation ready; AWS behavior unchanged, now in `src/aws/`; `src/pipeline/` is provider-neutral. User story work can begin.

> Recorded 2026-10-30: 386 passed, 2 skipped (the `src/azure/` and `src/gcp/` import checks, until those packages exist). The suite had 297 tests at the start of this feature, not 273. Test edits beyond the planned ones: the two `manifest_version` assertions now expect `"1.1"` (SC-008), and tests that used the removed shared `layout.TABLES` / `manifest.TABLES` now use `layout.tables_for("aws")`.

---

## Phase 3: User Story 1 - Weekly Azure pricing snapshot is published (Priority: P1) 🎯 MVP

**Goal**: `PIPELINE_PROVIDER=azure` publishes a manifest-described snapshot of `price_item` and `price_item_savings_plan` for the configured locations plus `global` (FR-002, FR-013, FR-014, FR-035).

**Independent Test**: run `azure` for `eastus` against a local root (offline with fakes; live in quickstart Q1); verify `azure/manifests/<date>/manifest.json`, `latest.json`, and `verify` exit 0.

### Tests for User Story 1 (test-first) ⚠️

- [X] T022 [P] [US1] Write `tests/unit/test_azure_transformations.py` (MUST fail) against the T002 fixtures, asserting data-model.md § Azure tables exactly:
  - `price_item` columns and types as listed, including `arm_region_name` `""` kept as `""`, `reservation_term` "null when absent", `unit_price`/`retail_price` "null + counted if unparseable", `effective_end_date` nullable, `extra_fields` holding "any field not listed above, kept verbatim"
  - `price_item_key` is "sha256 (hex, first 32 chars) of the canonical JSON of the item without `savingsPlan`" (sorted keys, no whitespace) and is stable across runs
  - `price_item_savings_plan` has one row per `savingsPlan` entry, joined by `price_item_key`
  - byte-identical duplicates are kept once and counted in `duplicates_removed`; items that differ only in price are all kept
  - each location's tiers are separate rows (`tier_minimum_units`)
  - location mapping: "lowercase, then replace runs of spaces with `-`"; `""` and `Global` both go to `global`, with only `""` counted in `unlocated_rows`; two different source values mapping to one partition name fail that location with a reason
- [X] T023 [P] [US1] Write `tests/unit/test_azure_config.py`, `tests/unit/test_azure_http_pages.py` and `tests/unit/test_azure_api.py` (MUST fail). `AzureSettings` per `contracts/configuration.md`: `AZURE_PRICES_API_URL`, `AZURE_PRICES_API_VERSION` (`2023-01-01-preview`), `INCLUDE_AZURE_DEVTEST_PRICES` (`true`; `false`/`true` only, else `SettingsError`); no secret fields. Then, with `FakeHttpSource`. `AzurePagedSource`: `NextPageLink` is followed until null; a 429 uses `Retry-After`, then `x-ms-ratelimit-retailPrices-retry-after`; no credential header is sent; a failed page resumes from its own `NextPageLink`. `AzurePricesClient`: the first URL is `<AZURE_PRICES_API_URL>?api-version=2023-01-01-preview&$filter=armRegionName eq '<source name>'` (geographies use the source spelling, e.g. `'North America'`); with `INCLUDE_AZURE_DEVTEST_PRICES=false` the filter adds `and type ne 'DevTestConsumption'`; `global` makes two listings (`''` and `'Global'`) stored as `prices-empty-pNNNN.json` and `prices-global-pNNNN.json`; other pages as `prices-pNNNN.json`; `requests_made`, `throttled_responses` and `items_received` are reported.
- [X] T024 [P] [US1] Write `tests/integration/test_run_snapshot_azure.py` (including SC-004 accounting: per location, `items_received` equals the `price_item` row count plus `duplicates_removed`) with a fake Azure downloader over the fixtures, against `store` (local and moto S3): a full run is `succeeded` with requested regions `eastus`, `north-america`, `global`, files under `azure/parquet/<table>/snapshot_date=<D>/region=<location>/`, raw under `azure/raw/<D>/<location>/<run_id>/`, `latest.json` moved, `verify` clean, manifest valid against the 1.1 schema; one failing location → `partial`, `latest.json` unchanged, alert kinds include the partial alert naming `azure`; a re-run of that location publishes revision 2 under new paths; with `INCLUDE_AZURE_DEVTEST_PRICES=false` the manifest has `excluded_price_types: ["DevTestConsumption"]`; an `azure` claim doesn't block an `aws` run for the same date (spec US1 scenario 4).

### Implementation for User Story 1

- [X] T025 [US1] Implement `src/azure/__init__.py`, `src/azure/config.py` (`AzureSettings(ProviderSettings)`), `src/azure/http_pages.py` (`AzurePagedSource(PagedHttpSource)`, overriding only `next_url` and `retry_after_seconds`) and `src/azure/api.py` (`AzurePricesClient(settings, fetch=None, limiter=None)` with `download_location(location, dest_dir) -> LocationDownload`) (R1–R3, FR-034, FR-035). Map partition names back to source filter values (`north-america` → `North America`) using the known geography list in research R3; an unknown location fails that location with "unknown to the source" after the first empty listing (spec edge case).
- [X] T026 [US1] Implement `src/azure/transformations.py`: `AzureTransform(LocationTransform)`, whose `transform_location(json_files, location, staging_dir, run_id, snapshot_date)` writes `part-<run_id>.parquet` per table with the exact pyarrow schemas of data-model.md § Azure tables and returns `TransformResult` with `items_received`, `duplicates_removed`, `unlocated_rows`, `unparseable_prices`. `AzureTransform.TABLE_SCHEMA_VERSIONS = {"price_item": 1, "price_item_savings_plan": 1}`; extend `tests/unit/test_providers.py` to assert it equals the `azure` spec.
- [X] T027 [US1] Implement `src/azure/downloader.py`: `AzurePricesDownloader(Downloader)` (wraps the client, returns `RegionDownload` plus counters; `failure_reason()` covers page failures). The `azure` spec in `src/pipeline/providers.py` already points at it, at `AzureTransform` and at `AzureSettings` (T012); set `excluded_price_types` on the run when Dev/Test is excluded.
- [X] T028 [US1] Make T022–T024 pass; run the full suite.

**Checkpoint**: Azure snapshots publish locally; AWS unchanged.

---

## Phase 4: User Story 2 - Weekly GCP pricing snapshot is published (Priority: P1)

**Goal**: `PIPELINE_PROVIDER=gcp` downloads the catalog once (43 requests, R4), files SKUs into the configured locations plus `global`, and publishes `service`, `sku`, `sku_price`, `sku_price_tier`, with the key never leaving the request header (FR-003, FR-006, FR-015, FR-031).

**Independent Test**: run `gcp` for `us-east1` against a local root with a fake source (offline) and with the `.env` key (quickstart Q2); verify as US1, and search every output for the key (SC-006).

### Tests for User Story 2 (test-first) ⚠️

- [X] T029 [P] [US2] Write `tests/unit/test_gcp_transformations.py` (MUST fail) against the T003 fixtures, asserting data-model.md § GCP tables exactly:
  - filing rules (data-model.md § Location): regional/multi-regional SKUs go to "each listed region (lowercased) that is configured", one `sku` row per location; `TYPE_GLOBAL` → `global`; "no `geoTaxonomy`" → `global`, counted in `unlocated_rows` and `catalog.skus_unlocated`; "regions listed, none configured" → not stored, counted in `catalog.skus_unconfigured_locations`; `US` files under `us` when `us` is configured and keeps `US` in `geo_regions`
  - SKUs of services outside `GCP_BILLING_BUSINESS_ENTITIES` are not stored and are counted in `skus_out_of_scope`
  - `service` rows only in the `global` partition
  - `sku`: `service_id` "without the `services/` prefix"; `product_taxonomy` "in source order", "null when the source gives none"; `geo_taxonomy_type` "null when absent"
  - `sku_price`: one row per `skuPrices` entry, `consumption_model` "without the `consumptionModels/` prefix"; "Rate fields are null when `value_type` isn't `rate`"; `unit_quantity` `decimal128(38, 9)`
  - `sku_price_tier`: `start_amount` and `list_price` `decimal128(38, 9)`; `list_price` "exactly `units + nanos / 10^9`; null + counted if malformed" (check `0.031611000` for the N2 core `Default`)
  - a SKU with no price is stored with no price rows (`skus_without_price`); a price with no SKU is not stored (`prices_without_sku`)
  - price rows are repeated in every location their SKU is filed under
  - a configured location that no in-scope SKU lists (e.g. a misspelled `us-esat1`) fails that location with "unknown to the source", while the other locations succeed (spec edge case; data-model.md § ProviderSpec validation)
- [X] T030 [P] [US2] Write `tests/unit/test_gcp_config.py`, `tests/unit/test_gcp_http_pages.py` and `tests/unit/test_gcp_api.py` (MUST fail) with `FakeHttpSource`. `GcpSettings` per `contracts/configuration.md`: `GCP_BILLING_API_URL`, `GCP_BILLING_API_VERSION` (`v2beta`), `GCP_BILLING_PAGE_SIZE` (`5000`, 1–5000), `GCP_BILLING_BUSINESS_ENTITIES` (`businessEntities/GCP`); `GCP_PRICING_API_KEY` is required (missing → `SettingsError`), listed in `SECRET_FIELDS`, and absent from `repr`. `GcpPagedSource`: follows `nextPageToken` by adding `pageToken` to the listing URL; the key is sent only as the `X-goog-api-key` header and appears in no request URL; a failed page resumes from its own page token; a 400/403 with `API_KEY_INVALID`, `PERMISSION_DENIED` or `API_KEY_SERVICE_BLOCKED` raises `PermanentSourceError` without retry; a 400 `INVALID_ARGUMENT` naming the page token (an expired token) makes `resume_rejected` true, so the listing restarts once from page 1; `redact()` removes the key, so no error text contains it. `GcpCatalogClient`: exactly three listings — `<GCP_BILLING_API_URL>/v1/services?pageSize=5000`, `/v2beta/skus?pageSize=5000`, `/v2beta/skus/-/prices?pageSize=5000&currencyCode=USD`; raw pages are stored as `services-pNNNN.json`, `skus-pNNNN.json`, `prices-pNNNN.json`; `requests_made` and `throttled_responses` are reported.
- [X] T031 [P] [US2] Write `tests/integration/test_run_snapshot_gcp.py` with a fake GCP downloader over the fixtures, against `store`: `--regions us-east1,us-central1` → `succeeded`, requested `us-central1`, `us-east1`, `global`; the N2 core SKU is under both region partitions with the same `geo_regions`; raw stored once under `gcp/raw/<D>/catalog/<run_id>/`; manifest has a valid `catalog` block with `api_version: "v2beta"`; a catalog page that keeps failing → snapshot `failed` with the reason in every location, `latest.json` unchanged, failure alert naming `gcp`; a rejected key → `failed` with a key-rejected reason and no retries (spec US2 scenario 3); a missing key → CLI exit 2 and nothing written (scenario 2); accounting (SC-004): `catalog.skus_received` equals the number of distinct `sku_id` values across all `sku` partitions plus `skus_out_of_scope` plus `skus_unconfigured_locations`, and `prices_without_sku` matches the fixture's orphan price.
- [X] T032 [P] [US2] Write `tests/integration/test_no_key_leak.py`: run a GCP fixture run with key `TEST-KEY-7f3a9c` through success, rejected-key and failing-page paths, capturing logs (`caplog`), the run report JSON, alert payloads (a recording notifier) and `settings` output; walk every object written to the local root (decompressing `.zst`) and assert the key appears nowhere (SC-006).

### Implementation for User Story 2

- [X] T033 [US2] Implement `src/gcp/__init__.py`, `src/gcp/config.py` (`GcpSettings(ProviderSettings)`), `src/gcp/http_pages.py` (`GcpPagedSource(PagedHttpSource)`, overriding `next_url`, `request_headers`, `is_permanent_error`, `resume_rejected` and `redact`) and `src/gcp/api.py` (`GcpCatalogClient(settings, fetch=None, limiter=None)` with `download_catalog(dest_dir) -> CatalogDownload`) (R4, R5). The services list always uses `v1`; SKUs and prices use `GCP_BILLING_API_VERSION`.
- [X] T034 [US2] Implement `src/gcp/transformations.py`: `GcpTransform(LocationTransform)` overriding `load(json_files)` (parse the catalog once per run; index prices by `sku_id`), `transform_location(catalog, location, staging_dir, run_id, snapshot_date)` (writes the four tables with the exact pyarrow schemas of data-model.md § GCP tables, using the configured locations given to the constructor; a location no in-scope SKU lists fails with "unknown to the source") and `run_counters(catalog)` (the manifest `catalog` block). `GcpTransform.TABLE_SCHEMA_VERSIONS` (all 1); extend `tests/unit/test_providers.py` to assert it equals the `gcp` spec. Decimal conversion uses `decimal.Decimal`, never float.
- [X] T035 [US2] Implement `src/gcp/downloader.py`: `GcpCatalogDownloader(CatalogDownloader)` (`download_catalog(dest_dir)`). The `gcp` spec already points at it, at `GcpTransform` and at `GcpSettings` (T012); pass the `catalog` block (from `GcpTransform.run_counters`) to `build_revision()` from `src/pipeline/runner.py`.
- [X] T036 [US2] Make T029–T032 pass; run the full suite.

**Checkpoint**: both new providers publish locally; AWS unchanged.

---

## Phase 5: User Story 3 - An architecture can be priced from each provider's tables (Priority: P1)

**Goal**: the documented lookups (data-model.md, `contracts/storage-layout.md` § Lookup examples) work against real-shaped tables, so `cloud-pricing-app` can build its per-provider mapping (FR-016, FR-019, SC-002).

**Independent Test**: run the documented lookups with pyarrow against tables built from the fixtures; each returns the expected rows.

- [X] T037 [P] [US3] Write `tests/integration/test_lookup_examples.py` (reads Parquet from runs built as in T024/T031):
  - Azure: `location='eastus'`, `arm_sku_name='Standard_D4s_v5'`, `price_type='Consumption'`, `service_name='Virtual Machines'`, `product_name` not containing `Windows`, `sku_name` not containing `Spot` or `Low Priority` → exactly one row with unit, currency and price (spec US3 scenario 1)
  - GCP: `location='us-east1'`, `product_taxonomy` containing `VMs On Demand` and `N2`, `display_name` starting with `N2 Instance Core` / `N2 Instance Ram`, `consumption_model_description='Default'`, `start_amount=0` → one core and one RAM price; `4 × core + 16 × RAM` computed in the test (scenario 2); the same SKUs' `Compute Flexible CUD - 1 Year` prices are found
  - tiered items: every tier present with its lower bound (scenario 3)
  - price types distinguishable by columns: Azure `price_type`/`reservation_term`/`price_item_savings_plan.term`; GCP `consumption_model_description` (scenario 4)
- [X] T038 [US3] Reconcile `specs/004-add-azure-gcp-pricing/contracts/storage-layout.md`, `data-model.md` and `contracts/app-note.md` with the implemented column names and any difference T037 found; the docs and the code must name the same columns.

**Checkpoint**: lookups proven offline; the live comparison with published prices is quickstart Q8.

---

## Phase 6: User Story 4 - Providers run, fail and recover independently in the cloud (Priority: P2)

**Goal**: per-provider task definitions and schedules from one `provider_runs` map, the GCP key from an operator-created SSM parameter readable only by the GCP task, and alerts and watchdog per provider (FR-007, FR-008, FR-025–FR-027, FR-033, R12, R13).

**Independent Test**: `tofu test` for each stack; then quickstart Part C in `prod`.

- [X] T039 [P] [US4] In `infra/bootstrap/github_oidc.tf`, add `role/cloud-pricing-task-execution-gcp-${each.key}` to the run role's `iam:PassRole` resources (currently `github_oidc.tf:284-287`). Extend `infra/bootstrap/tests/oidc.tftest.hcl` to assert it.
- [X] T040 [P] [US4] In `infra/data/variables.tf` and `infra/envs/prod.tfvars`, set `pricing_providers = ["aws", "azure", "gcp"]` (raw lifecycle for `azure/raw/` and `gcp/raw/`, reader and writer access). Extend `infra/data/tests/external_access.tftest.hcl` to assert the per-provider lifecycle rules.
- [X] T041 [US4] In `infra/pipeline/variables.tf`, replace `pricing_regions`, `schedule_expression`, `schedule_enabled`, `task_cpu`, `task_memory` with the `provider_runs` map of `contracts/configuration.md` (object with `locations`, `schedule_expression`, `schedule_enabled`, `task_cpu`, `task_memory`, optional `api_key_parameter`), with validation: keys ⊆ `pricing_providers`, `api_key_parameter` set only for `gcp`. Update `infra/envs/prod.tfvars` with the AWS values unchanged and Azure/GCP `schedule_enabled = false` (FR-033).
- [X] T042 [US4] In `infra/pipeline/ecs.tf`: `aws_ecs_task_definition.pipeline` becomes `for_each = var.providers` (family `cloud-pricing-pipeline-<provider>-<env>` for new providers; the AWS family name unchanged), with `PIPELINE_PROVIDER`, `PRICING_REGIONS` and the pacing variables in `environment`; for `gcp`, a `secrets` entry `GCP_PRICING_API_KEY` ← the parameter ARN and `execution_role_arn` = new `aws_iam_role.execution_gcp` (`cloud-pricing-task-execution-gcp-<env>`) with the standard execution policy plus `ssm:GetParameters` on that one parameter ARN only. OpenTofu never creates the parameter (R12). Add `moved { from = aws_ecs_task_definition.pipeline to = aws_ecs_task_definition.pipeline["aws"] }`.
- [X] T043 [US4] In `infra/pipeline/scheduler.tf`: `aws_scheduler_schedule.weekly` becomes `for_each = var.providers` (name `cloud-pricing-pipeline-<provider>-<env>` for new providers; AWS name unchanged), each targeting its provider's task definition, `state` from `schedule_enabled`; add the `moved` block to `["aws"]`; the scheduler role's `iam:PassRole` includes the GCP execution role.
- [X] T044 [US4] In `infra/pipeline/alerts.tf`, make the `task_crashed` rule match every provider's task family (it currently matches only `family:${local.task_family}`) and include the task group in the alert text so it names the provider (FR-026). In `infra/pipeline/watchdog.tf`, set the Lambda's `PROVIDERS` to the providers whose `schedule_enabled` is `true`.
- [X] T045 [US4] Extend `infra/pipeline/tests/pipeline.tftest.hcl` (mocked provider): three task definitions and schedules; AWS addresses come from `moved` blocks (no destroy/create of `["aws"]`); Azure/GCP schedules `DISABLED`; only the GCP task definition has `secrets` and the GCP execution role; the `ssm:GetParameters` statement's only resource is the parameter ARN; no `aws_ssm_parameter` resource exists; the crash rule lists all three families; watchdog `PROVIDERS` = `aws`.
- [X] T046 [P] [US4] In `.github/workflows/run-pipeline.yml`, add a `provider` input (`aws` default, `azure`, `gcp`) that selects the provider's task definition and passes `PIPELINE_PROVIDER`; keep existing inputs and behavior for `aws`.
- [X] T047 [US4] Run `tofu fmt -check`, `tofu validate` and `tofu test` for all three stacks, and the full pytest suite.

**Checkpoint**: infrastructure ready to deploy with new schedules disabled (quickstart C1–C3).

---

## Phase 7: User Story 5 - Owner sees what each source returned and repairs snapshots (Priority: P3)

**Goal**: transform-only, retention, verify and raw access work for Azure and GCP exactly as for AWS, touching only the provider being processed (FR-023, FR-024).

**Independent Test**: transform-only, retention dry run and verify against a seeded local root for each new provider.

- [X] T048 [P] [US5] Write `tests/integration/test_transform_only_new_providers.py`: after a fixture run, `run --transform-only` for `azure` and for `gcp` publishes a new revision with identical row counts and zero source requests (`FakeHttpSource` records none); with the raw purged it exits 3 (precondition).
- [X] T049 [P] [US5] Extend `tests/unit/test_retention.py` and `tests/unit/test_raw_access.py`: retention for `azure` and `gcp` thins with the AWS rules, honors dry run and the active-manifest guard, and never lists or deletes other providers' prefixes; `raw list`/`raw download` for `gcp` use the `catalog` unit and warn that `--regions` is ignored.
- [X] T050 [US5] Make T048–T049 pass in `src/pipeline/runner.py` (transform-only for the catalog model reads the single `catalog` raw prefix), `src/pipeline/retention.py` and `src/pipeline/raw_access.py`; run the full suite.

**Checkpoint**: all stories complete offline.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [X] T051 [P] Update `README.md` (FR-029): providers, settings (`contracts/configuration.md`), the local `.env` (git-ignored; `GCP_PRICING_API_KEY` without quotes; `set -a; source .env; set +a` for venv runs), the GCP key in SSM (create and rotate with `read -rs K && aws ssm put-parameter … && unset K`), running, re-running, recovering and verifying Azure and GCP, and the v2beta note (research R4).
- [X] T052 [P] Update `infra/README.md`: the `provider_runs` map, the `moved` blocks, the GCP execution role and the operator-created parameter.
- [X] T053 Create `specs/004-add-azure-gcp-pricing/checklists/validation.md` (same shape as feature 003's) and run quickstart Part Q (Q0–Q8) locally, recording results; Q8 compares each reference-architecture price with the provider's published price for that date (SC-002).
- [ ] T054 Release with schedules disabled and run quickstart Part C: C1 (operator creates the SSM parameter), C2 (deploy; plan shows no AWS destroy), C3–C5 (on-demand runs, isolation, rotation), C9 (local and cloud runs agree, SC-009), C10 (cloud no-leak search, SC-006), C6 (enable both schedules together, FR-033), C8 (AWS unchanged); record in `checklists/validation.md`. C7 (four weeks unattended, SC-001, SC-007) is recorded when it completes.
- [ ] T055 Send `contracts/app-note.md` to the `cloud-pricing-app` owner (manifest 1.1, new providers, lookup mapping).

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (Phase 1)**: none; T002–T004 in parallel after T001.
- **Foundational (Phase 2)**: needs Setup. Blocks all stories.
- **US1 (Phase 3)**: needs Phase 2.
- **US2 (Phase 4)**: needs Phase 2; can run in parallel with US1. US1 and US2 code is in separate packages (`src/azure/`, `src/gcp/`); the only shared files they touch are `tests/unit/test_providers.py` and, for T035, `src/pipeline/runner.py`.
- **US3 (Phase 5)**: needs US1 and US2.
- **US4 (Phase 6)**: needs US1 and US2 for a meaningful deploy; T039, T040 and T046 can start after Phase 2.
- **US5 (Phase 7)**: needs US1 and US2.
- **Polish (Phase 8)**: T053 needs US1–US3 and US5; T054 needs everything; T055 after T038.

### Within each story

Tests (test-first) → client → transform → downloader/spec wiring → story checkpoint.

### Parallel opportunities

- Phase 1: T002, T003, T004.
- Phase 2: T005 first (it moves files every later task names). Then T006, T007, T009, T011, T013, T015, T017 (all test files) in parallel; then implementations in order T008 → T010, T012 → T014 → T016 → T018 → T019 → T020.
- US1: T022, T023, T024 in parallel; T025 and T026 in parallel.
- US2: T029–T032 in parallel; T033 and T034 in parallel.
- US4: T039, T040, T046 in parallel with T041–T044.
- US5: T048, T049 in parallel.

## Parallel Example: User Story 2

```text
Task: "T029 Write tests/unit/test_gcp_transformations.py"
Task: "T030 Write tests/unit/test_gcp_http_pages.py and tests/unit/test_gcp_api.py"
Task: "T031 Write tests/integration/test_run_snapshot_gcp.py"
Task: "T032 Write tests/integration/test_no_key_leak.py"
# then
Task: "T033 Implement src/gcp/http_pages.py and src/gcp/api.py"
Task: "T034 Implement src/gcp/transformations.py (GcpTransform)"
```

## Implementation Strategy

### MVP

Phases 1–3 give a working Azure provider locally with AWS unchanged. Because FR-033 ships Azure and GCP to production together, the production MVP is Phases 1–6: both providers, lookups proven, infrastructure with schedules disabled.

### Incremental delivery

1. Setup + Foundational → AWS moved to `src/aws/`, AWS suite green on the provider-neutral core, code-organization test green.
2. US1 → Azure locally (quickstart Q1).
3. US2 → GCP locally (Q2–Q5, Q7).
4. US3 → lookups (Q8).
5. US4 → deploy disabled, validate in the cloud (C1–C5), enable both (C6).
6. US5 and Polish → recovery parity, docs, four-week record.

## Phase 9: Convergence

- [X] T056 Detect a repeated page in `src/pipeline/http_pages.py` `PagedHttpSource.pages()`: if `next_url` returns a URL already fetched in this listing (a GCP `nextPageToken` or Azure `NextPageLink` that repeats), treat it as a broken page (retry, then `SourceError`) instead of looping without bound; add cases for both sources to `tests/unit/test_http_pages.py`, per spec Edge Cases (pagination: repeated page) and FR-034 (missing)
- [X] T057 Store a price whose currency differs from `PRICING_CURRENCY` as null and count it in `unparseable_prices`: Azure `unit_price`/`retail_price` and savings plan prices when `currencyCode` differs, in `src/azure/transformations.py`; GCP `list_price` when `listPrice.currencyCode` or the price's `currencyCode` differs, in `src/gcp/transformations.py`; add tests to `tests/unit/test_azure_transformations.py` and `tests/unit/test_gcp_transformations.py`, per spec Edge Cases (other currency) and FR-017 (partial)
- [X] T058 Count an absent Azure `unitPrice` or `retailPrice` as unparseable (the GCP transform already counts an absent `listPrice`), and fail a location whose received items yield no parseable price at all, with a reason, in `src/azure/transformations.py` and `src/gcp/transformations.py`; update `data-model.md` § Azure tables notes only if wording must change; add tests, per FR-017 and spec Edge Cases (a required field disappears) (partial)
- [X] T059 Complete Q8 in `checklists/validation.md`: run the Azure data-transfer lookup against live data (collect `north-america` with `eastus`), then have the owner record each reference-architecture price side by side with the provider's published price for the snapshot date, per SC-002 and T053 (partial)
