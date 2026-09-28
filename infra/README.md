# Infrastructure (OpenTofu)

Three root modules with separate state, split by lifecycle (research R13):

| Stack | What | Lifecycle |
|---|---|---|
| `bootstrap/` | State bucket, GitHub OIDC + CI roles, account budget | Once per account, applied from a laptop |
| `data/` | Data bucket (protected), lifecycle rules, read-only policy, off-AWS access | Rarely changed; the bucket survives everything |
| `pipeline/` | VPC, ECR, ECS task, schedule, alerts, watchdog | Freely destroyed and re-created |

Every resource is tagged `project=cloud-pricing`, `component=<stack>` and `environment=<env>`
(`environment=shared` for bootstrap). Named resources follow `cloud-pricing-<component>-<env>`.
Settings are documented in
[`specs/003-pipeline-cloud-deployment/contracts/configuration.md`](../specs/003-pipeline-cloud-deployment/contracts/configuration.md).

## Apply order

1. **bootstrap**: see [`bootstrap/README.md`](bootstrap/README.md). Then set the state bucket
   name in `envs/prod.backend.hcl` (`bucket`) and `envs/prod.tfvars` (`state_bucket_name`). The
   data bucket name is already set in `envs/prod.tfvars` (`data_bucket_name`).
2. **data**:

   ```bash
   cd infra/data
   tofu init -backend-config=../envs/prod.backend.hcl -backend-config="key=prod/data.tfstate"
   tofu apply -var-file=../envs/prod.tfvars
   ```

3. **Push an image** (until CI does this, see below).
4. **pipeline**:

   ```bash
   cd infra/pipeline
   tofu init -backend-config=../envs/prod.backend.hcl -backend-config="key=prod/pipeline.tfstate"
   export TF_VAR_alert_email=<you@example.com>
   tofu apply -var-file=../envs/prod.tfvars -var image_tag=<git-sha>
   ```

`envs/prod.tfvars` holds settings for both stacks, so each stack warns about the variables it
doesn't declare. That's expected.

## Build and push an image by hand

Use this until CI publishes images. The ECR repository is created by the pipeline stack, so for
the very first apply run `tofu apply -target=aws_ecr_repository.pipeline` first.

```bash
REPO=$(cd infra/pipeline && tofu output -raw ecr_repository_url)
SHA=$(git rev-parse --short HEAD)
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin "${REPO%%/*}"
docker buildx build --platform linux/arm64 --push \
  --build-arg GIT_SHA=$SHA --build-arg IMAGE_TAG=$SHA -t "$REPO:$SHA" .
```

## Run the pipeline on demand

```bash
cd infra/pipeline
CLUSTER=$(tofu output -raw ecs_cluster_arn)
FAMILY=$(tofu output -raw task_definition_family)
SUBNETS=$(tofu output -json subnet_ids | tr -d '[]" ')
SG=$(tofu output -raw security_group_id)

aws ecs run-task --cluster "$CLUSTER" --task-definition "$FAMILY" --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=ENABLED}" \
  --overrides '{"containerOverrides":[{"name":"pipeline","command":["run","--trigger","manual","--regions","us-east-1"]}]}'
```

Any command from `contracts/cli.md` works as the override (`run --transform-only …`,
`retention --dry-run`, `raw list`, `verify`, …). Logs are in the CloudWatch log group from the
`log_group_name` output.

## Pause the schedule

Set `schedule_enabled = false` in `envs/prod.tfvars` (or pass `-var schedule_enabled=false`) and
apply the pipeline stack. Nothing else changes (FR-051).

## Alerts

All alerts go to the SNS topic `cloud-pricing-pipeline-alerts-<env>`, which emails
`TF_VAR_alert_email`. **After the first apply, click the confirmation link in the email from
AWS Notifications.** Until you do, no alerts are delivered. This is the only manual step, and it
recurs if the pipeline stack is destroyed and re-created.

