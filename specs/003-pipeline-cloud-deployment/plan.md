# Implementation Plan: Scheduled Cloud Pricing Pipeline with Snapshot Manifest and Retention

**Branch**: `003-pipeline-cloud-deployment` | **Date**: 2026-09-28 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/003-pipeline-cloud-deployment/spec.md`

## Summary

Move the weekly AWS pricing pipeline from a laptop running Dagster to a scheduled, pay-per-run **ECS Fargate task** in AWS. Each run executes the whole snapshot (all configured regions) in one container, with these steps:

1. Download pricing files, with configurable per-file retries.
2. Store the raw files zstd-compressed.
3. Transform one region at a time into the five Parquet tables.
4. Publish a **versioned snapshot manifest** and then the `latest.json` pointer.
5. Finish with an inline **retention step**.

All storage goes through a small `Storage` abstraction with two implementations, local directory and S3, so laptop and cloud share one code path. Consistency relies on these mechanisms:

- **Immutable revision files**: every revision writes new, run-specific data paths.
- **A per-date run claim**: an S3 conditional write, or `O_EXCL` locally.
- **A compare-and-swap update of `latest.json`**.
- **A deletion guard**: nothing listed by an active manifest is ever deleted.

The existing `_SUCCESS` / `_REGION_COMPLETE` / `.locks/` mechanism is removed.

Infrastructure is OpenTofu, split into three stacks:

- **`bootstrap`** (one-time): state bucket, GitHub OIDC roles and budget.
- **`data`** (protected): bucket, lifecycle rules and read-only policy.
- **`pipeline`** (freely destroyable): VPC without NAT, ECR, ECS task, EventBridge Scheduler, SNS, alarms, and a Lambda freshness watchdog.

GitHub Actions runs tests on pull requests, publishes an image on merges to `main`, and applies infrastructure with a manual `prod` approval. It also offers an on-demand run workflow. Dagster stays as an optional local runner that calls the same entry point.

**Off-AWS operation** (User Story 9): the Fargate schedule can be paused with `schedule_enabled=false`. A home server can then run the same multi-arch image against `s3://` with short-lived credentials from **IAM Roles Anywhere**, using the owner's own CA and separate writer and reader roles. The output is identical, so the web app reads the same way whether it runs in AWS or not, and whoever produced the data.

## Technical Context

**Language/Version**: Python 3.14. This matches the existing venv, and the stdlib `compression.zstd` is used for raw compression. The Lambda watchdog runs on the Python 3.13 runtime and uses only stdlib and boto3.

**Primary Dependencies**:
- **Runtime**: boto3, httpx, pandas, pyarrow and click, all already in use.
- **Local-only extra**: dagster and dagster-webserver, moved to `requirements-dagster.txt`.
- **Development**: pytest, pytest-asyncio and moto[s3,sns,cloudwatch].
- **Infrastructure**: OpenTofu ≥ 1.10 with the AWS provider ~> 6.x. The code stays Terraform-compatible, with no OpenTofu-only syntax.

**Storage**: Amazon S3 in the cloud, a local directory in dev. The root is set by a single URI, `file:///…` or `s3://bucket/prefix`. There is no database.

**Testing**: pytest (unit + integration). S3 behavior is tested with moto, with a real-S3 smoke test in the quickstart. `tofu validate` and `tofu plan` run in CI.

**Target Platform**: ECS Fargate, Linux/ARM64 (Graviton), in `us-east-1`. The same multi-arch image (arm64 and amd64) runs on a laptop or home server with `docker run`, against a local directory or against S3 with Roles Anywhere credentials.

**Project Type**: Batch data pipeline (CLI entry point) plus infrastructure-as-code plus CI/CD.

**Performance Goals**:
- A full 7-region run finishes in under 30 minutes. The local baseline is about 3.5 minutes, and the Fargate estimate is 10–15 minutes.
- Alerts reach the owner within 15 minutes of a run ending (SC-002).

**Constraints**:
- Transform peak memory is about **3.9 GB per region**, measured on us-east-1 with 235 files and 559 MB of JSON. Regions are therefore transformed **one at a time**, and the task is sized at 1 vCPU / 8 GB.
- There is no always-on compute and no NAT gateway.
- The pipeline's own costs stay under $3/month (SC-007).
- CI uses no long-lived AWS keys.

**Scale/Scope**:
- 7 regions, a weekly cadence, and about 1,600 raw files per snapshot.
- Raw data is about 3.3 GB uncompressed, roughly 170 MB compressed.
- Parquet is about 145 MB per snapshot.
- Steady state is about 60 weekly snapshots plus about 12 monthly snapshots per year.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Evaluated against `.specify/memory/constitution.md` **v1.1.1** (ratified 2026-09-28). The plan was first drafted before the constitution existed, so this is a full re-evaluation of the Phase 1 design.

