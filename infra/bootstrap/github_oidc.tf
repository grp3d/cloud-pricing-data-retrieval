# GitHub Actions → AWS without long-lived keys (FR-033, research R13/R14).
# The OIDC provider is an account singleton; the CI roles are per environment.

locals {
  github_enabled = var.github_repository != null
  state_arn      = "arn:aws:s3:::${local.state_bucket_name}"
  oidc_host      = "token.actions.githubusercontent.com"
  # Loop over environment names, not role objects: referencing a whole aws_iam_role pulls in
  # its deprecated managed_policy_arns attribute and triggers provider warnings.
  gha_envs = local.github_enabled ? toset(var.environments) : toset([])
}

resource "aws_iam_openid_connect_provider" "github" {
  count          = local.github_enabled ? 1 : 0
  url            = "https://${local.oidc_host}"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "gha_trust" {
  for_each = local.github_enabled ? {
    for pair in flatten([
      for env in var.environments : [
        { key = "plan-${env}", sub = "repo:${var.github_repository}:pull_request" },
        { key = "apply-${env}", sub = "repo:${var.github_repository}:environment:${env}" },
        { key = "run-${env}", sub = "repo:${var.github_repository}:ref:refs/heads/main" },
      ]
    ]) : pair.key => pair.sub
  } : {}

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github[0].arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = [each.value]
    }
  }
}

# --- plan: read-only, for pull requests ------------------------------------------------

resource "aws_iam_role" "gha_plan" {
  for_each           = local.github_enabled ? toset(var.environments) : toset([])
  name               = "cloud-pricing-gha-plan-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.gha_trust["plan-${each.key}"].json
}

