# Cloud Pricing Data Retrieval

Downloads AWS service pricing every week, stores the raw price lists (compressed), and publishes
five Parquet tables plus a versioned **snapshot manifest**, which is the contract the
`cloud-pricing-app` web app reads. The weekly run is built to be unattended and loud when 
something goes wrong.

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
| Commands (`run`, `retention`, `upload-history`, `raw`, `verify`, `settings`) | [`contracts/cli.md`](specs/003-pipeline-cloud-deployment/contracts/cli.md) |
| Settings (environment variables, OpenTofu variables, alert kinds) | [`contracts/configuration.md`](specs/003-pipeline-cloud-deployment/contracts/configuration.md) |
| Consumer contract (layout, manifest and `latest.json` schemas) | [`contracts/`](specs/003-pipeline-cloud-deployment/contracts/) |
| Infrastructure and CI/CD | [`infra/README.md`](infra/README.md) |
| Running outside AWS (home server, off-AWS readers) | [`infra/data/README.md`](infra/data/README.md) |
| End-to-end validation guide | [`quickstart.md`](specs/003-pipeline-cloud-deployment/quickstart.md) |

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
python -m src.aws_pricing_cli --region us-east-1 --all-services
```

See `python -m src.aws_pricing_cli --help` for all options. This ad hoc tool is unchanged by
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

### Snapshot manifest (the consumer contract)

Consumers read `<root>/aws/manifests/latest.json`, then the manifest it names, and load exactly
the files that manifest lists. Each file has its path, size, sha256 and row count. Never list or
glob the `parquet/` folders: after a re-run they can briefly hold files from more than one
revision. The full contract, including the storage layout and JSON Schemas, is in
[`specs/003-pipeline-cloud-deployment/contracts/`](specs/003-pipeline-cloud-deployment/contracts/).

The `_SUCCESS` / `_REGION_COMPLETE` markers and `.locks/` folder from earlier versions are no
longer written; the manifest replaces them. Runs for the same snapshot date are serialized by a
claim object (`<root>/aws/claims/<date>.json`), so a second concurrent run is refused.

### Optional: Dagster (local runner)

```bash
pip install -r requirements-dagster.txt
dagster dev -m src.dagster_app.definitions
```

The `pricing_snapshot` job calls the same entry point as the CLI. `pricing_weekly_schedule`
starts it every Monday at 13:00 UTC for all configured regions; from the Launchpad you can set
`snapshot_date`, `regions` or `transform_only`. The region list comes from `PRICING_REGIONS`
or the `pricing_regions` resource. Dagster is not used in the cloud.

## Testing

```bash
pip install -r requirements-dev.txt -r requirements-dagster.txt
pytest
```

Tests run offline: S3 and SNS are emulated with moto, and pricing downloads use a fake source
(`tests/helpers/fake_pricing.py`) backed by fixtures in `tests/fixtures/pricing/`.

- `tests/unit/` — settings, storage (local and S3), layout, retries, run claims, download core,
  transformations, manifests, `latest.json`, contract schemas, Dagster runner
- `tests/integration/` — end-to-end snapshot runs against a local root and against S3
