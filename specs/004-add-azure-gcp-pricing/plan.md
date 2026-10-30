# Implementation Plan: Azure and GCP Pricing Snapshots

**Branch**: `006-add-azure-gcp-pricing` | **Date**: 2026-10-29 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/004-add-azure-gcp-pricing/spec.md`

## Summary

Add `azure` and `gcp` as providers of the existing snapshot pipeline. Each provider has its own source client, its own catalog-native tables and its own weekly schedule. Everything else is shared, unchanged, from feature 003: layout, manifest, revisions, claims, `latest.json`, retention and alerts.

- **Azure** downloads one location at a time from the public Retail Prices API.
- **GCP** downloads its first-party catalog once per run from the Cloud Billing Catalog API's catalog-wide v2beta listings (43 requests; the key goes in a header), then files SKUs into the configured locations.

**Locations and failures**:

- Locations generalize regions: regions, multi-region locations, and an always-collected `global`.
- The GCP catalog is one download unit: a failed page is retried from that page, and a catalog that can't be completed makes the snapshot `failed`.
- Requests are paced per provider and kept to a minimum (FR-034).

**Contract**: the manifest moves to version 1.1, which is additive: wider location patterns, provider-specific table names, an optional GCP `catalog` block, optional Azure `excluded_price_types`, and per-location counters.

**Cloud**: per-provider task definitions and schedules come from one `providers` map. The GCP key reaches only the GCP task, from an operator-created SSM `SecureString`. Both new schedules ship disabled and are enabled together once validated.

## Technical Context

**Language/Version**: Python 3.14 (unchanged)

**Primary Dependencies**:
- Existing: pyarrow (Parquet, `decimal128`), click (CLI), boto3 (S3/SNS; AWS pricing).
- HTTP for Azure and GCP: the standard library `urllib.request`, wrapped by the existing retry policy and a small token-bucket limiter. No new third-party dependency.
- Infrastructure: OpenTofu 1.10.6, AWS provider 6.x (unchanged).

**Storage**: the S3 data bucket (prod) or a local directory, through the existing `Storage` abstraction. New prefixes are `azure/` and `gcp/`. The GCP key lives in SSM Parameter Store (`SecureString`, standard tier).

**Testing**: pytest, offline. Recorded and trimmed API page fixtures, fake downloaders, moto for S3. Manifests are validated against the 1.1 JSON schema. `tofu test` uses a mocked provider for the `providers` map, the `moved` blocks and the scope of the SSM grant.

**Target Platform**: ECS Fargate ARM64 (prod), Docker on a laptop, or an off-AWS host (unchanged).

**Project Type**: batch data pipeline (CLI + container) with infrastructure as code.

**Performance Goals**: a weekly run finishes well within `RUN_TIMEOUT_MINUTES` (120):
- Azure: about 190 requests in about 7 minutes (measured; research R1).
- GCP: 43 requests in about 2–4 minutes (measured; research R4).

**Constraints**:
- Request pacing within source limits (Azure 0.5 requests/s, GCP 1 request/s by default).
- No change to AWS data or tables.
- The GCP key never appears in the repo, image, OpenTofu state, task definition, logs, alerts or data.
- The added cost is under $3/month (estimated at about $0.20; R14).

**Scale/Scope**:
- 3 providers.
- Azure: about 11 configured locations plus `global`, about 200k items a week.
- GCP: 7 locations plus `global`, about 104k SKUs and 162k prices a week.
- About 30 MB of new Parquet per week.

## Constitution Check

*Checked against constitution v1.2.0, before Phase 0 and again after Phase 1. Re-checked against v1.3.0 (2026-10-30), which added Principle VI's code-organization rules; rows III, VI and VII updated.*

| Principle | Check | Result |
|---|---|---|
| I. Price history is irreplaceable | **No silent loss**: every location and the GCP catalog download has an outcome. Throttling is retried and counted. Unparseable values become explicit nulls and are counted. Rows with no source location go to `global`, with the source value null, and are counted. Out-of-scope SKUs, SKUs without price and prices without SKU are counted. Excluded Azure Dev/Test prices are recorded in the manifest. **Faithful values**: prices are stored as published (Azure `float64` from JSON numbers; GCP exact `decimal128` from units and nanos), and nothing is converted or estimated. **Recovery**: transform-only and region re-run; a GCP re-run is a full 43-request download. **Traceability**: unchanged (run, revision, version, raw). | ✅ |
| II. The manifest is the contract | Same write order and immutability. `manifest_version` 1.0 → 1.1 is a MINOR bump (only relaxed patterns and optional fields). The schema file is updated, and a note for `cloud-pricing-app` is included. Table schema versions are per provider. | ✅ |
| III. Orchestration-independent core | The new clients and transforms are plain modules. The CLI and container are unchanged wrappers. The key reaches the core only as an environment variable, with no SSM code in the core. All new values are configurable and named for their purpose (`pricing_download_rate_limit_per_second`, …). | ✅ |
| IV. Tested data correctness | Test-first applies to both transforms, the location filing rules, `derive_status` for a failed catalog, page-level resume, and the manifest 1.1 fields. The failure paths (throttling, broken pages, rejected key, failing `global`, a failing catalog page, SKU and price lists that disagree) and the no-leak check are all tested offline, from fixtures. | ✅ |
| V. Reproducible, cost-bounded operations | All infrastructure is code. One documented operator step (creating and rotating the SSM parameter) is needed because OpenTofu would store the decrypted value in state (R12); it falls under the constitution's documented-one-time-step exception. Estimated cost is about $0.20/month. The v2beta API is a beta; the owner accepted the risk, mitigated by stored raw data and per-provider table schema versions (research R4). The GCP key follows the v1.2.0 third-party-key rule: restricted, SecureString, readable only by the GCP execution role, and rotatable without a redeploy. | ✅ |
| VI. Provider-extensible, production-gated | **Code organization (v1.3.0)**: provider code lives in one package per provider, `src/aws/`, `src/azure/`, `src/gcp/`; the AWS modules move there first (file moves and import updates only). A provider file that extends a shared module has that module's name: `src/pipeline/http_pages.py` (base `PagedHttpSource`) is extended by `src/azure/http_pages.py` and `src/gcp/http_pages.py`; `src/pipeline/downloader.py`, `src/pipeline/transformations.py` and `src/pipeline/config.py` by each provider's `downloader.py`, `transformations.py` and `config.py` (AWS has no `config.py`: it has no provider-specific settings, so it uses the base `ProviderSettings`). AWS has no `http_pages.py` because it doesn't page over HTTP. `src/pipeline/` names no provider; `providers.py` is the only module that refers to provider packages, by import path. Provider packages don't import each other. `tests/unit/test_code_organization.py` enforces this. **Isolation**: each provider has its own tables, schema versions, claims, `latest.json`, schedule, retention and alerts, and they can't affect each other. **Gate**: AWS is in prod (v0.0.1), and its four weeks unattended are recorded (B9 in `specs/003-pipeline-cloud-deployment/checklists/validation.md`, 2026-10-30). | ✅ |
| VII. Simplicity & YAGNI | One new small registry (`providers.py`, a dict of three specs), no new third-party dependencies, no new AWS services beyond the approved SSM. The base classes have real variations from day one: Azure follows `NextPageLink` with no key, GCP follows `pageToken` with a key in a header. No empty subclass files. Per-provider task definitions replace a single one, and the AWS move is a one-time relocation (see Complexity Tracking). | ✅ |

**Post-design re-check**: still passes after Phase 1. No new violations came from the data model, the contracts or the quickstart.

## Project Structure

### Documentation (this feature)

```text
specs/004-add-azure-gcp-pricing/
├── plan.md
├── research.md            # R1–R15 (live Azure and GCP measurements)
├── data-model.md          # locations, ProviderSpec, Azure/GCP tables, manifest 1.1, status rules
├── quickstart.md          # Part Q (local), Part C (cloud)
├── contracts/
│   ├── manifest.schema.json   # 1.1
│   ├── latest.schema.json     # unchanged copy
│   ├── storage-layout.md      # changes vs 003 + lookup examples
│   ├── configuration.md
│   ├── cli.md
│   └── app-note.md            # for cloud-pricing-app
├── checklists/requirements.md
└── tasks.md               # /speckit-tasks
```

### Source Code (repository root)

```text
src/
├── pipeline/                       # shared, provider-neutral (constitution VI rule 1)
│   ├── providers.py                # NEW: ProviderSpec registry; the only module that refers to provider packages (by import path, resolved lazily)
│   ├── http_pages.py               # NEW: base PagedHttpSource: page loop, retry from the failed page, Retry-After, request counters (FR-034); overridable first_url, next_url, request_headers, is_permanent_error, resume_rejected (restart once), retry_after_seconds, redact
│   ├── rate_limit.py               # NEW: token bucket + concurrency cap (no provider variant)
│   ├── downloader.py               # base classes Downloader (per location) and NEW CatalogDownloader; RegionDownload, CatalogDownload; AwsPricingDownloader moves out
│   ├── transformations.py          # NEW: base class LocationTransform (load, transform_location, run_counters) and TransformResult
│   ├── config.py                   # shared settings; base ProviderSettings (from_env, SECRET_FIELDS, redaction); defaults from the spec
│   ├── layout.py                   # location pattern, tables_for(provider); AWS TABLES constant removed
│   ├── manifest.py                 # 1.1 fields, catalog block, excluded_price_types, per-provider schema versions
│   ├── runner.py                   # per_location vs catalog download models
│   ├── retention.py, verify.py, raw_access.py, backfill.py   # read table list / raw unit / legacy dir from the spec
│   └── __main__.py                 # provider-aware commands
├── aws/                            # MOVED, behavior unchanged
│   ├── api.py                      # was src/aws_pricing_api.py
│   ├── transformations.py          # was src/aws_pricing_transformations.py; adds AwsTransform(LocationTransform)
│   ├── regions.py                  # was src/aws_regions.py
│   ├── cli.py                      # was src/aws_pricing_cli.py (`python -m src.aws.cli`)
│   └── downloader.py               # AwsPricingDownloader(Downloader), was in src/pipeline/downloader.py
├── azure/                          # NEW
│   ├── config.py                   # AzureSettings(ProviderSettings): API URL and version, INCLUDE_AZURE_DEVTEST_PRICES
│   ├── http_pages.py               # AzurePagedSource: follows NextPageLink; x-ms-ratelimit retry header
│   ├── api.py                      # AzurePricesClient: per-location listings, $filter (incl. Dev/Test), raw page names
│   ├── downloader.py               # AzurePricesDownloader(Downloader)
│   └── transformations.py          # AzureTransform(LocationTransform): pages → price_item, price_item_savings_plan; location mapping
├── gcp/                            # NEW
│   ├── config.py                   # GcpSettings(ProviderSettings): API URL and version, page size, business entities, the key (secret)
│   ├── http_pages.py               # GcpPagedSource: pageToken, X-goog-api-key header, key-rejected errors, expired-token restart, key redaction
│   ├── api.py                      # GcpCatalogClient: v1 services + v2beta SKUs/prices listings, raw page names
│   ├── downloader.py               # GcpCatalogDownloader(CatalogDownloader)
│   └── transformations.py          # GcpTransform(LocationTransform): catalog → service, sku, sku_price, sku_price_tier; filing rules
└── dagster_app/                    # imports updated for src/aws/ only

