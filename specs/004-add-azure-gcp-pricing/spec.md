# Feature Specification: Azure and GCP Pricing Snapshots

**Feature Branch**: `006-add-azure-gcp-pricing`

**Created**: 2026-10-29

**Status**: Draft

**Input**: User description: "see docs/add_cloud_providers.txt, all project principles from prior iterations still apply" — Add Azure and GCP pricing to the data store, each with its own logical data model that supports building and pricing architectures the way the AWS data does, published under `<root>/azure` and `<root>/gcp` with the same conventions as `<root>/aws`.

## Context

Today the pipeline collects AWS pricing only. Since feature 003 it runs weekly in AWS (`prod`, release `v0.0.1`), writes compressed raw files and five Parquet tables under `<root>/aws/`, and publishes a per-snapshot manifest plus a `latest.json` pointer as the only contract with `cloud-pricing-app`. Retention, run claims, revisions, alerts and the missed-run watchdog all operate per provider. The storage layout, manifest and settings already model the provider explicitly (constitution Principle VI), so a new provider is meant to be an extension, not a migration.

This feature adds two providers:

- **Azure**, from the public Azure Retail Prices API (`https://prices.azure.com/api/retail/prices`), which needs no credentials.
- **GCP**, from the Cloud Billing Catalog API (`https://cloudbilling.googleapis.com`), which needs an API key.

Each provider gets its own data model (the constitution forbids a shared cross-cloud pricing schema). The models do not need to match AWS's tables, but they must be logical for that provider's catalog and must let a consumer price an architecture (for example, "two general-purpose 4-vCPU VMs, 500 GB of SSD block storage and 1 TB of object storage in region X") the way the AWS tables already allow.

Cloud provider prices from the past can't be downloaded later, so every week without an Azure or GCP snapshot after this feature ships is history lost for good (Principle I).

## Clarifications

### Session 2026-10-29

- Q: How should the GCP API key reach the deployed task? → A: An encrypted parameter in the AWS parameter store (SSM Parameter Store, SecureString), injected at task start. The infrastructure creates the parameter without its value; the operator sets the value once by a documented command. *(Revised at plan time, 2026-10-30, research R12: the operator creates the parameter as well as its value, because an OpenTofu-managed parameter stores the decrypted value in state; the infrastructure only grants read access. See FR-007.)* This adds a service to the approved AWS baseline, so it requires a constitution amendment.
- Q: Should the alert email also move to the parameter store? → A: No. The email subscription is managed as infrastructure code, so the address is in infrastructure state either way, and it is personal data rather than a credential. It stays a CI secret.
- Q: Should Azure and GCP tables copy the AWS table roles, or follow each provider's own catalog structure? → A: Each provider's own structure. The AWS tables mirror the AWS price list's own structure (products with attributes, and price terms per SKU); Azure and GCP follow the same rule and model their catalogs as the source structures them, without forcing them into AWS-shaped tables. Making lookups look alike across providers is the web app's job, through its own per-provider mapping.
- Q: If one GCP service still fails after retries, what is that week's GCP snapshot? → A: Everything else is published with status `partial`; the manifest names each failed service with its reason, and a re-run of only the failed services completes the snapshot as a new revision. Requests to both sources must also stay within the sources' rate limits. *(Superseded on 2026-10-30: with the v2beta catalog the whole GCP catalog is one 43-request download unit, so there are no per-service failures; see the session below.)*
- Q: Should Azure and GCP reach production in one release or separately? → A: Together, in one release.
- Q: How are prices not tied to one region (Azure "Global" items, GCP global SKUs) handled? → A: As an extra pseudo-region named `global`, always collected alongside the configured regions, listed with the regions in the manifest's requested, succeeded and failed lists, partitioned and re-runnable like a region; its failure makes the snapshot `partial`. `global` holds only prices the source marks as global or gives no location for.
- Q: Where does a GCP SKU that lists several specific regions go? → A: Into each configured region it lists (not `global`), with the full source region list kept on every row.
- Q: How are multi-region locations (GCP `us`, `europe`, `nam4`; Azure data transfer geographies such as "North America") handled? → A: As locations in their own right, each with its own partition, selected by configuration like regions. Every row keeps the source's own location value; where the source gives none, the row goes under `global` and its source location is null.

