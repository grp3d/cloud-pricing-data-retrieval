# Operations

How to run, re-run, recover and check the pipeline once it's deployed. Commands are in
[cli.md](cli.md); infrastructure tasks (applying stacks, alerts setup, destroy and re-create)
are in [`infra/README.md`](../infra/README.md).

## Schedule

Each provider has its own weekly EventBridge schedule (Mondays 13:00 UTC by default) and its own
ECS task definition, set by `provider_runs` in `infra/envs/prod.tfvars`. To pause one provider,
set its `schedule_enabled = false` and release; the others keep running.

## Run on demand

In GitHub: Actions → **run-pipeline**, choosing:

| Input | Values |
|---|---|
| `provider` | `aws`, `azure`, `gcp` |
| `mode` | `run`, `transform-only`, `retention` |
| `snapshot_date` | blank = today (UTC) |
| `regions` | blank = all configured; Azure and GCP always add `global` |
| `dry_run` | retention only |

Or from a terminal: `gh workflow run run-pipeline.yml -f provider=gcp -f regions=us-east1`.
For a manual `aws ecs run-task`, see [`infra/README.md`](../infra/README.md#run-the-pipeline-on-demand).

Logs are in CloudWatch (`/cloud-pricing/pipeline-<env>`). A run logs one JSON line per step:
start, each location's download and tables (`done (3/6)` or `failed (3/6): <reason>`), the GCP
catalog download, publishing, and retention. The last line is the run report.

## Recovering from a failed or partial run

| Situation | What to do |
|---|---|
| One or more locations failed (`RUN PARTIAL`, AWS or Azure) | Re-run just those: `regions=<location>`. This publishes a new revision that keeps the other locations. |
| GCP catalog download failed (`RUN FAILED`, GCP) | Re-run the date. The whole catalog is downloaded again (about 43 requests). |
| Tables are wrong but the raw data is fine | `mode=transform-only` rebuilds the tables from stored raw data without contacting the source, while raw data is kept (30 days). |
| `RUN REFUSED` | Another run held the date's claim. Wait for it to finish (or for the claim to expire, 180 min) and re-run. |
| GCP key rejected or missing | Fix the SSM parameter (see [configuration.md](configuration.md#the-gcp-api-key)) and re-run. No redeploy is needed. |

`latest.json` only moves to `succeeded` snapshots, so consumers keep reading the previous good
snapshot until the repair succeeds.

## Checking the data

```bash
PIPELINE_STORAGE_URI=s3://<bucket> PIPELINE_PROVIDER=azure python -m src.pipeline verify
```

`verify` checks every file listed by active manifests (size, sha256, row count) and exits 1 on
any mismatch. Reading S3 from outside AWS needs the reader role; see
[`infra/data/README.md`](../infra/data/README.md).

## Retention

Runs automatically at the end of every run. Raw data expires after 30 days (S3 lifecycle rule);
weekly Parquet snapshots are kept for 12 months, then one per month. To preview or run it on
demand: `mode=retention` (with `dry_run=true` to only print the plan).

## Raw data

```bash
python -m src.pipeline raw list
python -m src.pipeline raw download --snapshot-date 2026-11-09 --dest ./raw --decompress
```

Raw files are the source responses as received, compressed with zstd. They're what makes
`transform-only` and a future table schema change possible, so they're never modified.

## Alerts

Alerts are emailed through SNS and name the provider. The full list (`RUN FAILED`,
`RUN PARTIAL`, `TASK CRASHED`, `MISSED RUN`, …) and how to test them are in
[`infra/README.md`](../infra/README.md#alerts).

## Running outside AWS

The same image can run on any host (e.g. a home server) and write to the same bucket, using
IAM Roles Anywhere. Pause that provider's cloud schedule first. See
[`infra/data/README.md`](../infra/data/README.md).

## Upstream API notes

- **GCP** uses the v2beta Cloud Billing Catalog API, which is a beta. If Google changes it, the
  stored raw responses can be converted to a new table schema version.
- **Azure** paces at 0.5 requests/s and GCP at 1/s by default, to stay well inside the
  sources' throttling limits (`PRICING_DOWNLOAD_RATE_LIMIT_PER_SECOND`).
