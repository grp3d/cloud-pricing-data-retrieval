# Quickstart & Validation Guide: Scheduled Cloud Pricing Pipeline

**Feature**: 003-pipeline-cloud-deployment

This guide proves the feature works from start to finish, first locally and then in AWS. It points to [contracts/](./contracts/) for command options, settings and file formats instead of repeating them. Each scenario lists the spec items it proves.

## Prerequisites

- Python 3.14 with a venv, and `pip install -r requirements.txt -r requirements-dev.txt`. Add `-r requirements-dagster.txt` for the optional Dagster runner.
- Docker (with buildx) for image scenarios.
- AWS credentials allowed to call the Pricing API (`pricing:*`). These are needed even for local runs.
- For the cloud scenarios: OpenTofu ≥ 1.10, the AWS CLI v2, an AWS account, and admin rights for the one-time bootstrap.

## Automated tests (every PR)

```bash
pytest                                   # unit + integration (moto for S3; fake downloader, no network)
```

Expected: everything passes. The following integration tests cover the core scenarios offline:

- `test_run_snapshot_local.py` and `test_run_snapshot_s3.py`: succeeded, partial, a single-region re-run, transform-only, and retries.
- `test_concurrent_runs.py`: a refused run and a stale claim being taken over.
- `test_retention.py`: 15 months of seeded history, dry-run matching the real run, and the active-manifest guard.

---

## Part A — Local validation

```bash
export PIPELINE_STORAGE_URI=file://$PWD/.localdata      # throwaway root
```

### A1. Single-region local run (US7, SC-009)

```bash
python -m src.pipeline run --regions us-east-1
```

Expected:
- `.localdata/aws/` contains `raw/<today>/us-east-1/<run_id>/*.json.zst`, `parquet/<table>/snapshot_date=<today>/region=us-east-1/part-<run_id>.parquet` for all five tables, `manifests/<today>/manifest.json` (status `succeeded`, revision 1) and `manifests/latest.json`.
- There are no `_SUCCESS`, `_REGION_COMPLETE` or `.locks` files.
- `python -m src.pipeline verify` exits 0.

### A2. Partial run, then a single-region repair (US3 AS1, US4 AS1, SC-002 logic)

```bash
python -m src.pipeline run --snapshot-date 2026-01-05 --regions us-east-1,xx-fake-1
```