| # | Principle | Status | Evidence in this design |
|---|---|---|---|
| I | Price History Is Irreplaceable | PASS (after fix C1) | Every non-success outcome raises an alert, and the missed-run watchdog runs outside the pipeline image (R11, R12). A service file that fails after retries now fails its region instead of being silently dropped (R9). Published data is only removed by retention, which has a dry run and the active-manifest guard (R10, FR-046). Recovery paths are the region re-run, transform-only and `raw download`. Provenance per revision covers `run_id`, `run.host`, `pipeline_version` and `raw.location` (data-model). **C1**: unparseable prices were already stored as null, but uncounted. They are now counted and reported. |
| II | The Manifest Is the Contract | PASS | Write order is data → revision → manifest → `latest.json`. Data files are immutable and named per run (R6). `manifest_version` and per-table `schema_version` exist, and consumers reject unsupported versions (`contracts/manifest.schema.json`). The data doesn't depend on who produced it (FR-056). There is no shared database. The contract is published under `contracts/` for `cloud-pricing-app`. |
| III | Orchestration-Independent Core | PASS | Logic lives in `src/pipeline/`, and the CLI, container and Dagster call `run_snapshot`. A single `Storage` abstraction serves `file://` and `s3://` (R4). All behavior is set through environment variables and OpenTofu variables with defaults (`contracts/configuration.md`). Settings are named for their purpose (`pricing_download_retry_*`, `superseded_file_*`). |
| IV | Tested Data Correctness | PASS (after fix C2) | Tests run offline with a fake downloader and moto (R21), and cover partial runs, retries, refused and stale claims, crashes between steps, and retention edge cases. **C2**: tests now also validate written manifests and `latest.json` against the JSON Schemas. **Test-first ordering applies** (see below). |
| V | Reproducible, Cost-Bounded Operations | PASS | Everything is OpenTofu, in stacks separated by lifecycle (R13). Compute is pay-per-use: Fargate and Lambda, with no NAT gateway (R3). The cost estimate is about $1.40/month (R20), and a budget alert is set at $15/$25. Credentials are OIDC for CI, IAM roles in AWS and Roles Anywhere outside it, with no access keys (R14, R22). Resources are namespaced by environment and tagged (R13). Prod changes need an approval gate. The one-time manual steps are documented: bootstrap apply, SNS confirmation, CA creation, and cost-tag activation. |
| VI | Provider-Extensible, AWS First | PASS (after fix C3) | The provider is a segment in the storage layout, a manifest field, and the `PIPELINE_PROVIDER` / `providers` setting. Lifecycle rules and the watchdog are generated per provider. Shared names are provider-neutral. **C3**: the AWS-specific transform module is renamed so provider-specific code is named as such. |
| VII | Simplicity & YAGNI | PASS | There is no multi-provider framework: the runner calls the AWS modules directly. Every added piece of complexity is listed under Complexity Tracking with the simpler alternative that was rejected. |
| — | Stack-change constraint | PASS | Uses only the ratified stack: Python, Parquet, AWS, OpenTofu/Terraform, Docker and GitHub Actions. The AWS services used are all in the constitution's approved baseline (v1.1.1, Technical Constraints). Account singletons (the state bucket, the OIDC provider and the budget) follow the v1.1.1 exception: named `cloud-pricing-shared-*` and tagged `environment=shared`. |

**Fixes folded into this plan**:
- **C1**: the transform counts prices that can't be parsed (stored as null, never estimated) per region and table. They are reported in `run.region_results[].unparseable_prices` and the RunReport, and a count above 0 is logged as a warning. They don't fail the region, because the source genuinely contains non-numeric price strings.
- **C2**: add `jsonschema` to `requirements-dev.txt`, and add `tests/unit/test_contract_schemas.py`. It validates manifests written by the runner, retention (purged), backfill and `latest.json` against `contracts/*.schema.json`. The schema files are copied to `src/pipeline/schemas/`, and a test asserts they match the spec copies byte for byte.
- **C3**: rename `src/pricing_parquet_transformations.py` to `src/aws_pricing_transformations.py` and update its imports and tests. `src/aws_pricing_api.py` is already named correctly.

**Test-first obligation for `/speckit-tasks`** (Principle IV): tests MUST come before implementation, as separate, earlier tasks, for:
- Price transformation (`aws_pricing_transformations`, including C1).
- Manifest and revision building, and status derivation (`manifest.py`).
- The `latest.json` compare-and-swap (`latest.py`).
- Retention decisions (`retention.py`): superseded files, orphans, thinning, purge, the guard, and dry-run parity.
- Backfill manifest generation (`backfill.py`).