### Session 2026-10-30

- Q: Which GCP catalog API should the download use, given the measured request counts? → A: `v2beta`. Measured on 2026-10-30: v1 needs about 1,760 requests a run (one SKU list per each of 1,748 Google services; about 30 minutes), while v2beta's catalog-wide SKU and price listings need 43 (about 2 minutes) for the same 104k SKUs, and add commitment prices (flexible CUDs, savings plans) that v1 lacks. The beta-API risk is accepted: if v2beta changes considerably, stored data is converted from the old table schema to the new one at that time.
- Q: Should Azure Dev/Test prices be excludable, with separate dev and test settings? → A: Collected by default and excludable with one setting, `INCLUDE_AZURE_DEVTEST_PRICES`. Azure publishes Dev/Test as a single price type (`DevTestConsumption`), so separate dev and test settings would have nothing to tell apart.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Weekly Azure pricing snapshot is published (Priority: P1)

The owner gets a weekly Azure pricing snapshot for the configured Azure regions, published under `<root>/azure/` with raw files, tables, a manifest and a `latest.json` pointer, following exactly the same conventions (layout, revisions, run claims, statuses, retention, alerts) as AWS.

**Why this priority**: it starts the irreplaceable Azure history. Azure ships to production in the same release as GCP (User Story 2); its local pipeline can still be built and tested first, since it needs no credential.

**Independent Test**: Run the pipeline for provider `azure` and one region against a local directory, then verify the snapshot: `<root>/azure/manifests/<date>/manifest.json` lists every table file with size, checksum and row count, `latest.json` points at it, and verification passes.

**Acceptance Scenarios**:

1. **Given** the configured Azure regions, **When** the weekly Azure run completes for all of them, **Then** a `succeeded` manifest is published under `<root>/azure/manifests/<date>/` and `<root>/azure/manifests/latest.json` points to it.
2. **Given** one Azure region fails after its retries, **When** the run ends, **Then** the manifest is `partial`, records the failed region with a reason, `latest.json` does not move, and the owner is alerted.
3. **Given** a published Azure snapshot, **When** the owner re-runs one region for that date, **Then** a new revision is published under new file paths and earlier files are left untouched until retention removes them.
4. **Given** an Azure run is in progress for a date, **When** a second Azure run for the same date starts, **Then** it is refused and writes nothing, while an AWS or GCP run for the same date is not affected.

---

### User Story 2 - Weekly GCP pricing snapshot is published (Priority: P1)

The owner gets a weekly GCP pricing snapshot for the configured GCP regions, published under `<root>/gcp/` with the same conventions as AWS and Azure. The GCP API key is supplied through configuration (a local, git-ignored environment file, or the encrypted parameter store when deployed), never committed to the repository.

**Why this priority**: GCP is the third provider the app needs. It is equal in value to Azure and ships to production in the same release, but carries the extra work of handling a credential safely.

**Independent Test**: With an API key supplied through a local, git-ignored environment file, run the pipeline for provider `gcp` and one region against a local directory; verify the published snapshot as in User Story 1, and confirm the key appears in no stored file, manifest or log.

**Acceptance Scenarios**:

1. **Given** a valid API key and the configured GCP regions, **When** the weekly GCP run completes, **Then** a `succeeded` manifest is published under `<root>/gcp/manifests/<date>/` and `<root>/gcp/manifests/latest.json` points to it.
2. **Given** the API key is missing, **When** a GCP run starts, **Then** it fails immediately with a clear configuration error, writes no data, and alerts the owner (for scheduled runs).
3. **Given** the API key is rejected by the source, **When** a GCP run starts, **Then** the run fails without retrying the rejected requests, records the reason without revealing the key, and alerts the owner.
4. **Given** any GCP run, **When** its logs, manifests, raw files and alerts are inspected, **Then** the API key value appears in none of them.

