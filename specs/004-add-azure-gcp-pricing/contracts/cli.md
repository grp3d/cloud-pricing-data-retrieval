# Contract: CLI (feature 004 changes)

This extends `specs/003-pipeline-cloud-deployment/contracts/cli.md`. All commands work for every provider, selected by `PIPELINE_PROVIDER`. Exit codes are unchanged:

| Code | Meaning |
|---|---|
| 0 | completed (including `partial`/`failed` snapshots and refusals) |
| 2 | invalid settings |
| 3 | precondition failed |
| 124 | timeout |

| Command | Change |
|---|---|
| `run [--snapshot-date D] [--regions L1,L2] [--transform-only]` | `--regions` takes locations. For Azure and GCP, `global` is always included. For GCP, `--regions` limits which locations are **built**: the catalog is still downloaded in full (43 requests), because SKUs aren't fetched per location. |
| `run --transform-only` | For GCP, it rebuilds every requested location from the single `catalog` raw prefix. |
| `raw list` / `raw download` | For GCP, the raw unit is `catalog`. `--regions` is ignored for GCP, with a warning. |
| `settings` | Shows the provider's effective defaults. `GCP_PRICING_API_KEY` is shown as `***` when set and `null` when not. |
| `retention`, `verify` | No interface change. Table lists come from the provider. |
| `upload-history` | Still AWS only: there's no Azure or GCP local history. With any other provider, exits 2. |

## Run report additions

`region_results[]` gains `items_received`, `duplicates_removed`, `unlocated_rows`, `throttled_responses` and `requests_made`. GCP reports add a `catalog` object and Azure reports may add `excluded_price_types`, matching the manifest. Alert text names the provider, as it already does.