Wiring, the CLI, Dagster, the Dockerfile, OpenTofu and workflows MAY have their tests written afterwards.

**Post-design re-check**: PASS, with C1–C3 incorporated. No unjustified violations.

## Project Structure

### Documentation (this feature)

```text
specs/003-pipeline-cloud-deployment/
├── plan.md              # This file
├── research.md          # Phase 0: decisions and rationale
├── data-model.md        # Phase 1: entities, fields, state transitions
├── quickstart.md        # Phase 1: validation guide (local + cloud)
├── contracts/
│   ├── manifest.schema.json     # Snapshot manifest (shared with cloud-pricing-app)
│   ├── latest.schema.json       # latest.json pointer
│   ├── storage-layout.md        # Object/key layout + consumer rules
│   ├── cli.md                   # Pipeline CLI commands, arguments, exit codes
│   └── configuration.md         # Env vars, OpenTofu variables, alert messages
├── checklists/requirements.md
└── tasks.md             # Phase 2 (/speckit-tasks)
```

### Source Code (repository root)

```text
src/
├── aws_pricing_api.py              # MODIFY: per-file retries via RetryPolicy; classify retryable vs permanent errors
├── aws_pricing_cli.py              # KEEP: ad hoc single-region CLI (FR-037), unchanged behavior
├── aws_regions.py                  # KEEP
├── aws_pricing_transformations.py  # RENAME from pricing_parquet_transformations.py (C3) + MODIFY: remove markers/locks; staging dir with
│                                   #   run-specific file names; return per-file stats; count unparseable prices (C1)
├── partition_markers.py            # DELETE (FR-019)
├── pipeline/                       # NEW: cloud/local pipeline package
│   ├── __init__.py
│   ├── __main__.py                 # click CLI: run | retention | upload-history | raw list/download | verify
│   ├── config.py                   # PipelineSettings from env (incl. pricing_download_retry_*)
│   ├── storage.py                  # Storage protocol, LocalStorage, S3Storage, open_storage(uri)
│   ├── layout.py                   # Key builders: <provider>/<kind>/<date>/...
│   ├── manifest.py                 # Manifest model, revision merge, status, (de)serialize, validate
│   ├── latest.py                   # latest.json compare-and-swap update
│   ├── claims.py                   # Per-snapshot-date run claim (acquire/expire/release)
│   ├── retry.py                    # RetryPolicy: fixed | linear_backoff | exponential_backoff
│   ├── runner.py                   # run_snapshot(): download → compress → transform → upload → manifest → latest → retention
│   ├── retention.py                # superseded/orphan cleanup, thinning, purge, local raw expiry, dry-run
│   ├── backfill.py                 # upload-history (legacy local Parquet → store)
│   ├── raw_access.py               # list raw snapshots with purge dates; download one
│   ├── notify.py                   # Alerts/summaries: SNS when configured, else log-only
│   └── schemas/                    # manifest.schema.json, latest.schema.json (copies of the spec contracts, C2)
├── watchdog/
│   └── freshness_check.py          # NEW: Lambda handler, alerts if latest succeeded snapshot too old
└── dagster_app/                    # MODIFY: optional local runner; one job calling pipeline.runner.run_snapshot
    ├── definitions.py
    ├── resources.py
    ├── assets/pricing_assets.py    # single op wrapping run_snapshot
    ├── jobs/pricing_jobs.py
    └── schedules/pricing_schedules.py   # weekly schedule (local only); sensor removed

infra/
├── bootstrap/                      # One-time, local apply: tfstate bucket, GitHub OIDC provider + plan/apply/run roles, AWS Budget
├── data/                           # Protected stack: data bucket, lifecycle rules, bucket policy, read-only IAM policy (prevent_destroy on bucket)
│   ├── external_access.tf          #   Roles Anywhere trust anchor (owner CA) + writer/reader roles & profiles, gated by variables
│   └── README.md                   #   CA creation, client-cert issuance/rotation, revocation (CRL)
├── pipeline/                       # Destroyable stack: VPC (public subnets, no NAT) + S3 gateway endpoint, ECR, ECS cluster/task def,
│   │                               #   IAM task/exec roles, log group, EventBridge Scheduler + DLQ, SNS topic, ECS-stopped rule,
│   │                               #   watchdog Lambda + schedule, CloudWatch alarms,
│   │                               #   sns:Publish + ECR pull attached to the external writer role (if present)
│   └── lambda/ -> ../../src/watchdog (packaged via archive_file)
└── envs/
    ├── prod.tfvars                 # non-secret settings (regions, schedule, retention, sizes)
    └── prod.backend.hcl            # state bucket/key per stack and env

.github/workflows/
├── ci.yml                          # PR: pytest, docker build (no push), tofu fmt/validate/plan (read-only role)
├── deploy.yml                      # main: test → build+push arm64 image → tofu apply data+pipeline (environment: prod, manual approval)
└── run-pipeline.yml                # workflow_dispatch: on-demand run / transform-only / retention (dry-run) via ecs run-task

Dockerfile                          # python:3.14-slim, non-root, aws_signing_helper, ENTRYPOINT python -m src.pipeline, CMD run --trigger scheduled (multi-arch)
.dockerignore
requirements.txt                    # runtime (no dagster)
requirements-dagster.txt            # optional local runner
requirements-dev.txt                # pytest, pytest-asyncio, moto, jsonschema (C2)

tests/
├── unit/
│   ├── test_retry_policy.py
│   ├── test_storage_local_s3.py    # same suite parametrized over LocalStorage and moto S3Storage
│   ├── test_layout.py
│   ├── test_manifest.py            # revision merge, status, schema validation
│   ├── test_latest_pointer.py
│   ├── test_run_claims.py
│   ├── test_retention.py           # grace, thinning, purge, guard, dry-run parity
│   ├── test_backfill.py
│   ├── test_raw_access.py
│   ├── test_notify.py
│   ├── test_freshness_check.py
│   ├── test_aws_pricing_api_async.py   # extend: retry strategies, permanent vs retryable
│   ├── test_aws_pricing_transformations.py      # renamed (C3); update: no markers, run-specific names, stats, unparseable-price counts
│   ├── test_contract_schemas.py    # C2: written manifests/latest.json validate against contracts; schema copies in sync
│   └── test_aws_regions.py
│   (DELETE: test_partition_markers.py, test_region_run_lock.py, test_pricing_sensors.py; UPDATE: test_pricing_schedule.py, test_pricing_resources.py)
└── integration/
    ├── test_run_snapshot_local.py  # full run with fake downloader: succeeded/partial/re-run/transform-only
    ├── test_run_snapshot_s3.py     # same against moto S3
    └── test_concurrent_runs.py     # refused second run, stale claim takeover
    (DELETE: test_success_markers_multi_region.py; REWRITE: test_multi_region_run.py)
```

