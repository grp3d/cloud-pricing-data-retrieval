# Validation Record: Scheduled Cloud Pricing Pipeline

**Feature**: 003-pipeline-cloud-deployment | **Recorded**: 2026-09-28 (during `/speckit-implement`)

## Automated tests (T096)

- [x] `pytest`: **273 passed**, 0 skipped. Covers unit and integration tests, with moto for S3 and SNS and the fake pricing source.
- [x] `tofu fmt -check -recursive infra/`: clean (OpenTofu 1.10.6 via Docker, the version CI uses). It first flagged alignment in `prod.tfvars`, which is now fixed, along with the `.example` copies.
- [x] `tofu init -backend=false` + `tofu validate`: **bootstrap, data and pipeline all valid** against the real AWS provider schemas (hashicorp/aws 6.66.0, hashicorp/archive 2.8.1).
- [x] `tofu test` with a mocked AWS provider (added for T092, and run in CI): **data 4/4, pipeline 5/5 pass**. These check that off-AWS access is disabled by default, that a CA with no allowed CNs creates no roles, the writer and reader role and profile names, the session-length bound, the writer grant appearing only when the role exists, that `schedule_enabled=false` pauses the schedule, the task definition (ARM64, `aws-ecs` host label, data bucket URI), and that the run-claim TTL must exceed the timeout.
- [x] Provider lock files (`infra/*/.terraform.lock.hcl`) are committed, with checksums for `linux_amd64` (CI), `linux_arm64` and `darwin_arm64`.
- [x] Image build: `linux/arm64` (native) and `linux/amd64` (emulated) both build. The `aws_signing_helper` 1.7.0 checksum is verified for both architectures. `docker run <img> settings` works, with a baked-in `git_sha`, non-root user `pipeline`, Python 3.14.7, pyarrow 25.0.1, and no Dagster in the image. A bad setting exits 2. The image is 646 MB unpacked (arm64).

## Quickstart Part A: local (T097)

These were run against the **real AWS Price List API**, region `us-west-1`, with a scratch `file://` root.

| # | Scenario | Result |
|---|---|---|
| A1 | Single-region run | [x] `succeeded`, 132 services (140 without a price list), 36 s, peak RSS 2.2 GB. Raw data is 14 MB compressed and Parquet 22 MB. `verify` exits 0. No `_SUCCESS` / `_REGION_COMPLETE` / `.locks`. |
| A2 | Invalid region (`xx-fake-1`) | [x] Status `failed` with a reason, `latest.json` unchanged, `RUN FAILED` alert logged. The partial → re-run → `succeeded` path is covered offline (`test_partial_then_region_rerun_succeeds`). |
| A3 | Transform-only; then with raw data purged | [x] No downloads, revision 2. The inline retention waited 300 s and deleted the 5 superseded files. `verify` exits 0. With raw data removed it exits **3** and changes nothing. |
| A4 | Retry settings | [x] Retry tests pass with `linear_backoff`/base 1 in the environment. `settings` shows the effective values. `bogus` strategy exits **2**. |
| A5 | Concurrent run for the same date | [x] Second run: `refused: run already in progress (run_id=…)`, exit 0, nothing written. The first run completed and released its claim. |
| A6 | Retention dry run vs real run | [x] The dry run changed nothing (75 → 75 files). The real run deleted exactly the 35 planned files. `verify` exits 0. The 15-month thinning case is `tests/unit/test_retention.py`. |
| A7 | `upload-history` with real legacy data (2026-09-28, 7 regions) | [x] The dry run wrote nothing. The upload published 35 files with `origin=backfill`, `raw=null` and status `succeeded`. A repeat without `--overwrite` exits **3**. `--overwrite` published revision 2. Zero raw objects in the target. `verify` exits 0. |
| A8 | Container parity (`docker run`) | [x] A real run in the arm64 image, with a mounted local folder and temporary credentials passed as environment variables: `succeeded`, 132 services, 41 s. `verify` on the host exits 0. The manifest records `git_sha` from the build argument. No marker files. Output files are owned by the host user. |
| A9 | Dagster runner | [x] `dagster job execute -j pricing_snapshot` gave `RUN_SUCCESS` and a `succeeded` manifest. |

### Defect found by the real run (fixed)

