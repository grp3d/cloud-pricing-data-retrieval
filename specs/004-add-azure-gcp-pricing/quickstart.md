# Quickstart: Validating Azure and GCP Pricing Snapshots

**Feature**: 004-add-azure-gcp-pricing

These steps prove the feature end to end. Record results in `checklists/validation.md`, as feature 003 did. Part Q is local and Part C is the cloud. The commands for unchanged operations (verify, retention, raw access) are in `specs/003-pipeline-cloud-deployment/quickstart.md`. Settings are in [contracts/configuration.md](contracts/configuration.md), the CLI in [contracts/cli.md](contracts/cli.md), and tables and lookups in [data-model.md](data-model.md).

## Prerequisites

- Docker, and the repo's `venv` (`pip install -r requirements-dev.txt`).
- A GCP API key restricted to the **Cloud Billing API**, in `.env` (below). Rotate the key that was stored in plain text in `docs/add_cloud_providers.txt` if that hasn't been done.
- A git-ignored `.env` at the repo root:

  ```bash
  GCP_PRICING_API_KEY=<key>
  ```

- Build the image:

  ```bash
  docker build -t pricing-pipeline:local --build-arg GIT_SHA=$(git rev-parse --short HEAD) .
  ```

## Part Q: local

| # | Scenario | Command (abridged) | Expected |
|---|---|---|---|
| Q0 | Offline suite | `pytest` | All pass. This includes the new fixture-based Azure and GCP transform, runner, manifest-1.1 contract and no-leak tests. |
| Q1 | Azure, one location | `docker run --rm -v "$PWD/.localdata:/data" -e PIPELINE_STORAGE_URI=file:///data -e PIPELINE_PROVIDER=azure pricing-pipeline:local run --regions eastus` | `succeeded`; requested = `eastus`, `global`. `verify` exits 0. `items_received` for `eastus` ≈ 25k. No `429` responses at the default pace. Record duration and peak RSS. |
| Q2 | GCP, one location | same, with `--env-file .env -e PIPELINE_PROVIDER=gcp … run --regions us-east1` | `succeeded`. `catalog.requests_made` ≈ 43 and `catalog.skus_received` ≈ 104k (research R4, measured 2026-10-30). Record duration and peak RSS (task sizing, R13). |
| Q3 | Multi-region filing | Q2 with `--regions us-east1,us-central1` | The `N2 Instance Core running in Americas` SKU (`TYPE_MULTI_REGIONAL`) appears under both `region=us-east1` and `region=us-central1`, not under `global`, with the same `geo_regions` list in each. |
| Q4 | Bad or missing key | `-e GCP_PRICING_API_KEY=` and then `=invalid` | Missing key: exits 2 before any claim. Invalid key: snapshot `failed` with a key-rejected reason, no retries, and the key value absent from the output. |
| Q5 | Page resume | Offline test: a fake source fails one SKU page, then one price page | Each retry requests only the failed page (by its page token), the run `succeeded`, and `catalog.requests_made` counts the retries. A page that keeps failing makes the snapshot `failed` with the reason. |
| Q6 | Transform-only | `run --transform-only` for Azure and GCP | New revision, no requests to the source, same row counts. |
| Q7 | No-leak search | `grep -rF "$GCP_PRICING_API_KEY" .localdata/ <captured logs>`, run without printing the key | No matches (SC-006). |
| Q8 | Reference architecture (SC-002) | Lookups from data-model.md for each provider against Q1/Q2 data | Every component found. Each price matches the provider's public pricing page or calculator for that date; record the values side by side. |

## Part C: cloud (`prod`)

| # | Scenario | Steps | Expected |
|---|---|---|---|
| C1 | Create the key parameter (one-time, operator) | `read -rs K && aws ssm put-parameter --name /cloud-pricing-data-retrieval/prod/gcp-pricing-api-key --type SecureString --value "$K" && unset K`. To rotate, add `--overwrite`. | The parameter exists. After C2, the key value is in no OpenTofu state: `read -rs K; tofu state pull \| grep -cF "$K"; unset K` prints `0` for each stack. |
| C2 | Deploy with schedules disabled | Apply bootstrap from a laptop first (`deploy.yml` doesn't apply it): the run role gains `ecs:RunTask` on the Azure and GCP task families and `iam:PassRole` on the GCP execution role. Then tag a release. CI applies data (`pricing_providers`) and pipeline (`provider_runs` map, `moved` blocks). | The plan shows **no destroy** of the AWS schedule or task definition. The Azure and GCP task definitions and schedules are created with `DISABLED` schedules. The watchdog still checks only `aws`. |
| C3 | On-demand runs | `gh workflow run run-pipeline.yml -f provider=azure`, then `-f provider=gcp` | Both `succeeded`, and `verify` exits 0 for each. The GCP task's definition lists the parameter under `secrets`, with no plain value. |
| C4 | Provider isolation | Run all three for the same date at the same time, with the GCP key parameter temporarily set to an invalid value | AWS and Azure `succeeded`. GCP `failed`, with a `RUN FAILED` alert naming `gcp`. Restore the key, re-run GCP, and it `succeeded`. |
| C5 | Rotation without redeploy | `put-parameter --overwrite` with a new key, then run GCP | `succeeded` with no apply in between. |
| C6 | Enable both schedules (FR-033) | Set `schedule_enabled = true` for Azure and GCP after C1–C5 pass, then release | Both schedules `ENABLED`, and the watchdog checks `aws,azure,gcp`. |
| C7 | Four weeks unattended (SC-001) | Watch 4 Mondays | 4 `succeeded` snapshots each for Azure and GCP, AWS unaffected, no alerts. The cost increase for `component=pipeline` is under $3/month (SC-007). |
| C9 | Local and cloud agree (SC-009) | After C3, run Q1 and Q2 locally for the same snapshot date with `--regions eastus` and `--regions us-east1`, then compare each local manifest with the cloud one for that location | Same status, same tables, and per table the same row count and the same `items_received`; for GCP also the same `catalog.skus_received`. File checksums may differ (run ids and timestamps are in the rows). |
| C10 | Cloud no-leak search (SC-006) | With the key read by `read -rs K` and never echoed: `grep -cF "$K"` over `tofu state pull` for each stack, the image (`docker save … \| tar -xO \| grep -caF "$K"`), the GCP task definition (`aws ecs describe-task-definition`), the run's CloudWatch log stream (`aws logs filter-log-events --filter-pattern` is not used, because it would send the key as a query; download the stream and search locally), and the run-pipeline workflow logs (`gh run view --log`); then `unset K` | Every count is `0`. |
| C8 | AWS unchanged (SC-008) | Compare AWS manifests and files before and after the release | Earlier AWS objects are byte-identical, and new AWS manifests differ only in `manifest_version: "1.1"`. |
