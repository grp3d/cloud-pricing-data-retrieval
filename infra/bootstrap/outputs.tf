output "state_bucket_name" {
  description = "Put this in infra/envs/<env>.backend.hcl as `bucket`."
  value       = aws_s3_bucket.state.bucket
}

output "gha_role_arns" {
  description = "Per-environment CI roles. Set as GitHub repo variables AWS_ROLE_{PLAN,APPLY,RUN}_<ENV>."
  value = {
    for env in var.environments : env => {
      plan  = try(aws_iam_role.gha_plan[env].arn, null)
      apply = try(aws_iam_role.gha_apply[env].arn, null)
      run   = try(aws_iam_role.gha_run[env].arn, null)
    }
  }
}
