# CI role trust matches GitHub's token subjects (research R14; the OIDC hardening).
# Runs offline against a mocked AWS provider: `tofu test` in infra/bootstrap.

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  mock_resource "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::cloud-pricing-shared-tfstate-test01" }
  }
}

variables {
  state_bucket_name = "cloud-pricing-shared-tfstate-test01"
  github_repository = "grp3d/cloud-pricing-data-retrieval"
  github_owner_id   = "5554338"
  github_repo_id    = "1378571708"
}

run "immutable_subjects" {
  command = plan

  assert {
    condition = output.gha_trust_subjects["prod"].apply == [
      "repo:grp3d@5554338/cloud-pricing-data-retrieval@1378571708:environment:prod:ref:refs/tags/v*",
      "repo:grp3d@5554338/cloud-pricing-data-retrieval@1378571708:environment:prod:ref:refs/heads/main",
    ]
    error_message = "apply must require the prod environment AND a release tag or main."
  }
  assert {
    condition     = output.gha_trust_subjects["prod"].plan == ["repo:grp3d@5554338/cloud-pricing-data-retrieval@1378571708:pull_request:ref:refs/pull/*/merge"]
    error_message = "plan must only accept pull requests."
  }
  assert {
    condition = alltrue([
      for s in output.gha_trust_subjects["prod"].run : endswith(s, ":ref:refs/heads/main") && !strcontains(s, "*")
    ])
    error_message = "run must only accept main, without wildcards."
  }
  assert {
    condition     = aws_iam_role.gha_apply["prod"].name == "cloud-pricing-gha-apply-prod"
    error_message = "Role names follow constitution V."
  }
}

run "legacy_subject_format" {
  command = plan

  variables {
    github_immutable_subject = false
    github_owner_id          = null
    github_repo_id           = null
  }

  assert {
    condition     = startswith(output.gha_trust_subjects["prod"].plan[0], "repo:grp3d/cloud-pricing-data-retrieval:pull_request")
    error_message = "The legacy format has no IDs in the repo segment."
  }
}

run "immutable_format_requires_ids" {
  command = plan

  variables {
    github_repo_id = null
  }

  expect_failures = [aws_iam_openid_connect_provider.github]
}

# Feature 004 (T039): the run role may start every provider's task and pass the GCP
# execution role, which is the only role that may read the GCP key.
run "run_role_covers_every_provider" {
  command = plan

  assert {
    condition = toset(data.aws_iam_policy_document.gha_run["prod"].statement[0].resources) == toset([
      "arn:aws:ecs:*:123456789012:task-definition/cloud-pricing-pipeline-prod:*",
      "arn:aws:ecs:*:123456789012:task-definition/cloud-pricing-pipeline-azure-prod:*",
      "arn:aws:ecs:*:123456789012:task-definition/cloud-pricing-pipeline-gcp-prod:*",
    ])
    error_message = "run-pipeline must be able to start the aws, azure and gcp tasks."
  }
  assert {
    condition     = contains(data.aws_iam_policy_document.gha_run["prod"].statement[2].resources, "arn:aws:iam::123456789012:role/cloud-pricing-task-execution-gcp-prod")
    error_message = "run-pipeline must be able to pass the GCP execution role."
  }
}
