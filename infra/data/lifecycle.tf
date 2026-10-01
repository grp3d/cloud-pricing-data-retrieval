# Lifecycle rules for the data bucket. S3 allows one lifecycle configuration per bucket,
# so every rule lives here (FR-020, R15).

resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  # Safety net: deleted or overwritten objects stay recoverable for a few days.
  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_retention_days
    }
    expiration {
      expired_object_delete_marker = true
    }
  }

  # Raw pricing files expire after raw_retention_days, one rule per provider (FR-020).
  dynamic "rule" {
    for_each = toset(var.pricing_providers)
    content {
      id     = "expire-raw-${rule.value}"
      status = "Enabled"
      filter {
        prefix = "${rule.value}/raw/"
      }
      expiration {
        days = var.raw_retention_days
      }
      abort_incomplete_multipart_upload {
        days_after_initiation = 1
      }
    }
  }

  depends_on = [aws_s3_bucket_versioning.data]
}