---

### User Story 3 - An architecture can be priced from each provider's tables (Priority: P1)

A consumer (the web app, or the owner in a notebook) can take a simple reference architecture and find the matching prices in the Azure and GCP tables (compute by machine type and region, block storage, object storage, data transfer, managed database), in the same way it already can from the AWS tables.

**Why this priority**: This is the reason the data is collected. Snapshots that can't be used to price an architecture don't meet the goal.

**Independent Test**: For each provider, take a fixed reference architecture and look up each component's price in one snapshot's tables using only documented table columns. Compare each result with the provider's own published price for that item on the snapshot date.

**Acceptance Scenarios**:

1. **Given** a published Azure snapshot, **When** a consumer looks up "a specific VM size in a specific region, pay-as-you-go, Linux, per hour", **Then** exactly one price row matches, with its unit, currency and price-type stated.
2. **Given** a published GCP snapshot, **When** a consumer looks up "a specific machine family's per-vCPU-hour and per-GB-RAM-hour on-demand price in a specific region", **Then** the matching rows are found and combine into the machine price.
3. **Given** a price with usage tiers (e.g., storage or egress tiers), **When** a consumer looks it up, **Then** every tier is present with its lower bound, so a consumer can price any usage quantity.
4. **Given** the source offers several price types for an item (e.g., on-demand, spot, reservations, savings plans, committed use), **When** they are stored, **Then** each is distinguishable by explicit columns, not by parsing free text.

---

### User Story 4 - Providers run, fail and recover independently in the cloud (Priority: P2)

Azure and GCP runs are scheduled in the deployed environment alongside AWS. Each provider has its own schedule, run claim, manifest, `latest.json`, missed-run alert and retention. A failure or outage of one provider's source never blocks, delays or changes another provider's snapshot.

**Why this priority**: The cloud schedule is what makes collection reliable without the owner, but the local pipeline (Stories 1–3) delivers usable data first.

**Independent Test**: In the deployed environment, trigger on-demand runs for all three providers for the same date while one provider's source is made to fail; verify the other two publish `succeeded` snapshots and the failing one alerts the owner.

**Acceptance Scenarios**:

1. **Given** all three providers are scheduled, **When** the GCP source is unavailable for a whole run, **Then** GCP's snapshot is `failed` and alerted, and the AWS and Azure snapshots for that week are unaffected.
2. **Given** no `succeeded` Azure snapshot within the missed-run window, **When** the watchdog checks, **Then** the owner receives a missed-run alert naming `azure`, even if AWS and GCP are current.
3. **Given** the operator has set the key once in the parameter store, **When** CI deploys a release and the GCP run starts, **Then** the run receives the key with no manual console step, and the key appears in neither the infrastructure state nor the task definition.
4. **Given** the Azure or GCP schedule is switched off by configuration, **When** the schedule would have fired, **Then** no run starts for that provider, and the other providers' schedules are unchanged.
5. **Given** the GCP API key is rotated in the parameter store, **When** the next GCP run starts, **Then** it uses the new key with no redeploy.

---

### User Story 5 - Owner sees what each source returned and repairs snapshots (Priority: P3)

The owner can use the existing operations (single-region re-run, transform-only from stored raw data, raw data listing and download, retention dry run, verify) for Azure and GCP exactly as for AWS, by naming the provider.

**Why this priority**: Recovery paths are required by the constitution, but they reuse existing behavior and are needed less often.

**Independent Test**: For each new provider, run transform-only from stored raw data, a retention dry run and verify against a seeded local root; results match the AWS behavior documented in feature 003.

**Acceptance Scenarios**:

1. **Given** stored Azure raw data for a date, **When** the owner runs transform-only for `azure`, **Then** a new revision is built from the stored raw data with no download.
2. **Given** a GCP snapshot older than the table retention period, **When** retention runs, **Then** GCP is thinned under the same rules as AWS, with dry-run parity, and no other provider's data is touched.

---

### Edge Cases

- **Prices that aren't tied to any location** (GCP `global` SKUs, Azure "Global" items, or items with no location at all): filed under `global` with the source location kept (null where absent), so a consumer pricing an architecture in any location can find them. They are never dropped, and never assigned a guessed region.
- **Prices for a multi-region location**: where the source names the location itself (Azure data transfer "North America"; the few GCP SKUs located in `us` or `eu`), filed under that location's own partition when it is configured, not under `global`. Where the source lists member regions instead (most GCP multi-region and dual-region SKUs, e.g. a Nearline dual-region SKU listing `us-central1` and `us-east1`), filed under each configured member region, and marked as multi-regional.
- **A GCP SKU that lists several specific regions**: filed under each configured region it lists, with the full source region list on every row. Unconfigured regions in its list are skipped, as for single-region SKUs. It is never filed under `global`.
- **Source pagination breaks midway** (expired page token, truncated page, repeated page): treated as a temporary error and retried per the pricing download retry settings, resuming at the failed page where the source allows it (FR-034). If it still fails, the affected Azure region is recorded as failed with a reason; for GCP, the catalog download fails (FR-031). A partial download is never published as a complete region.
- **Throttling or rate limits** (HTTP 429 or quota errors): avoided in the first place by request pacing (FR-032); when they still happen, retried with backoff per the retry settings and counted in the manifest; never silently skipped (the feature 003 throttling defect must not recur).
- **The GCP catalog download fails after retries**: no GCP location can be built from a partial catalog, so the snapshot is `failed` with the reason and the owner is alerted. A re-run downloads the whole catalog again (about 43 requests).
- **GCP SKU and price lists disagree** (they are fetched separately, so the catalog can change in between): a SKU with no price is stored with no price rows, a price with no SKU is not stored, and both are counted in the manifest.
- **Location identifiers in each provider's native form** (`eastus`, `us-central1`, `nam4`, Azure geography names, plus `global`): accepted as-is, with any characters the storage layout can't hold (e.g., spaces in "North America") mapped by a documented, reversible rule; a configured region name the source doesn't recognize fails that region with a clear reason, and the others continue.
- **Prices in a currency other than the configured currency, or values that can't be parsed**: stored as explicit nulls and counted as unparseable in the manifest, never converted or estimated.
- **Duplicate items returned by the source** (the same item on two pages): kept faithfully or de-duplicated only if exactly identical, and the count of identical duplicates removed is recorded.
- **Prices with an effective date in the future** (published ahead of time): stored with their effective date so consumers can choose; never treated as current without that date being visible.
- **The GCP API key is about to be rotated or is revoked**: the next run fails with a clear credential reason and alerts; setting a new key in the parameter store (or local configuration) fixes it with no code change or redeploy.
- **The source changes or adds fields**: unknown fields are preserved in the raw data; if a required field disappears, affected rows are reported (and the region failed if none can be parsed), never filled with defaults.

## Requirements *(mandatory)*

### Functional Requirements

**Providers and sources**

- **FR-001**: The pipeline MUST support two new providers, `azure` and `gcp`, selectable wherever `aws` is selectable today (settings, runs, re-runs, transform-only, retention, verify, raw-data listing and download).
- **FR-002**: Azure pricing MUST be retrieved from the Azure Retail Prices API (`https://prices.azure.com/api/retail/prices`), paging through all results for each configured region and for prices that aren't tied to a region.
- **FR-003**: GCP pricing MUST be retrieved from the Cloud Billing Catalog API (`https://cloudbilling.googleapis.com`), using its catalog-wide SKU and price listings (API version `v2beta`), covering every public SKU of the in-scope services and all of each SKU's prices, including commitment prices, paging through all results.
- **FR-004**: Source addresses, API versions, currency (default USD), page sizes and the pricing download retry settings for each provider MUST be configurable, with safe defaults, and named for their purpose and provider where they differ by provider.
- **FR-005**: Each provider MUST have its own configurable location list, in that provider's native names, covering regions and multi-region locations (FR-013). Defaults MUST be the regions closest to the current AWS defaults (US East, US East 2/Central, US West, Europe West/UK, Japan East) plus the multi-region locations that cover them, configurable without code changes.