The first A1 run failed. **`ListPriceLists` throttled 17 of 272 services**, and the code turned throttling into a permanent failure after botocore's 3 quick attempts. The old code logged these errors and **silently skipped those services**, so earlier weekly snapshots may be missing services. The fix, made test-first (2 new tests):
- Throttling on the price-list lookup is retried per the `pricing_download_retry_*` policy.
- boto3 now uses the `adaptive` retry mode (client-side rate limiting).

Research R9 was updated. After the fix, A1 succeeded with 0 failed services.

## Quickstart Parts B and C: cloud and off-AWS (T099)

- [ ] **Pending deployment.** B1–B9 and C1–C5 need the AWS account setup (bootstrap apply, GitHub environment/variables), and B9/SC-001/SC-010 need 4 weeks of operation.

## Constitution v1.1.1 review checklist (T098)

| Principle | Result | Notes |
|---|---|---|
| I. Price history is irreplaceable | [x] | Region failures are never silent: failed or truncated files fail the region, "no price list" is counted, and unparseable prices are stored as null and counted. Retention has a dry run and the active-manifest guard. Recovery paths: region re-run, transform-only, `raw download`. The real-run throttling defect above was exactly this class of silent data loss, and is now fixed. |
| II. The manifest is the contract | [x] | Write order is enforced and tested. Data files are immutable and named per run. `manifest_version` and table `schema_version` are present. Stored documents are validated against the schemas (`assert_valid_contract`). Output doesn't depend on who produced it (`test_local_and_s3_manifests_are_equivalent`). |
| III. Orchestration-independent core | [x] | Logic lives in `src/pipeline/`, and the CLI, container and Dagster call `run_snapshot`. One `Storage` abstraction. All settings come from the environment and OpenTofu variables, named for their purpose (`pricing_download_retry_*`, `superseded_file_*`). |
| IV. Tested data correctness | [x] | Test-first was followed for transformations, manifest and status, `latest.json`, retention, backfill, and the throttling fix: tests were written and seen failing (collection errors or assertion failures) before each implementation. One ordering note: `build_purged_revision` and the transform-only path were implemented earlier than their story phases, still after their tests (`test_manifest.py` and the US2 runner tests). CI runs the Dagster tests. |
| V. Reproducible, cost-bounded operations | [x] | Everything is infrastructure-as-code, with stacks separated by lifecycle, no NAT, no always-on compute, and no long-lived keys (GitHub OIDC, IAM roles, Roles Anywhere). Every named resource ends in `-<env>`; account singletons are `cloud-pricing-shared-*` and tagged `environment=shared`. Prod applies need approval. `tofu validate` and `tofu test` pass for every stack. |
| VI. Provider-extensible, AWS first | [x] | The provider is modeled in the layout, manifest, `PIPELINE_PROVIDER` and `pricing_providers`. AWS-specific modules are named `aws_*`. |
| VII. Simplicity & YAGNI | [x] | No multi-provider framework. The additions beyond the plan are listed below. |

## Deviations from plan.md / tasks.md (for review)

- **New small modules**, not in the plan's source tree:
  - `src/pipeline/downloader.py`: the downloader interface, so the runner can be tested offline.
  - `src/pipeline/verify.py`: the `verify` command's logic.
  - `src/pipeline/clock.py`: the clock-skew check.
- **New setting `PIPELINE_ENVIRONMENT`**, used in alert subjects, plus the watchdog's `ENVIRONMENT`. `contracts/configuration.md` is updated.
- **OpenTofu variable renamed `providers` → `pricing_providers`**, because `providers` is a reserved variable name. The contract, tfvars, tasks and research are updated.
- **R10**: a superseded file ages from the superseding revision's time, and an orphan from its own upload time. Both conditions together would have added nothing for superseded files and made tests depend on the wall clock. Research and tasks are updated.
- **R9**: `ListPriceLists` throttling is now in scope for the retry policy, and boto3 uses `adaptive` mode (see the defect above).
- **R14**: the multi-arch image is built with QEMU on one runner (R14's documented fallback). `deploy.yml` combines image push and apply in one approval-gated job, so each deploy needs a single approval.
- **`aws_signing_helper` is pinned to 1.7.0**, checksum-verified per architecture. That's the newest version published on the AWS download host; GitHub's v1.8.5 has no binaries.
- **`infra/data/lifecycle.tf`** (T070) was created with the data stack in US1, because S3 allows one lifecycle configuration per bucket and it also holds the noncurrent-version rule.