Expected:
- The manifest has status `partial`, and `regions.failed` contains `xx-fake-1` with a reason and `attempts` = 1 (a permanent error, so it isn't retried).
- `latest.json` has not changed.
- A `RUN PARTIAL` alert is logged, because `ALERT_TOPIC_ARN` is unset.

The partial → re-run → `succeeded` path needs a region that fails first and succeeds later. That can't be reproduced reliably against the live API, so it is proven offline by `tests/integration/test_run_snapshot_local.py::test_partial_then_region_rerun_succeeds`, which uses the fake downloader. That test also checks that revision 2 carries forward the untouched regions' files and that `latest.json` moves only on `succeeded`.

### A3. Transform-only rebuild (US4 AS2 and AS3)

```bash
python -m src.pipeline run --snapshot-date <A1 date> --transform-only
```

Expected:
- There are no Pricing API calls in the log.
- A new revision has new `part-<run_id>` files.
- The old files are still present until the grace period (5 min) has passed, and are then removed by the run's inline retention step. The log shows `waited_seconds` ≤ 300.

Next, delete that date's `raw/` folder and repeat. Expected: exit 3, "raw data purged", and nothing changes.

### A4. Retry strategies (FR-047)

```bash
PRICING_DOWNLOAD_RETRY_STRATEGY=linear_backoff PRICING_DOWNLOAD_RETRY_BASE_DELAY_SECONDS=1 \
  pytest tests/unit/test_retry_policy.py tests/unit/test_aws_pricing_api_async.py -k retry
python -m src.pipeline settings        # shows effective pricing_download_retry_* values
PRICING_DOWNLOAD_RETRY_STRATEGY=bogus python -m src.pipeline settings   # exit 2
```

### A5. Concurrent run refused (US4 AS4, FR-005)

Start `python -m src.pipeline run --regions us-east-1 --snapshot-date 2026-01-12` and, while it is running, start the same command in a second terminal. Expected: the second one exits 0 immediately and logs `refused: run already in progress (run_id=…)`.

### A6. Retention dry run and real run (US5, SC-005)

```bash
python -m src.pipeline retention --dry-run --output plan.json
python -m src.pipeline retention --output result.json
```

Expected:
- The dry run changes nothing.
- `result.json`'s deletions equal `plan.json`'s.
- `verify` still exits 0.

The seeded, more-than-12-month version of this check is `tests/unit/test_retention.py`.

### A7. History upload (US8)

```bash
export TARGET=$PIPELINE_STORAGE_URI
python -m src.pipeline upload-history --snapshot-date 2026-09-28 --source "$DATA_DIRECTORY_ROOT" --dry-run
python -m src.pipeline upload-history --snapshot-date 2026-09-28 --source "$DATA_DIRECTORY_ROOT"
python -m src.pipeline upload-history --snapshot-date 2026-09-28 --source "$DATA_DIRECTORY_ROOT"            # exit 3
python -m src.pipeline upload-history --snapshot-date 2026-09-28 --source "$DATA_DIRECTORY_ROOT" --overwrite
```

Expected:
- The legacy-layout snapshot is published with `origin=backfill`, `raw=null` and status `succeeded`.
- The second call aborts before writing anything.
- `--overwrite` creates revision 2.
- No `raw/` objects are created.

### A8. Container parity (FR-001, FR-036)

```bash
docker build -t pricing-pipeline:local --build-arg GIT_SHA=$(git rev-parse --short HEAD) .
docker run --rm -v "$PWD/.localdata:/data" -e PIPELINE_STORAGE_URI=file:///data \
  -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN \
  pricing-pipeline:local run --regions us-east-2
```

Expected: the same layout as A1, and the manifest's `run.pipeline_version.git_sha` equals the build argument.

### A9. Optional Dagster runner (FR-007)

Run `dagster dev -m src.dagster_app.definitions`, open the Launchpad for `pricing_snapshot`, and set `regions: [us-west-1]`. Expected: the same result as A1 for that region.

---

## Part B — Cloud validation (prod)

### B1. One-time bootstrap (FR-033)

```bash
cd infra/bootstrap
tofu init && tofu apply -var github_repository=<owner>/cloud-pricing-data-retrieval \
  -var state_bucket_name=<unique> -var budget_email=<you>
# then migrate this stack's own state into the new bucket (documented in infra/bootstrap/README.md)
```

In GitHub, create the environment `prod` with a required reviewer, and add these settings:
- **Secrets**: `TF_VAR_alert_email` and `TF_VAR_budget_email`.
- **Variables**: `AWS_REGION`, plus the role ARNs from the bootstrap output `gha_role_arns` as `AWS_ROLE_PLAN_PROD`, `AWS_ROLE_APPLY_PROD` and `AWS_ROLE_RUN_PROD`. They aren't secret.

### B2. Deploy from CI (FR-034, US6 AS3)

1. Open a PR. Expected: tests pass, the image builds, OpenTofu checks pass, and a `tofu plan` for `data` and `pipeline` is posted as a comment.
2. Merge the PR. Expected: `ci.yml` runs on `main`, and **nothing deploys**.
3. Tag the release on `main` and push it: `git tag -a v1.0.0 -m "First cloud release" && git push origin v1.0.0`. Expected: `deploy.yml` checks the tag is on `main`, runs the tests, and waits for approval.
4. Approve the deployment. Expected: image `cloud-pricing-pipeline-prod:v1.0.0` is pushed, and the data and pipeline stacks are applied.
5. Confirm the SNS subscription email. This is the only manual step.
6. Negative check: push a tag like `v0.0.1-test` or a `v*` tag on a side branch. Expected: `deploy.yml` fails in its first job, before any AWS access.

### B3. First run on demand (US1, US6 AS1)

Actions → **run-pipeline** → `mode=run`, leaving the other inputs blank. Expected within about 20 minutes:
- `aws s3 ls s3://<bucket>/aws/manifests/` shows today's date and `latest.json`.
- `python -m src.pipeline verify` with `PIPELINE_STORAGE_URI=s3://<bucket>` exits 0.
- ECS shows no running tasks.
- CloudWatch Logs has the run's log stream.

### B4. Simulated region failure and alert (SC-002)

Run **run-pipeline** with `regions=us-east-1,xx-fake-1`. Expected:
- A `RUN PARTIAL` email arrives within 15 minutes of the task stopping.
- Running `regions=us-east-1` again for the same date gives a higher revision.

With `regions=us-east-1` alone, a run for a new date gives `succeeded` and `latest.json` moves.

### B5. Crash alert

Temporarily set `task_memory=2048` in a throwaway apply (or run with a command override of `python -c "import sys; sys.exit(9)"`). Expected: a `TASK CRASHED` email, and no manifest changes.

### B6. Missed-run alert (SC-003)

Apply with `schedule_enabled=false` and `max_snapshot_age_days=0`, then wait for the next daily watchdog run, or invoke the Lambda by hand with `aws lambda invoke`. Expected: a `MISSED RUN` email. Revert both settings afterwards.

### B7. Retention in the cloud (SC-004, SC-005)

- Run **run-pipeline** with `mode=retention` and `dry_run=true`, and review the plan in the logs.
- Check that the data bucket's lifecycle configuration shows `aws/raw/` expiring after 30 days.
- After 5 or more weekly runs, `raw list` shows no raw data older than 30 days plus at most 2 days of lifecycle lag.

### B8. Destroy and re-create the pipeline stack (SC-008)

```bash
cd infra/pipeline
tofu destroy -var-file=../envs/prod.tfvars -var image_tag=<sha>
tofu apply   -var-file=../envs/prod.tfvars -var image_tag=<sha>
```

Expected:
- The data bucket and its objects are untouched, because it lives in the separate `data` stack with `prevent_destroy`.
- The schedule exists again, and a B3 on-demand run succeeds.
- No console steps are needed. The SNS subscription may need re-confirming, because the topic is re-created.

### B9. Four weeks unattended (SC-001) and cost (SC-007)

- After 4 Mondays, `aws s3 ls s3://<bucket>/aws/manifests/` shows 4 dates, each with status `succeeded`, and no alerts other than optional success summaries.
- In Cost Explorer, filtered by the tag `project=cloud-pricing` and `component=pipeline`, the monthly cost is under $3.

---

## Part C — Off-AWS writer and reader (User Story 9, SC-010)

### C1. One-time CA and client certificates

Follow `infra/data/README.md`:
1. Create the owner CA with `openssl`, and keep its key offline.
2. Issue a client certificate with `CN=home-server` (the writer) and one with `CN=dev-laptop` (a reader).

Put the CA's **public** certificate in `roles_anywhere_ca_bundle_pem`, set `external_writer_subjects=["home-server"]` and `external_reader_subjects=["dev-laptop"]`, and apply the `data` and then `pipeline` stacks through CI.

Expected outputs: the trust anchor ARN, plus the writer and reader role and profile ARNs.

### C2. Pause the cloud schedule (FR-051)

Apply `pipeline` with `schedule_enabled=false`. Expected: the Scheduler schedule shows as disabled, all other resources remain, and no `SCHEDULE FAILED` alert is raised on Monday.

### C3. Home server run to S3 (FR-052, FR-053)

On the home server:
1. Write `~/.pricing/aws-config` with a `credential_process` profile that uses `aws_signing_helper`, the certificate, the key and the writer ARNs (template in `infra/data/README.md`).
2. Run:

```bash
docker run --rm -v ~/.pricing:/creds:ro   -e AWS_CONFIG_FILE=/creds/aws-config -e AWS_PROFILE=pricing-writer   -e PIPELINE_STORAGE_URI=s3://<bucket> -e PIPELINE_HOST_LABEL=home-server   -e ALERT_TOPIC_ARN=<topic-arn>   <account>.dkr.ecr.us-east-1.amazonaws.com/cloud-pricing-pipeline-prod:<sha> run --trigger scheduled
```

3. Schedule the same command weekly with cron or a systemd timer (or use the Dagster schedule).

Expected:
- The same S3 layout as B3, with `run.host = "home-server"`.
- `latest.json` moves.
- The watchdog stays quiet.
- There are no `aws_access_key_id` entries anywhere on the machine.

Negative checks:
- Set the machine's clock wrong by 10 minutes. Expected: exit 2, "clock skew".
- Remove `home-server` from `external_writer_subjects` and apply. Expected: the next run fails to get credentials.
- Start a Fargate on-demand run for the same date while the home run is in progress. Expected: it is refused.

### C4. Read from anywhere (FR-054, FR-056)

On `dev-laptop`, with the reader profile:
- `PIPELINE_STORAGE_URI=s3://<bucket> python -m src.pipeline verify` exits 0.
- `aws s3 cp s3://<bucket>/aws/manifests/latest.json -` works.
- `aws s3 rm s3://<bucket>/aws/manifests/latest.json` fails with `AccessDenied`.

Run the same `verify` from inside AWS with the `cloud-pricing-data-read-<env>` policy (e.g., a one-off ECS task or CloudShell with a role that has it). Expected: identical results.

### C5. Crash alert off AWS (FR-057)

Run the home command with an invalid `PIPELINE_STORAGE_URI=s3://nonexistent-bucket-xyz`. Expected: a non-zero exit, preceded by a `RUN CRASHED` alert (or a `RUN FAILED` alert, if the failure is handled).