**Credential handling**

- **FR-006**: The GCP API key MUST be supplied only through configuration at run time (a git-ignored local environment file or shell variable for local runs; the encrypted parameter store for deployed runs, per FR-007). It MUST NOT be stored in the repository, in images, in manifests, in raw or table data, or written to logs or alerts.
- **FR-007**: In the deployed environment, the GCP API key MUST be kept as an encrypted parameter in the AWS parameter store and injected into GCP runs at task start, so the pipeline reads it the same way as in local runs (from its configuration) and contains no parameter-store-specific logic. The parameter and its value MUST be created and rotated by a documented operator command, not by the infrastructure code, so the key never appears in infrastructure state, CI logs or the task definition. The infrastructure code MUST grant read access to that parameter only to the GCP run's task, referring to it by name. Rotating the key MUST NOT require a redeploy.
- **FR-008**: Runs for providers that need no credential (Azure, AWS) MUST NOT require or receive the GCP API key.

**Storage layout and contract**

- **FR-009**: Azure and GCP data MUST be stored under `<root>/azure/` and `<root>/gcp/` respectively, with the same structure as `<root>/aws/`: compressed raw files by snapshot date, region (or source unit) and run; tables partitioned by snapshot date and region; manifests per snapshot date with revision history; a per-provider `latest.json`; and per-provider run claims.
- **FR-010**: Every Azure and GCP snapshot MUST be described by a manifest with the same fields, statuses (`succeeded`, `partial`, `failed`, `purged`), write ordering, revision rules and immutability as AWS manifests (feature 003, FR-013 to FR-019, FR-043, FR-044). The manifest's `provider` field MUST be `azure` or `gcp`.
- **FR-011**: Each Azure and GCP table MUST have its own explicit data schema version recorded in the manifest. Adding these providers MUST NOT change the AWS tables, the AWS schema versions, or any existing AWS data.
- **FR-012**: If adding these providers requires a change to the shared manifest or pointer format (for example, new location names, provider table names or GCP catalog counters), the manifest version MUST be bumped, the contract schema files updated, and a note prepared for `cloud-pricing-app`. If no format change is needed, the manifest version MUST stay the same.
- **FR-013**: Every price row MUST be filed under exactly one **location** partition, and MUST also keep the **source location** value exactly as the source published it (null where the source gives none). Locations are:
  - a **region** (e.g., `eastus`, `us-east1`);
  - a **multi-region location** the source names as a location of its own (Azure data transfer geographies such as "North America" or "Intercontinental"; the few GCP SKUs located in `us` or `eu`). GCP's catalog describes most multi-region and dual-region SKUs by listing their member regions, so those are filed under each configured member region by the rule below;
  - the **`global`** pseudo-location, only for prices the source marks as global (GCP `global`, Azure "Global") or gives no location for.

  A GCP SKU that lists several specific locations MUST be filed under each configured location it lists, never under `global`. `global` is always collected; regions and multi-region locations are collected only when configured. Every collected location appears in the manifest's requested, succeeded and failed lists, is partitioned and re-runnable like a region, and its failure makes the snapshot `partial`. The manifest MUST count the rows filed under `global` because the source gave no location, so a change in the source's behavior is visible.

**Data models**

