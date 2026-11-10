# Cloud Pricing Data Retrieval

Downloads the public price lists of **AWS, Azure and GCP** every week and publishes them as
Parquet tables with a versioned **snapshot manifest**. The manifest is the contract that
[`cloud-pricing-app`](#using-the-data) reads. Raw source responses are kept (compressed) so
tables can be rebuilt. Runs are unattended and send an email alert when something goes wrong.

```text
 EventBridge Scheduler ──▶ ECS Fargate task, one per provider ─┐
 (weekly, per provider)      python -m src.pipeline run        │         S3 data bucket
                                                               ├──▶  <provider>/raw/…
 laptop / home server ─────▶ same Docker image or venv ────────┘     <provider>/parquet/…
                                                                     <provider>/manifests/latest.json
 SNS email alerts: run failed or partial, crash, missed run                      │
                                                                                 ▼
                                                                         cloud-pricing-app
```

| Provider | Source | Tables |
|---|---|---|
| `aws` | AWS Price List API | `service_dim`, `product_dim`, `product_attribute`, `region_dim`, `price_fact` |
| `azure` | Azure Retail Prices API (public) | `price_item`, `price_item_savings_plan` |
| `gcp` | Cloud Billing Catalog API, v2beta (API key) | `service`, `sku`, `sku_price`, `sku_price_tier` |

Each provider runs separately, with its own schedule, manifests and alerts, and can't affect
the others.

## Getting started

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt
pytest                                                # offline, no credentials needed
```

Run a one-location snapshot into a local folder:

```bash
export PIPELINE_STORAGE_URI=file://$PWD/.localdata

PIPELINE_PROVIDER=azure python -m src.pipeline run --regions eastus     # no credentials
PIPELINE_PROVIDER=aws   python -m src.pipeline run --regions us-east-1  # needs AWS credentials

# GCP: put GCP_PRICING_API_KEY=<key> in .env at the repo root (git-ignored), then
set -a; source .env; set +a
PIPELINE_PROVIDER=gcp   python -m src.pipeline run --regions us-east1

python -m src.pipeline verify      # check the files against their manifests
python -m src.pipeline settings    # show the effective settings
```

The same in Docker (the image the cloud runs):

```bash
docker build -t pricing-pipeline:local --build-arg GIT_SHA=$(git rev-parse --short HEAD) .
docker run --rm --env-file .env -v "$PWD/.localdata:/data" \
  -e PIPELINE_STORAGE_URI=file:///data -e PIPELINE_PROVIDER=gcp \
  pricing-pipeline:local run --regions us-east1
```

Code layout, tests, the optional Dagster runner and how to add a provider:
[`docs/development.md`](docs/development.md).

## Deploying

Prod runs in AWS and is deployed by GitHub Actions from release tags. Merging to `main` never
deploys. Details for each step are in [`infra/README.md`](infra/README.md).

**One-time setup** (already done for prod)

- [ ] Apply the `bootstrap` stack from a laptop: state bucket, GitHub OIDC roles, budget
      ([`infra/bootstrap/README.md`](infra/bootstrap/README.md)).
- [ ] Configure GitHub: the `prod` environment, secrets and variables, rulesets and the OIDC
      subject ([`infra/README.md`](infra/README.md#one-time-github-setup)).
- [ ] Apply the `data` stack, and create the ECR repository, from a laptop.
- [ ] Create the GCP API key in SSM ([`docs/configuration.md`](docs/configuration.md#the-gcp-api-key)).
- [ ] After the first deploy, confirm the SNS email subscription.

**Every release**

- [ ] Merge to `main` through a pull request. Check the prod `tofu plan` comment on the PR:
      no unexpected destroys.
- [ ] If the change touches `infra/bootstrap/`, apply that stack from a laptop. CI doesn't
      apply it.
- [ ] Tag the `main` commit with a new version and push it:
      `git tag -a vX.Y.Z -m "…" && git push origin vX.Y.Z`.
- [ ] Approve the run in Actions → **deploy** (Review deployments → prod). It pushes the image
      and applies the `data` and `pipeline` stacks.
- [ ] Run each changed provider on demand (Actions → **run-pipeline**) and check it `succeeded`.
- [ ] Add the release to [`CHANGELOG.md`](CHANGELOG.md).

Rolling back: Actions → deploy → Run workflow with an earlier tag. A new provider ships with
its schedule disabled and is enabled in a later release once its on-demand runs pass.

## Using the data

**Who uses the data**: `cloud-pricing-app` reads it through the read-only IAM policy
`cloud-pricing-data-read-<env>`.

Consumers read `<provider>/manifests/latest.json`, then the manifest it names, and load exactly
the files that manifest lists, never by listing `parquet/` folders. The layout, manifest fields,
every provider's table columns and example lookups are in
[`docs/data-contract.md`](docs/data-contract.md).

## Reference

| Topic | Where |
|---|---|
| Data contract: layout, manifest, tables, lookups | [`docs/data-contract.md`](docs/data-contract.md) |
| CLI commands and exit codes | [`docs/cli.md`](docs/cli.md) |
| Settings, the GCP key, OpenTofu variables | [`docs/configuration.md`](docs/configuration.md) |
| Running, recovering, retention, raw data | [`docs/operations.md`](docs/operations.md) |
| Development, tests, adding a provider | [`docs/development.md`](docs/development.md) |
| Infrastructure, CI/CD, alerts | [`infra/README.md`](infra/README.md) |
| Running or reading outside AWS | [`infra/data/README.md`](infra/data/README.md) |
| Release history | [`CHANGELOG.md`](CHANGELOG.md) |
| Design records and validation, per feature | [`specs/`](specs/) |
