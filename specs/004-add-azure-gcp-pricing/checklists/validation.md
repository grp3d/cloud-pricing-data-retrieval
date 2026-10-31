# Validation Record: Azure and GCP Pricing Snapshots

**Feature**: 004-add-azure-gcp-pricing | **Recorded**: 2026-10-30 (during `/speckit-implement`)

## Automated tests

- [x] `pytest`: **512 passed**, 0 skipped (297 at the start of this feature). Covers the Azure and GCP transforms, clients and paging; the manifest 1.1 contract; per-provider runs on local and moto S3 storage; the documented lookups; transform-only and retention per provider; the no-key-leak search; and the code-organization rules (constitution v1.3.0 Principle VI). No test uses the network or a real key.
- [x] `tofu fmt -check -recursive infra/`: clean (OpenTofu 1.12.6 locally; CI uses 1.10.6).
- [x] `tofu validate`: pipeline valid.
- [x] `tofu test` with a mocked AWS provider: **bootstrap 4/4, data 7/7, pipeline 9/9 pass**. These cover:
  - one task definition and schedule per provider, with AWS keeping its feature-003 names and container-definition shape (`moved` blocks)
  - Azure and GCP schedules `DISABLED`
  - only the GCP task having `secrets`, and the GCP execution role's `ssm:GetParameters` limited to the one parameter ARN
  - `api_key_parameter` required for, and only allowed for, `gcp`
  - `TASK CRASHED` covering all three families, and the watchdog checking only scheduled providers
  - per-provider raw expiry rules
  - the run role covering all three families and passing the GCP execution role
- [ ] Image build. Docker isn't available on this machine. The `Dockerfile` is unchanged (it copies `src/`, which now includes `src/aws/`, `src/azure/` and `src/gcp/`), and CI builds the image on the pull request.

## Quickstart Part Q: local

These were run against the **live** sources on 2026-10-30, from the repo's venv (same code as the image) with a scratch `file://` root. The GCP key came from `.env` and was never printed.

| # | Scenario | Result |
|---|---|---|
| Q0 | Offline suite | [x] 512 passed (above). |
| Q1 | Azure, `--regions eastus` | [x] `succeeded`; requested `eastus`, `global`. `eastus`: 25,222 items, 26 requests. `global`: 7,426 items (3,967 with no location), 8 requests. 0 duplicates, 0 unparseable, **0 throttled**. Tables: `price_item` 32,648 rows, `price_item_savings_plan` 7,123. 105 s, peak RSS 234 MiB. `verify` exits 0. |
| Q2 | GCP, `--regions us-east1` | [x] `succeeded`. `catalog.requests_made` **43**, `skus_received` **104,224**, 98 out of scope (Maps/Search), 444 unlocated, 0 SKUs without price, 0 prices without SKU, 0 throttled. `us-east1`: 2,504 SKUs; `global`: 15,900. Tables: `service` 1,748, `sku` 18,404, `sku_price` 25,054, `sku_price_tier` 28,292. 138 s, peak RSS 959 MiB (task sizing: 2 GiB is enough). A separate full-catalog transform of 8 locations peaked at 933 MiB and took 3 s. |
| Q3 | Multi-region filing (`us-east1,us-central1`) | [x] Run as transform-only from Q2's stored catalog, so no new requests. `N2 Instance Core running in Americas` (`BB77-5FDA-69D9`, `TYPE_MULTI_REGIONAL`) is under both `region=us-east1` and `region=us-central1` with `geo_regions` `[us-central1, us-east1, us-west1]` in each, and not under `global`. `verify` exits 0. |
| Q4 | Missing, then invalid key | [x] Missing: exit **2** (`GCP_PRICING_API_KEY is required…`), and nothing was created under the root. Invalid: snapshot `failed`, `catalog.requests_made` **1** (no retries), reason `catalog download failed: HTTP 400 … API key not valid … INVALID_ARGUMENT`. The invalid key's value appears in no output. |
| Q5 | Page resume | [x] Offline: `tests/unit/test_gcp_api.py` (a failed page retries its own token; an expired token restarts the listing once), `tests/unit/test_http_pages.py`, and `test_catalog_page_that_keeps_failing_fails_the_snapshot` (`failed` with the reason in every location). |
| Q6 | Transform-only | [x] Azure: revision 2, same row counts, no requests. GCP: revision 2 (Q3), `catalog.requests_made` 0. With the raw data gone, both are refused (`PreconditionError`, CLI exit 3) by `tests/integration/test_transform_only_new_providers.py`. |
| Q7 | No-leak search | [x] The real key was searched for, without printing it, in all 121 output files (logs, manifests, `latest.json`, Parquet, decompressed raw): **0 matches**. |
| Q8 | Reference architecture (SC-002) | [ ] **Lookups**: every component was found in the live data. **Comparison with published prices**: not done in this session; needs the owner. Values found (USD, 2026-10-30): <ul><li>Azure `eastus`: D4s v5 Linux 0.192 / hour; Premium SSD P10 LRS 19.71 / month; Blob Hot LRS 0.0208 / GB-month; PostgreSQL 144 consumption rows. Data transfer uses the `north-america` geography, which Q1 didn't collect; the lookup works on fixtures.</li><li>GCP `us-east1`: N2 0.031611 / vCPU-hour and 0.004237 / GiB-hour, so `n2-standard-4` = 0.194236 / hour; Balanced PD 0.20 / GiB-month; GCS Standard 0.026 (US multi-region) / GiB-month; 32 Premium egress SKUs; 25 Cloud SQL Postgres compute SKUs.</li></ul> |

## Quickstart Part C: cloud (`prod`)

Not started. Each step needs the owner (T054):

- [ ] C1: the operator creates `/cloud-pricing/prod/gcp-pricing-api-key`.
- [ ] C2: release, and the plan shows no destroy of the AWS task definition or schedule.
- [ ] C3: on-demand runs for `azure` and `gcp`.
- [ ] C4: provider isolation, with a temporarily invalid GCP key.
- [ ] C5: key rotation without a redeploy.
- [ ] C9: a local run and a cloud run for the same location agree (SC-009).
- [ ] C10: cloud no-leak search (SC-006).
- [ ] C6: enable both schedules together.
- [ ] C8: AWS unchanged.
- [ ] C7: four weeks unattended (SC-001, SC-007).
