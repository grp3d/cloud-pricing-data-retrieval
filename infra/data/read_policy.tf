# Read-only access for consumers (the web app in AWS, and off-AWS readers via Roles
# Anywhere): manifests and Parquet only (FR-011, FR-054).

data "aws_iam_policy_document" "data_read" {
  statement {
    sid     = "ReadManifestsAndParquet"
    actions = ["s3:GetObject"]
    resources = flatten([
      for p in var.pricing_providers : [
        "${aws_s3_bucket.data.arn}/${p}/manifests/*",
        "${aws_s3_bucket.data.arn}/${p}/parquet/*",
      ]
    ])
  }

  statement {
    sid       = "ListBucket"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.data.arn]
  }
}

resource "aws_iam_policy" "data_read" {
  name        = "cloud-pricing-data-read-${var.environment}"
  description = "Read-only access to published pricing manifests and Parquet files."
  policy      = data.aws_iam_policy_document.data_read.json
}