- **FR-014**: Azure MUST have its own set of tables whose structure follows the Azure retail price catalog as the source organizes it (a mostly flat list of price items), rather than the AWS table roles. Together the tables MUST identify: the service and product family; the product, SKU and meter; the region; the price type (e.g., pay-as-you-go, spot, dev/test, reservation, savings plan) with term length where applicable; the unit of measure; the tier lower bound; the currency; the unit price and retail price as published; and the effective start date. Tables MUST be split only where the source's own structure has a one-to-many relationship (for example, one price item with several savings plan prices).
- **FR-015**: GCP MUST have its own set of tables whose structure follows the Cloud Billing catalog as the source organizes it (services; SKUs with their product and geographic taxonomies; each SKU's prices, one per consumption model such as the default on-demand price or a 1- or 3-year commitment; and each price's tiers), rather than the AWS table roles. Together the tables MUST identify: the service; the SKU and its display name; its product taxonomy categories, in the source's order; the type of its geographic scope (regional, multi-regional, global) and the regions it lists; the consumption model; the pricing unit and unit quantity; each pricing tier with its start amount; the currency; and the published list price.
- **FR-030**: Data MUST NOT be reshaped into structures the source doesn't have (for example, splitting a provider's descriptive fields into a generic attribute table, or inventing a shared "product" concept), so each table can be explained by pointing to the source structure it comes from. The table list is therefore defined per provider, and shared pipeline code (manifests, verify, retention) MUST work from each provider's own table list.
- **FR-016**: Each provider's tables MUST let a consumer price the reference architecture components (compute by machine size or by vCPU and memory, block storage, object storage, data transfer out, a managed relational database) for a given region and price type using documented columns only, without parsing free-text descriptions where the source gives structured values.
- **FR-017**: Transformations MUST be faithful to the source: no invented, estimated, defaulted, currency-converted or "corrected" prices. Values the source omits or that can't be parsed MUST be stored as explicit nulls and counted as unparseable in the manifest.
- **FR-018**: Every Azure and GCP table row MUST be traceable to its snapshot date, run, revision, pipeline version and (while retained) the raw source data, as for AWS.
- **FR-019**: Each provider's table set and columns MUST be documented in a contract file in this repo, including how each table maps to the source's structure and how to look up a price for a typical item (e.g., a VM size in a region), so `cloud-pricing-app` can build its per-provider lookup mapping against it.

**Run behavior**

- **FR-020**: Each provider MUST run as its own snapshot, with its own run claim, so concurrent runs of different providers for the same date never conflict and a failure in one provider never affects another provider's snapshot or `latest.json`.
- **FR-021**: Within a provider, a failure in one location MUST NOT stop the others. Every failure MUST be recorded with a reason, and the snapshot status MUST reflect it. Nothing may be skipped silently.
- **FR-031**: For GCP, the whole catalog (the in-scope service list, every SKU page and every price page) is one download unit, fetched once per run and shared by every location. A page that fails is retried from that page (FR-034); if the catalog still can't be downloaded completely, the snapshot MUST be `failed` with the reason, because no location can be built from a partial catalog. SKUs without a price and prices without a SKU MUST be counted in the manifest.
- **FR-032**: Requests to each provider's pricing source MUST be paced to stay within that source's published or observed rate limits, using per-provider settings for the maximum request rate and the maximum number of concurrent requests, with conservative defaults. Throttling responses MUST be counted per run and reported in the manifest, so the owner can see when the limits are being approached.
- **FR-022**: Temporary source errors (timeouts, throttling, dropped connections, broken or truncated pages) MUST be retried per the pricing download retry settings. Errors that can't be fixed by retrying (rejected credential, unknown region) MUST fail without retrying.
- **FR-023**: Raw source responses MUST be stored compressed, in a form from which the tables can be rebuilt by a transform-only run without contacting the source.
- **FR-024**: Retention (raw data lifecycle, table thinning, superseded-file cleanup, purging, dry run, active-manifest guard) MUST apply to Azure and GCP with the same rules and configurable periods as AWS, and MUST only ever touch the provider being processed.

**Deployment and operations**

