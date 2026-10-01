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

The repo is public, so Actions logs are public. The role ARNs are stored as **secrets** (masked
in logs), and every AWS login uses `mask-aws-account-id: true`, which hides the account ID in all
later log output.

1. Settings → Actions → General:
   - **Fork pull request workflows:** require approval for all external contributors.
   - **Workflow permissions:** read repository contents.
2. Settings → Environments → create **`prod`**:
   - **Required reviewers:** yourself.
   - **Deployment branches and tags:** *Selected branches and tags*, then add branch `main` and tag
     pattern `v*`.
3. Settings → Secrets and variables → Actions:
   - **Secrets:** `TF_VAR_ALERT_EMAIL`, and the three ARNs from `tofu output gha_role_arns` in
     `infra/bootstrap`: `AWS_ROLE_PLAN_PROD`, `AWS_ROLE_APPLY_PROD`, `AWS_ROLE_RUN_PROD`.
   - **Variables:** `AWS_REGION` (`us-east-1`), and `AWS_PLAN_ENABLED` = `true` once the OIDC
     step below is done (it turns on the PR plan comment).
   - **Variables for `run-pipeline.yml`**, from the pipeline stack outputs after the first deploy:
     `PIPELINE_CLUSTER_ARN`, `PIPELINE_TASK_FAMILY`, `PIPELINE_SUBNET_IDS` (comma-separated),
     `PIPELINE_SECURITY_GROUP_ID`, `PIPELINE_LOG_GROUP`.
4. Settings → Rules → Rulesets:
   - **Tags `v*`:** restrict creation, update and deletion to yourself.
   - **`main`:** require a pull request, and block force pushes and deletion. Add the `ci` jobs as
     required status checks once they have run on a PR.

The very first deploy needs the data stack and the ECR repository to exist. Apply the data stack
from your laptop, and create the repository once with
`tofu apply -var-file=../envs/prod.tfvars -var image_tag=initial -target=aws_ecr_repository.pipeline`
in `infra/pipeline`.

### OIDC trust (who may assume which AWS role)

GitHub gives each job a token whose subject (`sub`) says where it runs. This repo was created after
2026-07-15, so the subject uses GitHub's immutable format, with the owner and repo IDs:
`repo:grp3d@5554338/cloud-pricing-data-retrieval@1378571708:...`. The repo's subject template is set
to `["repo", "context", "ref"]`, so every token also names its ref, and the CI roles
(`infra/bootstrap/github_oidc.tf`) accept only:

| Role | Accepted subject |
|---|---|
| `cloud-pricing-gha-plan-prod` | `…:pull_request:ref:refs/pull/*/merge` |
| `cloud-pricing-gha-apply-prod` | `…:environment:prod:ref:refs/tags/v*` or `…:environment:prod:ref:refs/heads/main` |
| `cloud-pricing-gha-run-prod` | `…:ref:refs/heads/main` (both renderings of a plain run) |

`tofu output gha_trust_subjects` in `infra/bootstrap` shows the exact list.

**One-time setup, done together** (CI can't reach AWS between the two steps):

1. Set the repo's subject template. This needs a token that can administer the repo, for example
   a classic personal access token with the `repo` scope, deleted afterwards:

   ```bash
   curl -L -X PUT \
     -H "Accept: application/vnd.github+json" \
     -H "Authorization: Bearer <TOKEN>" \
     https://api.github.com/repos/grp3d/cloud-pricing-data-retrieval/actions/oidc/customization/sub \
     -d '{"use_default": false, "use_immutable_subject": true, "include_claim_keys": ["repo", "context", "ref"]}'

   # check (works without a token on a public repo)
   curl -s https://api.github.com/repos/grp3d/cloud-pricing-data-retrieval/actions/oidc/customization/sub
   ```

2. Apply the bootstrap stack with the repo's numeric IDs (both public):

   ```bash
   cd infra/bootstrap
   tofu apply -var github_owner_id=5554338 -var github_repo_id=1378571708   # plus your usual TF_VAR_* values
   ```

3. After the branch is merged, run Actions → **oidc-subject** once from `main`. Its `sub` line
   must match one of the `run` entries in `tofu output gha_trust_subjects`. The PR plan job and
   the first deploy confirm the `plan` and `apply` entries.

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
