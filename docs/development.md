# Development

## Code layout

```text
src/
├── pipeline/        shared core: CLI (__main__), runner, storage, manifests, retention, retries,
│                    paging and download base classes. Names no provider.
│   └── providers.py the registry: each provider's tables, defaults and classes, by import path
├── aws/             AWS Price List client, downloader, transform; ad hoc CLI (cli.py)
├── azure/           Retail Prices client, settings, paging, downloader, transform
├── gcp/             Cloud Billing Catalog client, settings, paging, downloader, transform
├── watchdog/        Lambda that alerts when a provider's latest.json is stale
└── dagster_app/     optional local Dagster runner (AWS only)
infra/               OpenTofu stacks: bootstrap, data, pipeline
specs/               Spec Kit feature records (history; see CHANGELOG.md)
```

Rules (constitution Principle VI, enforced by `tests/unit/test_code_organization.py`):

- `src/pipeline/` names no provider. `providers.py` is the only module that refers to provider
  packages, and only by import path.
- Provider packages don't import each other.
- A provider file that extends a shared module has the same name, e.g. `src/gcp/http_pages.py`
  extends `src/pipeline/http_pages.py`.

## Adding a provider

1. Create `src/<provider>/` with a downloader, transform and (if it has settings) `config.py`.
2. Register it in `src/pipeline/providers.py`.
3. Add it to `pricing_providers` and `provider_runs` in `infra/envs/<env>.tfvars`, with
   `schedule_enabled = false` until its on-demand runs pass.
4. Document its tables in [data-contract.md](data-contract.md) and its settings in
   [configuration.md](configuration.md), and add a [changelog](../CHANGELOG.md) entry.

## Tests

```bash
pip install -r requirements-dev.txt -r requirements-dagster.txt
pytest
```

Tests run offline with no key: S3 and SNS are emulated with moto, and downloads use fake sources
(`tests/helpers/fake_pricing.py`) backed by recorded fixtures in `tests/fixtures/pricing/`
(including Azure and GCP pages).

- `tests/unit/`: settings, storage, layout, retries, claims, downloads, transforms, manifests,
  `latest.json`, contract schemas, Dagster runner, code organization.
- `tests/integration/`: end-to-end runs for each provider against a local root and S3, the
  documented lookups, and the GCP no-key-leak search.

CI (`ci.yml`) also builds the image and runs `tofu fmt`, `validate` and `test`.

## Dagster (optional)

```bash
pip install -r requirements-dagster.txt
dagster dev -m src.dagster_app.definitions
```

The `pricing_snapshot` job calls the same entry point as the CLI, and `pricing_weekly_schedule`
runs it Mondays 13:00 UTC. From the Launchpad you can set `snapshot_date`, `regions` or
`transform_only`. It runs AWS only and isn't used in the cloud.

## Documentation

`README.md` is the orientation page and shouldn't grow with each feature. When a feature
changes behavior, update the matching page in `docs/` (the current state) and add a line to
`CHANGELOG.md`. The feature's design history stays in `specs/<NNN>-<name>/`.
