# Off-AWS access is created only when configured (US9; FR-053, FR-055; task T092).
# Runs offline against a mocked AWS provider: `tofu test` in infra/data.

mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  # Computed ARNs must be well-formed, or the provider's own validators reject them.
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock" }
  }
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::123456789012:policy/mock" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::cloud-pricing-data-prod-test01" }
  }
  mock_resource "aws_rolesanywhere_trust_anchor" {
    defaults = { arn = "arn:aws:rolesanywhere:us-east-1:123456789012:trust-anchor/mock" }
  }
}

variables {
  environment      = "prod"
  data_bucket_name = "cloud-pricing-data-prod-test01"
}

run "disabled_by_default" {
  command = plan

  assert {
    condition     = length(aws_rolesanywhere_trust_anchor.owner_ca) == 0
    error_message = "No trust anchor may exist without a CA bundle."
  }
  assert {
    condition     = length(aws_iam_role.external_writer) == 0 && length(aws_iam_role.external_reader) == 0
    error_message = "No external roles may exist without a CA bundle."
  }
  assert {
    condition     = output.writer_role_name == null
    error_message = "writer_role_name must be null when off-AWS access is disabled."
  }
  assert {
    condition     = aws_s3_bucket.data.bucket == "cloud-pricing-data-prod-test01"
    error_message = "The bucket must use the configured data_bucket_name."
  }
}

run "ca_but_no_subjects_creates_no_roles" {
  command = plan

  variables {
    roles_anywhere_ca_bundle_pem = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
  }

  assert {
    condition     = length(aws_rolesanywhere_trust_anchor.owner_ca) == 1
    error_message = "The trust anchor should exist once a CA bundle is set."
  }
  assert {
    condition     = length(aws_iam_role.external_writer) == 0 && length(aws_iam_role.external_reader) == 0
    error_message = "With no allowed CNs, nothing outside AWS may assume a role (FR-055)."
  }
}

run "writer_and_reader_enabled" {
  command = plan

  variables {
    roles_anywhere_ca_bundle_pem = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
    external_writer_subjects     = ["home-server"]
    external_reader_subjects     = ["dev-laptop"]
  }

  assert {
    condition     = aws_iam_role.external_writer[0].name == "cloud-pricing-pipeline-writer-prod"
    error_message = "Writer role name must follow constitution V naming."
  }
  assert {
    condition     = aws_iam_role.external_reader[0].name == "cloud-pricing-data-reader-prod"
    error_message = "Reader role name must follow constitution V naming."
  }
  assert {
    condition     = length(aws_rolesanywhere_profile.writer) == 1 && length(aws_rolesanywhere_profile.reader) == 1
    error_message = "Each enabled role needs its own Roles Anywhere profile."
  }
  assert {
    condition     = output.writer_role_name == "cloud-pricing-pipeline-writer-prod"
    error_message = "The pipeline stack reads writer_role_name."
  }
}

run "session_duration_is_bounded" {
  command = plan

  variables {
    external_session_duration_seconds = 60
  }

  expect_failures = [var.external_session_duration_seconds]
}

run "bucket_name_is_validated" {
  command = plan

  variables {
    data_bucket_name = "Not_A_Valid_Bucket"
  }

  expect_failures = [var.data_bucket_name]
}

run "read_policy_lists_only_published_prefixes" {
  command = plan

  assert {
    condition = (
      length(data.aws_iam_policy_document.data_read.statement[1].condition) == 1 &&
      toset(one(data.aws_iam_policy_document.data_read.statement[1].condition).values) == toset(["aws/manifests/*", "aws/parquet/*"])
    )
    error_message = "Readers may list only manifests/ and parquet/ (not raw files, claims or internals)."
  }
}
