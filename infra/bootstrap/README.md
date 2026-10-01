# bootstrap: one-time account setup

This stack creates the resources that exist once per AWS account and serve every environment
(constitution V "account singletons", tagged `environment=shared`):

- the IaC state bucket, named by the required `state_bucket_name` variable (`state.tf`). Choose a
  globally unique name without your account ID, for example `cloud-pricing-shared-tfstate-<random>`
- the GitHub Actions OIDC provider and per-environment CI roles (`github_oidc.tf`)
- the account budget `cloud-pricing-shared-budget` (`budget.tf`)
- the ECS service-linked role `AWSServiceRoleForECS`, which Fargate tasks need to start
  (`service_linked_roles.tf`)

Apply it from a laptop with admin credentials. It's the only stack that isn't applied from CI.

## Working with this stack

Its state is in `s3://cloud-pricing-shared-tfstate-g08a9i/shared/bootstrap.tfstate`:

```bash
cd infra/bootstrap
tofu init -backend-config=../envs/shared.backend.hcl
tofu plan     # with the TF_VAR_* values below; a routine plan says "No changes"
tofu apply
```

Typical reasons to apply: adding an environment to `environments`, changing the budget, or
changing the CI roles' trust (see [`../README.md`](../README.md#oidc-trust-who-may-assume-which-aws-role)).

## Building a new account

The state bucket is created by this stack, so the very first apply can't store its state there
yet. Only for a brand-new account:

1. In `versions.tf`, comment out `backend "s3" {}`.
2. `tofu init` and `tofu apply` with the values below. The state is local at first.
3. Put the state bucket name into `infra/envs/*.backend.hcl` (`bucket`) and `infra/envs/*.tfvars`
   (`state_bucket_name`).
4. Uncomment `backend "s3" {}`, then move the state into the bucket:
   `tofu init -migrate-state -backend-config=../envs/shared.backend.hcl`. Answer `yes` to copy it.
5. Once `tofu plan` shows no changes, delete the local `terraform.tfstate*` files.

## Values to keep for every later apply

Variables aren't stored in state, so every `plan`/`apply` here needs the same values. Set them once
per shell (names are case-sensitive):

```bash
export TF_VAR_state_bucket_name=cloud-pricing-shared-tfstate-g08a9i
export TF_VAR_github_repository=grp3d/cloud-pricing-data-retrieval
export TF_VAR_github_owner_id=5554338
export TF_VAR_github_repo_id=1378571708
export TF_VAR_budget_email=<you@example.com>
```

Leaving out `github_repository` or `budget_email` doesn't prompt; it plans to **destroy** the CI
roles or the budget. Check that a routine `tofu plan` says "No changes".

The OIDC trust rules and the one-time GitHub setting they depend on are described in
[`../README.md`](../README.md#oidc-trust-who-may-assume-which-aws-role).