tests/
├── fixtures/pricing/azure/, fixtures/pricing/gcp/   # NEW recorded, trimmed pages (no key)
├── helpers/fake_pricing.py                          # Azure/GCP fakes
├── unit/test_code_organization.py                   # NEW: enforces constitution VI rules 1 and 4
├── unit/test_azure_*.py, test_gcp_*.py, test_providers.py, test_rate_limit.py, test_http_pages.py,
│   test_manifest_v1_1.py                            # NEW
└── integration/test_run_snapshot_azure.py, test_run_snapshot_gcp.py, test_no_key_leak.py,
    test_lookup_examples.py, test_transform_only_new_providers.py   # NEW

infra/
├── bootstrap/github_oidc.tf        # PassRole for cloud-pricing-task-execution-gcp-<env>
├── data/variables.tf, envs/prod.tfvars   # pricing_providers = aws, azure, gcp
└── pipeline/
    ├── variables.tf                # providers map replaces pricing_regions / schedule_* / task_*
    ├── ecs.tf                      # task definition per provider; GCP secrets + execution role
    ├── scheduler.tf                # schedule per provider; moved blocks for aws
    ├── watchdog.tf                 # PROVIDERS = enabled providers only
    └── tests/                      # tofu test: map, moved, SSM grant scope

.github/workflows/run-pipeline.yml  # provider + services inputs
README.md                           # Azure/GCP operation, key setup and rotation (FR-029)
```

**Structure Decision**: keep the single-project layout from features 001–003, organized by constitution v1.3.0 Principle VI:
- One package per provider under `src/`. A file that extends a shared module has that module's name, so a provider's custom paging, downloading or transforming is always in `src/<provider>/<shared module name>.py`. A provider with no custom behavior for a shared module uses the base class and has no file for it.
- Shared modules in `src/pipeline/` stay provider-neutral and get provider knowledge only from `providers.py`.
- Imports go one way: provider packages import `src/pipeline/`, never each other. `tests/unit/test_code_organization.py` enforces this.
- Test files stay flat, with the provider as a prefix (`test_azure_*`, `test_gcp_*`), matching the existing `test_aws_*`.

## Implementation Notes for `/speckit-tasks`

1. **Order of work**:
   - Move AWS into `src/aws/` first, as its own step: file moves and import updates only, and the AWS suite passes with the same test count. Tests change only in import paths and `mock.patch` targets.
   - Write the code-organization test next. It fails until the shared generalization removes the AWS imports from `src/pipeline/`, and passes at the Phase 2 checkpoint.
   - Shared generalization (`providers.py`, layout/manifest/runner reading the spec). Prove it with the AWS suite passing unchanged, plus the manifest 1.1 contract tests.
   - Then Azure (US1) and GCP (US2, including the catalog model and its counters).
   - Then the lookup contract (US3), infrastructure (US4) and operations docs (US5).
2. **AWS safety**: the existing 273 tests must pass at every step, and SC-008 is checked in quickstart C8. The `moved` blocks must produce a plan with no AWS schedule or task-definition replacement. `tofu test` asserts that.
3. **GCP measurements**: done on 2026-10-29/30 against the live v1 and v2beta catalogs (research R4–R6). Raw pages from that download can seed the trimmed test fixtures; they contain no key.
4. **Spec adjustments made during planning**: FR-031 rewritten for the v2beta catalog as one download unit (clarification 2026-10-30), FR-034 (minimize requests), FR-035 (Azure Dev/Test setting), and GCP multi-region filing by member regions (FR-013). The spec's outdated "Zone 1" examples were replaced with the geography names Azure actually uses (R3).

## Complexity Tracking

| Addition | Why needed | Simpler alternative rejected because |
|---|---|---|
| `src/pipeline/providers.py` (ProviderSpec dict) | Shared modules currently import AWS table constants. Three providers need their own table lists, defaults and download models. | `if provider ==` branches spread provider knowledge into shared code (Principle VI). A plugin or registry framework would be YAGNI. |
| Moving the AWS modules into `src/aws/` | Constitution v1.3.0 Principle VI: one package per provider, and `src/pipeline/` free of provider imports (today `runner.py`, `downloader.py`, `config.py` and `manifest.py` import AWS modules). | Leaving AWS at `src/aws_*.py` would give the three providers two conventions, and the code-organization test couldn't apply to all of them. Compatibility shims at the old paths would keep two import paths alive. |
| Base classes in `http_pages.py`, `downloader.py`, `transformations.py`, `config.py`, each extended per provider | Azure and GCP page, authenticate and fail differently. AWS, Azure and GCP each need their own downloader, transform and (Azure, GCP) settings behind one runner. | A single function with provider flags would put provider knowledge in shared code (Principle VI rule 1). Separate copies of the page loop would duplicate retry-from-page and counting (FR-034). |
| Second download model (`catalog`) in the runner | GCP SKUs aren't fetched per location: one catalog download covers every location. | Downloading the catalog once per location would multiply requests by 8 (FR-034). |
| Per-provider ECS task definitions and schedules (`for_each`) | Different env, size and secrets per provider. Only the GCP task may read the key. | One task definition with overrides would give every provider's task the key's execution role (FR-007, constitution V). |
| Extra IAM execution role `cloud-pricing-task-execution-gcp-<env>` | Limits `ssm:GetParameters` on the key to the GCP task. | Granting the shared execution role access would let the AWS and Azure tasks read the key. |
| Operator-created SSM parameter (documented manual step) | An OpenTofu-managed `aws_ssm_parameter` stores the decrypted value in state on refresh. | Write-only attributes need OpenTofu ≥ 1.11. Upgrading the toolchain for one secret isn't justified. |
| Manifest `catalog` block and per-location counters | FR-031 (catalog counters), FR-032 (throttling visible), FR-034 (requests counted), FR-013 (unlocated rows counted). | Per-location counters can't describe a download shared by every location. |
