# Off-AWS access with short-lived credentials (US9; FR-053–FR-055, research R22).
#
# Machines outside AWS (a home-server writer, a laptop reader) present an X.509 client
# certificate issued by the owner's own CA and get temporary credentials for one of two
# roles. Nothing here exists until roles_anywhere_ca_bundle_pem is set, and a role only
# exists while at least one certificate CN is allowed to use it. Removing a CN and applying
# revokes that machine; see README.md for CRL-based revocation.

locals {
  roles_anywhere_enabled = var.roles_anywhere_ca_bundle_pem != ""
  writer_enabled         = local.roles_anywhere_enabled && length(var.external_writer_subjects) > 0
  reader_enabled         = local.roles_anywhere_enabled && length(var.external_reader_subjects) > 0
}

resource "aws_rolesanywhere_trust_anchor" "owner_ca" {
  count   = local.roles_anywhere_enabled ? 1 : 0
  name    = "cloud-pricing-trust-anchor-${var.environment}"
  enabled = true

  source {
    source_type = "CERTIFICATE_BUNDLE"
    source_data {
      x509_certificate_data = var.roles_anywhere_ca_bundle_pem
    }
  }
}

data "aws_iam_policy_document" "external_trust" {
  for_each = {
    writer = var.external_writer_subjects
    reader = var.external_reader_subjects
  }

  statement {
    actions = ["sts:AssumeRole", "sts:TagSession", "sts:SetSourceIdentity"]
    principals {
      type        = "Service"
      identifiers = ["rolesanywhere.amazonaws.com"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [try(aws_rolesanywhere_trust_anchor.owner_ca[0].arn, "arn:aws:rolesanywhere:::none")]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:PrincipalTag/x509Subject/CN"
      values   = length(each.value) > 0 ? each.value : ["__none__"]
    }
  }
}

# --- writer: publishes snapshots from outside AWS ------------------------------------------

resource "aws_iam_role" "external_writer" {
  count                = local.writer_enabled ? 1 : 0
  name                 = "cloud-pricing-pipeline-writer-${var.environment}"
  assume_role_policy   = data.aws_iam_policy_document.external_trust["writer"].json
  max_session_duration = max(3600, var.external_session_duration_seconds)
}

data "aws_iam_policy_document" "external_writer" {
  statement {
    sid       = "DataReadWrite"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = [for p in var.pricing_providers : "${aws_s3_bucket.data.arn}/${p}/*"]
  }

  statement {
    sid       = "DataList"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.data.arn]
  }

  statement {
    sid = "PricingApi"
    actions = [
      "pricing:DescribeServices",
      "pricing:GetProducts",
      "pricing:ListPriceLists",
      "pricing:GetPriceListFileUrl",
      "ec2:DescribeRegions",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "external_writer" {
  count  = local.writer_enabled ? 1 : 0
  name   = "cloud-pricing-pipeline-writer-${var.environment}"
  role   = aws_iam_role.external_writer[0].id
  policy = data.aws_iam_policy_document.external_writer.json
}

resource "aws_rolesanywhere_profile" "writer" {
  count            = local.writer_enabled ? 1 : 0
  name             = "cloud-pricing-writer-profile-${var.environment}"
  role_arns        = [aws_iam_role.external_writer[0].arn]
  duration_seconds = var.external_session_duration_seconds
  enabled          = true
}

# --- reader: the same read-only access the web app has inside AWS --------------------------

resource "aws_iam_role" "external_reader" {
  count                = local.reader_enabled ? 1 : 0
  name                 = "cloud-pricing-data-reader-${var.environment}"
  assume_role_policy   = data.aws_iam_policy_document.external_trust["reader"].json
  max_session_duration = max(3600, var.external_session_duration_seconds)
}

resource "aws_iam_role_policy_attachment" "external_reader" {
  count      = local.reader_enabled ? 1 : 0
  role       = aws_iam_role.external_reader[0].name
  policy_arn = aws_iam_policy.data_read.arn
}

resource "aws_rolesanywhere_profile" "reader" {
  count            = local.reader_enabled ? 1 : 0
  name             = "cloud-pricing-reader-profile-${var.environment}"
  role_arns        = [aws_iam_role.external_reader[0].arn]
  duration_seconds = var.external_session_duration_seconds
  enabled          = true
}