| Alert | Source | When |
|---|---|---|
| `RUN FAILED` / `RUN PARTIAL` | container | A region failed, or the snapshot isn't complete |
| `RUN REFUSED` | container | A scheduled run found another run already in progress |
| `RETENTION ERROR` | container | Cleanup failed (the snapshot itself is unaffected) |
| `RUN CRASHED` | container | Unhandled error (best effort, sent before exiting) |
| `RUN SUCCEEDED` | container | Only with `success_summary_enabled = true` |
| `TASK CRASHED` | EventBridge rule | Task stopped with a non-zero exit, OOM, or never started |
| `SCHEDULE FAILED` | CloudWatch alarm | The schedule couldn't launch the task (scheduler DLQ) |
| `MISSED RUN` | watchdog Lambda, daily 14:00 UTC | No succeeded snapshot within `max_snapshot_age_days` (default 8) |
| `WATCHDOG ERROR` | CloudWatch alarm | The watchdog Lambda itself failed |

To test the missed-run alert, apply with `-var schedule_enabled=false -var max_snapshot_age_days=0`
and invoke the watchdog directly:

```bash
aws lambda invoke --function-name cloud-pricing-watchdog-prod /dev/stdout
```

Revert both settings afterwards.


## CI/CD (GitHub Actions)

| Workflow | Trigger | Does |
|---|---|---|
| `ci.yml` | pull request | `pytest` (incl. Dagster), multi-arch image build (no push), `tofu fmt`/`validate`, prod `tofu plan` posted as a PR comment |
| `deploy.yml` | push to `main` | tests, then **after approval**: push the image `cloud-pricing-pipeline-prod:<sha>` and `tofu apply` data then pipeline |
| `run-pipeline.yml` | manual | Starts the pipeline task: `run`, `transform-only` or `retention` (optionally dry run) |

CI authenticates with GitHub OIDC; there are no AWS keys in GitHub.

### One-time GitHub setup

1. Settings → Environments → create **`prod`** and add yourself as a **required reviewer**.
2. Settings → Secrets and variables → Actions:
   - **Secrets**: `TF_VAR_ALERT_EMAIL`.
   - **Variables**: `AWS_REGION` (`us-east-1`), and from the bootstrap output `gha_role_arns`:
     `AWS_ROLE_PLAN_PROD`, `AWS_ROLE_APPLY_PROD`, `AWS_ROLE_RUN_PROD`. They aren't secret.
   - **Variables for `run-pipeline.yml`**, from the pipeline stack outputs:
     `PIPELINE_CLUSTER_ARN`, `PIPELINE_TASK_FAMILY`, `PIPELINE_SUBNET_IDS` (comma-separated),
     `PIPELINE_SECURITY_GROUP_ID`, `PIPELINE_LOG_GROUP`.
3. Commit `infra/envs/prod.backend.hcl` with the real state bucket name.

The very first deploy needs the ECR repository before the image push. Create it once with
`tofu apply -target=aws_ecr_repository.pipeline` in `infra/pipeline`.

## Destroy and re-create the pipeline stack (SC-008)

```bash
cd infra/pipeline
tofu destroy -var-file=../envs/prod.tfvars -var image_tag=<sha>
tofu apply   -var-file=../envs/prod.tfvars -var image_tag=<sha>
```

The data bucket is in the `data` stack with `prevent_destroy`, so it and all snapshots are
untouched. Afterwards, re-confirm the SNS email subscription (the topic is new), and update the
`PIPELINE_*` repo variables if the IDs changed. No console steps are needed.

## Other environments

`envs/dev.tfvars.example` and `envs/qa.tfvars.example` show the parameterization; only `prod` is
provisioned. To add one: copy the example to `<env>.tfvars`, add `<env>.backend.hcl`, add the
environment to the bootstrap stack's `environments` variable, and create a matching GitHub
Environment. Every resource name and tag carries the environment, so nothing collides with prod.

## Cost tracking

Activate the cost allocation tags `project`, `component` and `environment` once, in Billing →
Cost allocation tags. Cost Explorer can then filter to `project=cloud-pricing` and
`component=pipeline` (SC-007: target under $3/month for the pipeline).