- **FR-025**: Azure and GCP MUST run on a weekly, configurable schedule in the deployed environment, each switchable on and off by configuration alone. The default MUST be the same weekly cadence as AWS.
- **FR-033**: Azure and GCP MUST reach production in the same release. Neither provider's schedule is enabled in production until both pass their local and cloud validation.
- **FR-035**: Dev/Test prices (Azure price type `DevTestConsumption`) MUST be collected by default and MUST be excludable by a setting. When excluded, they are not requested from the source at all, and the manifest MUST record the excluded price type, so a snapshot without them is never mistaken for a source that published none.
- **FR-034**: Each run MUST retrieve its configured scope with as few requests to the source as practical: the largest page size the source allows; each download unit fetched once per run (the GCP catalog is downloaded once, however many locations its SKUs are filed under); retries that resume at the failed page instead of restarting the location or service, unless the source rejects the resume point; and no re-download of data already held (transform-only runs reuse stored raw data). The manifest MUST record the number of requests made, so the weekly request count is visible.
- **FR-026**: Failure, partial, refused and crash alerts and the missed-run watchdog MUST cover Azure and GCP, and every alert MUST name the provider.
- **FR-027**: All infrastructure changes MUST be code, deployed through CI with manual approval for `prod`, with the monthly cost of any new resource estimated in the plan.
- **FR-028**: The pipeline MUST stay runnable on a laptop for each new provider against a local directory, needing only access to that provider's public pricing source (and, for GCP, the API key).
- **FR-029**: The README and quickstart MUST document how to configure, run, re-run, recover and verify Azure and GCP snapshots, including how to supply and rotate the GCP API key.

### Key Entities *(include if feature involves data)*

- **Provider**: One of `aws`, `azure`, `gcp`. It is a top-level storage segment, a manifest field and a configuration value. Each provider has its own source, region list, data model, schedule, `latest.json` and run claims.
- **Azure Price Item**: One published retail price: a meter of a product SKU in a service, in a location (region, data transfer geography, or `global`), for a price type and optional term, with a unit of measure, tier lower bound, currency, unit and retail price, and an effective start date.
- **Location**: Where a price applies and the partition it is filed under: a region, a multi-region location, or `global` (FR-013). Each row also keeps the source's own location value.
- **GCP SKU**: A billable item of a GCP service, with a display name, a product taxonomy (an ordered list of categories), a geographic taxonomy (regional, multi-regional or global, with the regions it lists), and one or more prices, one per consumption model, each with one or more tiers.
- **GCP Pricing Tier**: One tier of a SKU's price for one consumption model: start amount and list price in the configured currency, in the price's unit and unit quantity.
- **Snapshot / Manifest / Latest Pointer / Run Claim / Run**: As defined in feature 003, now instantiated per provider.
- **Reference Architecture**: A fixed list of components (compute, block storage, object storage, data transfer, managed database) used to prove that each provider's tables can price an architecture.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Four consecutive weekly runs in the deployed environment publish `succeeded` Azure and GCP snapshots with no manual steps, alongside unaffected AWS snapshots.
- **SC-002**: For each provider, every component of the reference architecture can be priced from one snapshot's tables, and 100% of the looked-up prices match the provider's own published price for that item on the snapshot date.
- **SC-003**: 100% of files listed in any active Azure or GCP manifest exist and match their recorded size, checksum and row count.
- **SC-004**: For a run where the source returns N distinct price items (Azure price items, GCP SKUs) for the collected locations, all N are accounted for: either stored (including items stored with unparseable values as nulls), or not stored and counted in the manifest under a stated reason (identical duplicates; for GCP, services outside the configured scope and SKUs whose listed regions are all unconfigured), with zero items unaccounted for. Items filed under several locations count once. Prices whose SKU is missing from the SKU listing are counted separately and are not part of N.
- **SC-005**: A simulated outage of one provider's source during a scheduled week produces a failure alert for that provider within 15 minutes of the run ending, and the other two providers publish `succeeded` snapshots that week.
- **SC-006**: A search of the repository, images, infrastructure state, task definitions, CI logs, stored data, manifests, run logs and alerts finds the GCP API key value zero times.
- **SC-007**: Adding both providers raises the pipeline's own monthly cloud cost by no more than $3 at the default region lists and weekly cadence, keeping the project inside its $15–20 normal budget.
- **SC-008**: Existing AWS snapshots, tables, schema versions and `latest.json` are byte-for-byte unchanged by this feature, and the AWS weekly run keeps succeeding throughout.
- **SC-009**: A local run for each new provider and one region completes, and its result is equivalent to a cloud run for the same provider and region.

