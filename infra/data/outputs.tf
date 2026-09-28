output "data_bucket_name" {
  value = aws_s3_bucket.data.bucket
}

output "data_bucket_arn" {
  value = aws_s3_bucket.data.arn
}

output "data_read_policy_arn" {
  description = "Attach to the web app's role for read-only access (FR-011)."
  value       = aws_iam_policy.data_read.arn
}

output "roles_anywhere_trust_anchor_arn" {
  value = try(aws_rolesanywhere_trust_anchor.owner_ca[0].arn, null)
}

output "writer_role_arn" {
  value = try(aws_iam_role.external_writer[0].arn, null)
}

output "writer_role_name" {
  description = "Read by the pipeline stack to grant alert publishing and image pulls (null if disabled)."
  value       = try(aws_iam_role.external_writer[0].name, null)
}

output "writer_profile_arn" {
  value = try(aws_rolesanywhere_profile.writer[0].arn, null)
}

output "reader_role_arn" {
  value = try(aws_iam_role.external_reader[0].arn, null)
}

output "reader_profile_arn" {
  value = try(aws_rolesanywhere_profile.reader[0].arn, null)
}