**Structure Decision**: Single Python project. A new `src/pipeline/` package holds everything cloud-related that is independent of orchestration, and the existing modules keep their role as the pricing-download and transform core. Infrastructure lives in `infra/` as three separately-stated OpenTofu root modules, because each has a different lifecycle (one-time, protected, destroyable). CI lives in `.github/workflows/`.

## Complexity Tracking

No principle is violated. Under Principle VII, each piece of added complexity is recorded here with its justification:

| Complexity added | Why needed (current requirement) | Simpler alternative rejected because |
|---|---|---|
| Three OpenTofu stacks (`bootstrap`, `data`, `pipeline`) | They have different lifecycles: state must exist before any remote state, data must survive pipeline teardown, and the pipeline must be freely re-creatable (FR-012, FR-033, SC-008). | With a single stack and `prevent_destroy`, `tofu destroy` of the pipeline fails outright. With no protection, one command could erase history. |
| Revision history plus immutable per-run file names | Readers must never see half-written or mixed data during re-runs, and good data must never regress (FR-043, clarification). | Overwriting in place breaks SC-006 during re-runs and loses earlier revisions. |
| Per-date run claim using S3 conditional writes | Concurrent runs must be refused (FR-005), including across hosts (cloud and home server). | `flock` only works locally. A DynamoDB table is an extra resource. Listing ECS tasks doesn't cover home runs. |
| Custom `Storage` interface instead of fsspec/`pyarrow.fs` | Needs conditional writes (claims, `latest.json`) with the same semantics locally and on S3 (R4). | fsspec and `pyarrow.fs` don't expose conditional writes. |
| Lambda freshness watchdog | The missed-run alert must fire even if the image or schedule is broken, with an 8-day window (FR-027). | A CloudWatch alarm can't express an 8-day window. A check task using the same image would share the pipeline's failure modes. |
| Scheduler dead-letter queue plus alarm | Detects a schedule that fails to launch the task within hours, not days (FR-026, US3). | Relying only on the watchdog delays detection by up to 8 days. |
| IAM Roles Anywhere with an owner-managed CA | Off-AWS writers and readers without long-lived keys (FR-053, clarification). | IAM user keys are ruled out by the clarification. AWS Private CA costs $50–400/month. |
| Multi-arch image (arm64 and amd64) | Fargate uses Graviton (arm64), and typical home servers are amd64 (US9). | A single arm64 image won't run natively on an amd64 home server. amd64 on Fargate costs about 20% more. |
| Dedicated VPC (public subnets, no NAT) | Fargate needs a network, and the default VPC may not exist and isn't namespaced by environment (R3). | A NAT gateway (about $32/month) breaks the budget. |
