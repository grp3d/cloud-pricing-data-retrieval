# bootstrap: one-time account setup

This stack creates the resources that exist once per AWS account and serve every environment
(constitution V "account singletons", tagged `environment=shared`):

- the IaC state bucket, named by the required `state_bucket_name` variable (`state.tf`). Choose a
  globally unique name without your account ID, for example `cloud-pricing-shared-tfstate-<random>`
- the GitHub Actions OIDC provider and per-environment CI roles (`github_oidc.tf`)
- the account budget `cloud-pricing-shared-budget` (`budget.tf`)

Apply it from a laptop with admin credentials. It's the only stack that isn't applied from CI.

## First apply (local state)

```bash
cd infra/bootstrap
tofu init
tofu apply \
  -var state_bucket_name=cloud-pricing-shared-tfstate-g08a9i \
  -var github_repository=<owner>/cloud-pricing-data-retrieval \
  -var budget_email=<you@example.com>
```

## Move its state into the bucket it just created

1. Put the state bucket name into `infra/envs/shared.backend.hcl` and
   `infra/envs/prod.backend.hcl` (`bucket = "..."`), and into `state_bucket_name` in
   `infra/envs/prod.tfvars`. The pipeline stack uses it to read the data stack's outputs.
2. In `versions.tf`, uncomment `backend "s3" {}`.
3. Migrate:

```bash
tofu init -migrate-state -backend-config=../envs/shared.backend.hcl
```

4. Delete the local `terraform.tfstate*` files once `tofu plan` shows no changes.

## Later changes

Run `tofu apply` from this directory, for example to add an environment to `environments` or to
change the budget thresholds. Keep the `-var` values from the first apply, or set them as
`TF_VAR_*` environment variables.
