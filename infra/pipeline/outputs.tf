output "ecs_cluster_arn" {
  value = aws_ecs_cluster.pipeline.arn
}

output "task_definition_family" {
  description = "The AWS provider's family (unchanged name; run-pipeline derives the others from it)."
  value       = local.task_families["aws"]
}

output "task_definition_families" {
  value = local.task_families
}

output "subnet_ids" {
  value = aws_subnet.public[*].id
}

output "security_group_id" {
  value = aws_security_group.task.id
}

output "ecr_repository_url" {
  value = aws_ecr_repository.pipeline.repository_url
}

output "log_group_name" {
  value = aws_cloudwatch_log_group.pipeline.name
}