## Assumptions

- **AWS-first gate**: Constitution Principle VI allows another provider only after the AWS pipeline works end to end in production. This spec assumes that gate is met by the `v0.0.1` deployment and the four-week unattended check (feature 003, B9, due about 2026-10-26); the result should be recorded before implementation starts.
- **Constitution review**: the constitution asks for a review when a new provider is added. Principle VI's "AWS first" wording and the Technical Constraints' AWS-only phrasing are expected to need a PATCH or MINOR amendment, handled through `/speckit-constitution` alongside this feature. Adding the AWS parameter store (SSM Parameter Store, standard tier) for FR-007 extends the approved AWS service baseline and requires a constitution amendment (MINOR) before implementation. It adds no cost at the standard tier.
- The pipeline keeps running in AWS for all three providers. Azure and GCP pricing is only read from their public APIs; no Azure or GCP account resources are created.
- Prices are collected in USD only. Other currencies are out of scope.
- The weekly cadence and the existing retention periods (raw 30 days, tables 12 months then monthly) apply unchanged to the new providers.
- Default Azure regions are `eastus`, `eastus2`, `westus`, `westus2`, `westeurope`, `uksouth`, `japaneast`; default GCP regions are `us-east1`, `us-east4`, `us-west1`, `us-west2`, `europe-west1`, `europe-west2`, `asia-northeast1`. Default multi-region locations are the Azure data transfer geographies that contain these regions. GCP has no default multi-region locations, because its catalog lists the member regions of multi-region SKUs (measured 2026-10-30). The plan may adjust these if measurement shows a better match; they are configuration, not code.
- The GCP Cloud Billing Catalog is downloaded once per run, catalog-wide, rather than per region, because SKUs list their regions. Filtering to configured locations (plus `global`) happens when building the tables; raw files may therefore be organized by source unit rather than by region, which the plan defines within the per-provider layout.
- The location identifier rules in the storage layout will need widening to accept Azure's native names (no hyphen, e.g. `eastus`), multi-region names (`us`, `eu`), Azure geography names and `global`; this is a layout validation change, not a contract change.
- The GCP API key that was shared in `docs/add_cloud_providers.txt` must be removed from that file before it is committed, and should be restricted to the Cloud Billing API and rotated, since it has been stored in plain text.
- Compute sizing per provider (memory, duration) is measured at plan time. Azure's catalog is large (millions of items overall) but is filtered by region, so each provider's run is expected to fit within the existing task sizing or a modest per-provider increase.
- `cloud-pricing-app` consumes the new providers in a separate, later feature. It keeps one lookup experience across providers by mapping each lookup (e.g., "price this VM size in this region") to each provider's own tables. This feature delivers the data, the contract documentation (with lookup examples) and the app note only.

## Out of Scope

- Changes to the AWS data model or AWS tables.
- A shared cross-cloud schema, cross-provider SKU mapping or "equivalent instance" matching.
- Pricing that needs an account or agreement (enterprise agreements, negotiated discounts, customer-specific prices, marketplace third-party offers beyond what the public sources return).
- Currencies other than USD.
- Price trend analytics and the app's architecture-pricing feature itself.
- Uploading historical Azure or GCP data (none exists).
- Provisioning `dev` and `qa` environments.
