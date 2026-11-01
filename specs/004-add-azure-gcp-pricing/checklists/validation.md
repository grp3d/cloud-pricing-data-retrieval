# Validation Record: Azure and GCP Pricing Snapshots

**Feature**: 004-add-azure-gcp-pricing | **Recorded**: 2026-10-30 (during `/speckit-implement`)

## Automated tests

- [x] `pytest`: **522 passed**, 0 skipped (297 at the start of this feature; 512 at T053, plus 10 from the convergence tasks T056–T058 on 2026-10-31). Covers the Azure and GCP transforms, clients and paging; the manifest 1.1 contract; per-provider runs on local and moto S3 storage; the documented lookups; transform-only and retention per provider; the no-key-leak search; and the code-organization rules (constitution v1.3.0 Principle VI). No test uses the network or a real key.
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
- [x] Image build (2026-10-31, Docker 29.8.1, commit `7e2922f`): builds unchanged, 687 MB; the image holds `src/aws/`, `src/azure/` and `src/gcp/`, and no `.env`. `settings` works for `aws` and `azure`; `gcp` exits 2 without a key and shows `api_key` as `***` with one. A live Azure `run --regions eastus` in the container `succeeded` (manifest 1.1; `price_item` 32,648, `price_item_savings_plan` 7,123; 36 requests, 2 throttled responses retried; 239 s) and `verify` reported 0 problems.

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
| Q8 | Reference architecture (SC-002) | [x] Done 2026-10-31 by automated checks; see **Q8 results** below. All 12 components: our row equals the provider's own price API exactly; 10 also confirmed on the public pricing pages (rounding allowed). |

## Quickstart Part C: cloud (`prod`)

Not started. Each step needs the owner (T054):

- [x] C1: the operator creates `/cloud-pricing-data-retrieval/prod/gcp-pricing-api-key`.
- [ ] C2: release, and the plan shows no destroy of the AWS task definition or schedule.
- [ ] C3: on-demand runs for `azure` and `gcp`.
- [ ] C4: provider isolation, with a temporarily invalid GCP key.
- [ ] C5: key rotation without a redeploy.
- [ ] C9: a local run and a cloud run for the same location agree (SC-009).
- [ ] C10: cloud no-leak search (SC-006).
- [ ] C6: enable both schedules together.
- [ ] C8: AWS unchanged.
- [ ] C7: four weeks unattended (SC-001, SC-007).

## Convergence (T056–T058, 2026-10-31)

- [x] Stored live data from Q1/Q2 (2026-10-30) rebuilt offline with the stricter rules (repeated-page guard, currency check, counting absent prices, failing a location with no readable price): Azure `eastus` and `global`, and GCP's seven default regions plus `us`, `eu` and `global`, all `succeeded` with 0 unparseable prices and 0 requests. The full-catalog GCP transform still takes 3 s (peak 934 MiB).

## Q8 results (SC-002, T059, 2026-10-31)

Each reference component was pinned to exact rows in the stored 2026-10-30 tables (Q1/Q2; Azure data transfer from `eastus`). Two automated checks, no owner steps:

1. **Price API, exact**: a direct query to the provider's own price API for the same meter or SKU, made without the pipeline's code, returned identical values, tiers included.
2. **Public pricing page or calculator data, rounding allowed**: GCP pages list hourly prices, so monthly figures were compared ×730.

| Provider | Component | Pinned row | Our price (USD) | API | Web |
|---|---|---|---|---|---|
| Azure `eastus` | VM D4s v5, Linux, pay-as-you-go | `Virtual Machines Dsv5 Series`, `arm_sku_name` `Standard_D4s_v5` | 0.192 / hour | ✅ | ✅ calculator 0.192 |
| | Managed disk Premium SSD P10, LRS | `Premium SSD Managed Disks`, meter `P10 LRS Disk` | 19.71 / month | ✅ | ✅ calculator 19.71 |
| | Blob Hot LRS data stored | `General Block Blob v2`, meter `Hot LRS Data Stored` | 0.0208 (0–50 TB), 0.019968 (50–500 TB), 0.019136 (500 TB+) / GB-month | ✅ | not in calculator data |
| | Internet data transfer out | `Rtn Preference: MGN`, meter `Standard Data Transfer Out` | first 100 GB free, then 0.087, 0.083, 0.07, 0.05 / GB | ✅ | ✅ bandwidth page, same tiers |
| | PostgreSQL Flexible Server GP Ddsv5, 2 vCore | `…General Purpose Ddsv5 Series Compute`, `sku_name` `2 vCore` | 0.178 / hour | ✅ | ✅ calculator 0.178 |
| GCP `us-east1` | N2 vCPU, on demand | `N2 Instance Core running in Americas` (`BB77-5FDA-69D9`) | 0.031611 / vCPU-hour | ✅ | page prices load by script; not checked |
| | N2 RAM, on demand | `N2 Instance Ram running in Americas` (`5B01-D157-A097`) | 0.004237 / GiB-hour | ✅ | as above |
| | `n2-standard-4` (4 × vCPU + 16 × GiB) | derived | 0.194236 / hour | ✅ (inputs) | — |
| | Balanced PD, zonal | `Balanced PD Capacity` (`6AE1-525F-8B80`) | 0.10 / GiB-month | ✅ | ✅ 0.000136986 / GiB-hour ≈ 0.10 |
| | Cloud Storage Standard, US multi-region | `Standard Storage US Multi-region` (`0D5D-6E23-4250`) | 0.026 / GiB-month | ✅ | ✅ multi-region 0.000035616 / GiB-hour ≈ 0.026 (static page shows the `asia` column) |
| | Internet egress, Premium tier, Americas to Americas | `Network Internet Data Transfer Out from Americas to Americas` (`F274-1692-F213`) | 0–1 GiB free, then 0.12, 0.11 (from 1,024 GiB), 0.08 (from 10,240 GiB) / GiB | ✅ | ✅ same tiers |
| | Cloud SQL for PostgreSQL, zonal vCPU / RAM | `…Zonal - vCPU in Americas` (`965E-7C36-08E6`), `…Zonal - RAM in Americas` (`93DA-3F55-CB04`) | 0.0413 / vCPU-hour, 0.007 / GiB-hour | ✅ | ✅ same |

**Corrections found by this check**:

- The value first recorded for GCP Balanced PD (0.20) came from `Regional Balanced PD Capacity`, a different SKU; zonal is 0.10. The display name tells them apart (`Regional …`), and so does the last taxonomy category (`Lite` vs `Regional Lite`).
- Azure internet egress is filed under the **region** (`eastus`), product `Rtn Preference: MGN` (or `Bandwidth - Routing Preference: Internet`). The geography partitions (`north-america`, …) hold only `Bandwidth Alliance` and inter-region (`Intra Continent`) prices. A `north-america` run on 2026-10-31 had 2 items. `contracts/storage-layout.md` § Lookup examples is corrected.
- Azure tier bounds in the data are cumulative and slightly irregular (e.g. 100, 10,335, 51,295 GB), while the page rounds them to "next 10 TB" etc. They are stored as published.

Scripts and downloaded pages are in the session scratchpad (not committed).
