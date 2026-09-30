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
   tofu apply -var-file=../envs/prod.tfvars -var image_tag=<release tag, e.g. v1.0.0>
   ```

`envs/prod.tfvars` holds settings for both stacks, so each stack warns about the variables it
doesn't declare. That's expected.

## Build and push an image by hand

Normally `deploy.yml` builds and pushes images for release tags. This manual path is a fallback,
for example if GitHub Actions is unavailable. Build from the tagged commit and use the same
version tag CI would use. The ECR repository is created by the pipeline stack, so for the very
first apply run `tofu apply -target=aws_ecr_repository.pipeline` first.

```bash
TAG=v1.0.0
git checkout "$TAG"
REPO=$(cd infra/pipeline && tofu output -raw ecr_repository_url)
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin "${REPO%%/*}"
docker buildx build --platform linux/arm64,linux/amd64 --push \
  --build-arg GIT_SHA=$(git rev-parse HEAD) --build-arg IMAGE_TAG=$TAG -t "$REPO:$TAG" .
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

Prod is deployed only from **release tags** on `main`. Merging to `main` never deploys.

| Workflow | Trigger | Does |
|---|---|---|
| `ci.yml` | pull request, push to `main` | `pytest` (incl. Dagster), multi-arch image build (no push), `tofu fmt`/`validate`/`test`. On pull requests, also a prod `tofu plan` posted as a comment |
| `deploy.yml` | push of a `vX.Y.Z` tag, or manual (from `main`, naming an existing tag) | checks the tag is on `main`, runs the tests, then **after approval**: pushes the image `cloud-pricing-pipeline-prod:vX.Y.Z` (reused if it already exists) and applies data, then pipeline |
| `run-pipeline.yml` | manual | Starts the pipeline task: `run`, `transform-only` or `retention` (optionally dry run) |

CI authenticates with GitHub OIDC; there are no AWS keys in GitHub.

### Releasing to prod

1. Merge PRs into `main` as usual (`ci.yml` runs on the PR and again on `main`).
2. When you want to release, tag the `main` commit and push the tag:

   ```bash
   git checkout main && git pull
   git tag -a v1.0.0 -m "First cloud release"
   git push origin v1.0.0
   ```

3. In Actions → **deploy**, open the run and approve it (**Review deployments → prod → Approve**).
   GitHub emails you when it's waiting.

Details:
- **Tag format:** `vMAJOR.MINOR.PATCH`. Anything else, or a tag whose commit isn't on `main`, fails
  before any AWS access.
- **Every release needs a new version.** Image tags in ECR can't be overwritten, so a fix-up of
  `v1.0.0` is `v1.0.1`.
- **Redeploy or roll back:** Actions → deploy → **Run workflow** (on branch `main`) with an existing
  tag, e.g. `v0.9.0`. Its image is reused, and the stacks are applied with that version.
- **Push tags from the command line.** Tags created through the GitHub Releases web page may not
  start the `push: tags` trigger. Use a manual redeploy if that happens.

### One-time GitHub setup

1. Settings → Environments → create **`prod`**:
   - **Required reviewers:** yourself.
   - **Deployment branches and tags:** *Selected branches and tags*, then add branch `main` and tag
     pattern `v*`. Jobs from any other ref can't use `prod`, and so can't get the AWS apply role.
2. Settings → Rules → Rulesets:
   - **`main`:** require a pull request and passing status checks (the `ci` jobs), and block force
     pushes and deletion.
   - **Tags `v*`:** restrict creation, update and deletion to yourself.
3. Settings → Secrets and variables → Actions:
   - **Secrets**: `TF_VAR_ALERT_EMAIL`.
   - **Variables**: `AWS_REGION` (`us-east-1`), and from the bootstrap output `gha_role_arns`:
     `AWS_ROLE_PLAN_PROD`, `AWS_ROLE_APPLY_PROD`, `AWS_ROLE_RUN_PROD`. They aren't secret.
   - **Variables for `run-pipeline.yml`**, from the pipeline stack outputs:
     `PIPELINE_CLUSTER_ARN`, `PIPELINE_TASK_FAMILY`, `PIPELINE_SUBNET_IDS` (comma-separated),
     `PIPELINE_SECURITY_GROUP_ID`, `PIPELINE_LOG_GROUP`.

The very first deploy needs the data stack and the ECR repository to exist. Apply the data stack
from your laptop, and create the repository once with
`tofu apply -var-file=../envs/prod.tfvars -var image_tag=initial -target=aws_ecr_repository.pipeline`
in `infra/pipeline`.

### Deferred hardening

The AWS apply role trusts any job running in the GitHub `prod` environment. The environment's
allowed refs (`main`, `v*`) and the rulesets above are what restrict that to releases. For a second
check on the AWS side, customize the repository's OIDC subject claim to include the ref and require
it in the CI roles' trust policies (bootstrap stack). This changes the token identity for every
workflow, so all three roles must be updated together. Recommended once the repo gains
collaborators, becomes public, or gets more environments.

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
