# Changelog

Releases are `vMAJOR.MINOR.PATCH` tags on `main`. Each entry links to the feature's design
record in `specs/`. Current behavior is documented in [`docs/`](docs/), not here.

## v0.1.0 (2026-11-10): Azure and GCP pricing

Feature [004](specs/004-add-azure-gcp-pricing/).

- Weekly Azure (Retail Prices API) and GCP (Cloud Billing Catalog API, v2beta) snapshots, each
  with its own tables, manifests, `latest.json`, schedule and alerts.
- Locations: partitions can now be multi-region locations and `global`, as well as regions.
- Manifest `1.1`: optional per-location counters, `excluded_price_types` (Azure) and `catalog`
  (GCP). AWS tables and paths are unchanged.
- GCP API key held in SSM Parameter Store and readable only by the GCP task.
- Provider code moved into `src/aws/`, `src/azure/`, `src/gcp/`.

**For consumers**: AWS manifests now say `"manifest_version": "1.1"`; any reader that accepts
`1.x` needs no change. If you validate against the bundled schema, switch to the 1.1 schema
(`src/pipeline/schemas/manifest.schema.json`).

## v0.0.1 (2026-10-01): scheduled cloud pipeline

Feature [003](specs/003-pipeline-cloud-deployment/).

- Weekly AWS snapshot on ECS Fargate (EventBridge Scheduler), with SNS alerts and a missed-run
  watchdog. The same image runs locally or off AWS.
- Snapshot manifests and `latest.json` as the consumer contract (manifest `1.0`), with
  revisions, retention and `verify`.
- Infrastructure as OpenTofu; releases deployed by GitHub Actions from tags.

**Breaking for consumers**: the `_SUCCESS` / `_REGION_COMPLETE` markers and `.locks/` folder are
gone. Read `aws/manifests/latest.json` → the manifest it names → exactly the files it lists.
The old `$DATA_DIRECTORY_ROOT/pricing_aws/` tree is left as is and can be uploaded with
`upload-history`.

## Before v0.0.1 (local only)

- Feature [002](specs/002-parquet-success-markers/) (2026-09): Parquet success markers,
  replaced by manifests in v0.0.1.
- Feature [001](specs/001-multi-region-async-pricing/) (2026-09): multi-region async AWS
  download and Parquet transform.
