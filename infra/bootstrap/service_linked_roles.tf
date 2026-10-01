# AWS-managed roles that services need in order to act in this account. Account singletons
# (constitution V), tagged environment=shared.
#
# ECS needs AWSServiceRoleForECS to set up a Fargate task's networking. ECS creates it on
# the first CreateCluster only if the caller may call iam:CreateServiceLinkedRole, which the
# CI apply role may not, so it is created here. In an account where it already exists:
#   tofu import aws_iam_service_linked_role.ecs arn:aws:iam::<account-id>:role/aws-service-role/ecs.amazonaws.com/AWSServiceRoleForECS

resource "aws_iam_service_linked_role" "ecs" {
  aws_service_name = "ecs.amazonaws.com"
}