resource "aws_iam_role_policy_attachment" "gha_plan_readonly" {
  for_each   = local.gha_envs
  role       = aws_iam_role.gha_plan[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

data "aws_iam_policy_document" "gha_plan_state" {
  for_each = local.github_enabled ? toset(var.environments) : toset([])

  statement {
    sid       = "StateLockFiles"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${local.state_arn}/${each.key}/*.tflock"]
  }
}

resource "aws_iam_role_policy" "gha_plan_state" {
  for_each = local.gha_envs
  name     = "cloud-pricing-gha-plan-state-${each.key}"
  role     = aws_iam_role.gha_plan[each.key].name
  policy   = data.aws_iam_policy_document.gha_plan_state[each.key].json
}

# --- apply: manages that environment's data + pipeline stacks ---------------------------

data "aws_iam_policy_document" "gha_apply" {
  for_each = local.github_enabled ? toset(var.environments) : toset([])

  statement {
    sid       = "State"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${local.state_arn}/${each.key}/*"]
  }

  statement {
    sid       = "StateList"
    actions   = ["s3:ListBucket"]
    resources = [local.state_arn]
  }

  # Resources named cloud-pricing-*-<env> (constitution V naming rule).
  statement {
    sid = "ManageEnvironmentResources"
    actions = [
      "s3:*", "ecr:*", "ecs:*", "scheduler:*", "sns:*", "sqs:*", "lambda:*", "events:*",
      "cloudwatch:*", "logs:*", "rolesanywhere:*",
    ]
    resources = [
      "arn:aws:s3:::cloud-pricing-data-${each.key}-*",
      "arn:aws:s3:::cloud-pricing-data-${each.key}-*/*",
      "arn:aws:ecr:*:${local.account_id}:repository/cloud-pricing-*-${each.key}",
      "arn:aws:ecs:*:${local.account_id}:cluster/cloud-pricing-${each.key}",
      "arn:aws:ecs:*:${local.account_id}:task-definition/cloud-pricing-*-${each.key}:*",
      "arn:aws:scheduler:*:${local.account_id}:schedule/*/cloud-pricing-*-${each.key}",
      "arn:aws:sns:*:${local.account_id}:cloud-pricing-*-${each.key}",
      "arn:aws:sqs:*:${local.account_id}:cloud-pricing-*-${each.key}",
      "arn:aws:lambda:*:${local.account_id}:function:cloud-pricing-*-${each.key}",
      "arn:aws:events:*:${local.account_id}:rule/cloud-pricing-*-${each.key}",
      "arn:aws:cloudwatch:*:${local.account_id}:alarm:cloud-pricing-*-${each.key}",
      "arn:aws:logs:*:${local.account_id}:log-group:/cloud-pricing/*-${each.key}*",
      "arn:aws:logs:*:${local.account_id}:log-group:/aws/lambda/cloud-pricing-*-${each.key}*",
      "arn:aws:rolesanywhere:*:${local.account_id}:*",
    ]
  }

  statement {
    sid = "ManageEnvironmentIam"
    actions = [
      "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:UpdateRole", "iam:TagRole", "iam:UntagRole",
      "iam:UpdateAssumeRolePolicy", "iam:PutRolePolicy", "iam:GetRolePolicy", "iam:DeleteRolePolicy",
      "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:AttachRolePolicy", "iam:DetachRolePolicy",
      "iam:ListInstanceProfilesForRole", "iam:PassRole",
      "iam:CreatePolicy", "iam:DeletePolicy", "iam:GetPolicy", "iam:GetPolicyVersion", "iam:ListPolicyVersions",
      "iam:CreatePolicyVersion", "iam:DeletePolicyVersion", "iam:TagPolicy", "iam:UntagPolicy",
    ]
    resources = [
      "arn:aws:iam::${local.account_id}:role/cloud-pricing-*-${each.key}",
      "arn:aws:iam::${local.account_id}:policy/cloud-pricing-*-${each.key}",
    ]
  }

  # VPC resources can't be scoped by name, so the environment tag carries the scope:
  # creates must carry the tag, and changes are limited to resources that already have it.
  statement {
    sid       = "CreateTaggedNetwork"
    actions   = ["ec2:Create*"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/environment"
      values   = [each.key]
    }
  }

  statement {
    sid       = "ManageTaggedNetwork"
    actions   = ["ec2:*"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/environment"
      values   = [each.key]
    }
  }

  statement {
    sid       = "TagOnCreate"
    actions   = ["ec2:CreateTags"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:CreateAction"
      values = [
        "CreateVpc", "CreateSubnet", "CreateInternetGateway", "CreateRouteTable",
        "CreateSecurityGroup", "CreateVpcEndpoint",
      ]
    }
  }

  statement {
    sid = "ReadOnlyDiscovery"
    actions = [
      "ec2:Describe*", "iam:Get*", "iam:List*", "sts:GetCallerIdentity", "ecr:GetAuthorizationToken",
      "s3:ListAllMyBuckets", "s3:GetBucketLocation",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role" "gha_apply" {
  for_each           = local.github_enabled ? toset(var.environments) : toset([])
  name               = "cloud-pricing-gha-apply-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.gha_trust["apply-${each.key}"].json
}

resource "aws_iam_role_policy" "gha_apply" {
  for_each = local.gha_envs
  name     = "cloud-pricing-gha-apply-${each.key}"
  role     = aws_iam_role.gha_apply[each.key].name
  policy   = data.aws_iam_policy_document.gha_apply[each.key].json
}

# --- run: start on-demand pipeline tasks ----------------------------------------------

data "aws_iam_policy_document" "gha_run" {
  for_each = local.github_enabled ? toset(var.environments) : toset([])

  statement {
    actions   = ["ecs:RunTask"]
    resources = ["arn:aws:ecs:*:${local.account_id}:task-definition/cloud-pricing-pipeline-${each.key}:*"]
  }

  statement {
    actions   = ["ecs:DescribeTasks"]
    resources = ["*"]
  }

  statement {
    actions = ["iam:PassRole"]
    resources = [
      "arn:aws:iam::${local.account_id}:role/cloud-pricing-task-${each.key}",
      "arn:aws:iam::${local.account_id}:role/cloud-pricing-task-execution-${each.key}",
    ]
  }
}

resource "aws_iam_role" "gha_run" {
  for_each           = local.github_enabled ? toset(var.environments) : toset([])
  name               = "cloud-pricing-gha-run-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.gha_trust["run-${each.key}"].json
}

resource "aws_iam_role_policy" "gha_run" {
  for_each = local.gha_envs
  name     = "cloud-pricing-gha-run-${each.key}"
  role     = aws_iam_role.gha_run[each.key].name
  policy   = data.aws_iam_policy_document.gha_run[each.key].json
}
