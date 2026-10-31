# Cloud Pricing Data Retrieval

Downloads AWS, Azure and GCP pricing every week, stores the raw responses (compressed), and
publishes each provider's own Parquet tables plus a versioned **snapshot manifest**, which is
the contract the `cloud-pricing-app` web app reads. Each provider is a separate weekly run with
its own manifests, `latest.json`, schedule and alerts. The runs are built to be unattended and
loud when something goes wrong.

```text
                      ┌─────────────────────── same Docker image ───────────────────────┐
 EventBridge Scheduler│→ ECS Fargate task (AWS)                                          │
 cron / systemd timer │→ home server (IAM Roles Anywhere)      python -m src.pipeline run │──▶ S3 or a local directory
 laptop / Dagster     │→ local directory                                                 │     aws/raw/…  aws/parquet/…
                      └──────────────────────────────────────────────────────────────────┘     aws/manifests/latest.json
                                                                                                      │
 alerts: SNS email (run failed/partial/crashed, task crashed, schedule failed, missed run)            ▼
                                                                                         cloud-pricing-app (reads manifests)
```

| What | Where |
|---|---|
| Commands (`run`, `retention`, `upload-history`, `raw`, `verify`, `settings`) | [`contracts/cli.md`](specs/003-pipeline-cloud-deployment/contracts/cli.md), plus the [feature 004 changes](specs/004-add-azure-gcp-pricing/contracts/cli.md) |
| Settings (environment variables, OpenTofu variables, alert kinds) | [`contracts/configuration.md`](specs/003-pipeline-cloud-deployment/contracts/configuration.md), plus the [Azure/GCP settings](specs/004-add-azure-gcp-pricing/contracts/configuration.md) |
| Consumer contract (layout, manifest 1.1 and `latest.json` schemas) | [`contracts/`](specs/004-add-azure-gcp-pricing/contracts/) (extends [feature 003's](specs/003-pipeline-cloud-deployment/contracts/)) |
| Azure and GCP tables and lookup examples | [`data-model.md`](specs/004-add-azure-gcp-pricing/data-model.md), [`storage-layout.md`](specs/004-add-azure-gcp-pricing/contracts/storage-layout.md) |
| Infrastructure and CI/CD | [`infra/README.md`](infra/README.md) |
| Running outside AWS (home server, off-AWS readers) | [`infra/data/README.md`](infra/data/README.md) |
| End-to-end validation guides | [feature 003](specs/003-pipeline-cloud-deployment/quickstart.md), [Azure and GCP](specs/004-add-azure-gcp-pricing/quickstart.md) |

> **Breaking change for consumers (feature 003):** the `_SUCCESS` / `_REGION_COMPLETE` marker files
> and `.locks/` folder are gone. Consumers read `aws/manifests/latest.json` → the manifest it names →
> exactly the files it lists. The old `$DATA_DIRECTORY_ROOT/pricing_aws/` tree is left as is and
> can be uploaded date by date with `upload-history`.

## Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt          # runtime + test dependencies
pip install -r requirements-dagster.txt      # optional: the local Dagster runner
```

## Ad hoc CLI usage (single region)

```bash
python -m src.aws.cli --region us-east-1 --all-services
```

See `python -m src.aws.cli --help` for all options. This ad hoc tool is unchanged by
the snapshot pipeline below: it downloads one region into `--output-dir` (or
`$DATA_DIRECTORY_ROOT`) and publishes no manifest.

## Snapshot pipeline

`python -m src.pipeline` runs one weekly snapshot end to end: it downloads every configured
region's price lists (retrying transient errors), stores them zstd-compressed, transforms them
into the five Parquet tables, and publishes a **snapshot manifest** plus a `latest.json` pointer.
The same command runs on a laptop, in the container on AWS Fargate, or on any other host.

```bash
export PIPELINE_STORAGE_URI=file://$PWD/.localdata   # or s3://bucket[/prefix]
python -m src.pipeline run --regions us-east-1       # all configured regions if omitted
python -m src.pipeline verify                        # check files against their manifests
python -m src.pipeline settings                      # show effective settings
```

### In Docker (same image as the cloud)

```bash
docker build -t pricing-pipeline:local --build-arg GIT_SHA=$(git rev-parse --short HEAD) .
docker run --rm -v "$PWD/.localdata:/data" -e PIPELINE_STORAGE_URI=file:///data \
  -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN \
  pricing-pipeline:local run --regions us-east-2
```

A local run needs no AWS account beyond credentials that can call the AWS Price List API. The
layout and manifest are the same as a cloud run's; only `run.host` differs
(`PIPELINE_HOST_LABEL`, default `local`).

Settings come from environment variables (see
[`contracts/configuration.md`](specs/003-pipeline-cloud-deployment/contracts/configuration.md)).
If `PIPELINE_STORAGE_URI` is unset, data goes to `$DATA_DIRECTORY_ROOT/pipeline`.

### Azure and GCP

Set `PIPELINE_PROVIDER` to `azure` or `gcp` (default `aws`). Everything else works as for AWS:
`run`, `run --regions`, `run --transform-only`, `retention`, `raw list` / `raw download`,
`verify` and `settings`, each touching only that provider's `<root>/<provider>/` prefix.
`upload-history` is AWS-only.

| | Azure | GCP |
|---|---|---|
| Source | Retail Prices API (public, no credential) | Cloud Billing Catalog API, v2beta (API key) |
| Download unit | one location: about 26 requests for `eastus` (research R1) | the whole catalog once per run: about 43 requests |
| Tables | `price_item`, `price_item_savings_plan` | `service`, `sku`, `sku_price`, `sku_price_tier` |
| Default locations | 7 regions + `north-america`, `europe`, `asia`, `intercontinental` | 7 regions |
| Pacing | 0.5 requests/s | 1 request/s |

`global` is always collected for both (prices with no location). `PRICING_REGIONS` accepts any
location the provider uses (`eastus`, `north-america`, `us-east1`, `us`). A GCP SKU that lists
several regions is filed under each configured one. Pacing is set with
`PRICING_DOWNLOAD_RATE_LIMIT_PER_SECOND` and `PRICING_DOWNLOAD_MAX_CONCURRENT_REQUESTS`.
`INCLUDE_AZURE_DEVTEST_PRICES=false` skips Azure Dev/Test prices at the source and records that
in the manifest. The full list is in
[`configuration.md`](specs/004-add-azure-gcp-pricing/contracts/configuration.md).

**The GCP API key** is never committed.

- **Locally**, put it in a git-ignored `.env` at the repo root, without quotes:
  `GCP_PRICING_API_KEY=...`. Pass it to Docker with
  `--env-file .env`, or load it for a venv run with `set -a; source .env; set +a`.
- **In AWS**, it lives in the SSM `SecureString` parameter
  `/cloud-pricing-data-retrieval/<env>/gcp-pricing-api-key`. The operator creates and rotates it; OpenTofu never
  does, so the value never enters state. ECS injects it into the GCP task only. Run this without
  echoing the key:

  ```bash
  read -rs K && aws ssm put-parameter --name /cloud-pricing-data-retrieval/prod/gcp-pricing-api-key \
    --type SecureString --value "$K" && unset K     # add --overwrite to rotate
  ```

  Rotation needs no redeploy; the next run reads the new value.
- **Restrict the key** to the Cloud Billing API in the Google Cloud console.

The key is sent only in the `X-goog-api-key` header. It is shown as `***` by `settings` and
scrubbed from logs, errors and alerts. A missing key exits 2 before anything is written; a
rejected key fails the snapshot without retries.

```bash
# Azure, one location (plus global)
PIPELINE_PROVIDER=azure python -m src.pipeline run --regions eastus
# GCP, one location (plus global); the catalog is downloaded once either way
set -a; source .env; set +a
PIPELINE_PROVIDER=gcp python -m src.pipeline run --regions us-east1
PIPELINE_PROVIDER=gcp python -m src.pipeline verify
```

**Recovering**:

- **Azure**: a failed location makes the snapshot `partial`. Re-run it with `--regions <location>`, which publishes a new revision.
- **GCP**: if the catalog can't be downloaded completely, the snapshot is `failed`. Re-run the date; it downloads the whole catalog again, about 43 requests.
- **Both**: `--transform-only` rebuilds tables from the stored raw data without contacting the source while the raw data is kept (30 days).

GCP's v2beta catalog API is a beta. If Google changes it considerably, the raw responses are kept
and stored data can be converted to a new table schema version.

**Code organization** (constitution Principle VI): shared code is in `src/pipeline/` and names
no provider. Each provider has a package, `src/aws/`, `src/azure/` and `src/gcp/`. A file that
extends a shared module has the same name, for example `src/gcp/http_pages.py` extends
`src/pipeline/http_pages.py`. `src/pipeline/providers.py` registers each provider's tables,
defaults and classes. `tests/unit/test_code_organization.py` enforces this.

### Snapshot manifest (the consumer contract)

Consumers read `<root>/<provider>/manifests/latest.json`, then the manifest it names, and load
exactly the files that manifest lists. Each file has its path, size, sha256 and row count. Never list or
glob the `parquet/` folders: after a re-run they can briefly hold files from more than one
revision. The full contract, including the storage layout and JSON Schemas, is in
[`specs/004-add-azure-gcp-pricing/contracts/`](specs/004-add-azure-gcp-pricing/contracts/)
(manifest 1.1, which extends [feature 003's](specs/003-pipeline-cloud-deployment/contracts/)).

The `_SUCCESS` / `_REGION_COMPLETE` markers and `.locks/` folder from earlier versions are no
longer written; the manifest replaces them. Runs for the same snapshot date are serialized by a
claim object (`<root>/<provider>/claims/<date>.json`), so a second concurrent run for the same
provider and date is refused. Runs for different providers never block each other.

### Optional: Dagster (local runner)

```bash
pip install -r requirements-dagster.txt
dagster dev -m src.dagster_app.definitions
```

The `pricing_snapshot` job calls the same entry point as the CLI. `pricing_weekly_schedule`
starts it every Monday at 13:00 UTC for all configured regions; from the Launchpad you can set
`snapshot_date`, `regions` or `transform_only`. The region list comes from `PRICING_REGIONS`
or the `pricing_regions` resource. Dagster runs AWS only and is not used in the cloud.

## Testing

```bash
pip install -r requirements-dev.txt -r requirements-dagster.txt
pytest
```

Tests run offline: S3 and SNS are emulated with moto, and pricing downloads use fake sources
(`tests/helpers/fake_pricing.py`) backed by fixtures in `tests/fixtures/pricing/`, including
recorded Azure and GCP pages (`azure/`, `gcp/`; no key in them). No test needs a network or a key.

- `tests/unit/` — settings, storage (local and S3), layout, retries, run claims, download core,
  transformations, manifests, `latest.json`, contract schemas, Dagster runner
- `tests/integration/` — end-to-end snapshot runs for each provider against a local root and
  against S3, the documented lookups, and the GCP no-key-leak search
