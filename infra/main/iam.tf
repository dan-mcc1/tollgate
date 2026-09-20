# Two roles for the task, and the permissions GitHub Actions gets to deploy.
#
#   Execution role: used by ECS itself, BEFORE the app starts. Pulls the image, reads the
#                   secrets to inject as environment variables, creates the log stream.
#   Task role:      used by the app's own code WHILE it runs, for calls to AWS APIs.
#                   Tollgate makes none, so it has no permissions at all.

data "aws_iam_policy_document" "ecs_tasks_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    # Only ECS acting for this account may assume these roles.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

# --- Execution role ------------------------------------------------------------------------

resource "aws_iam_role" "execution" {
  name               = "${var.name}-task-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

# AWS-managed: pull from ECR, write to CloudWatch Logs.
resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Read exactly these three secrets, nothing else in Secrets Manager.
resource "aws_iam_role_policy" "execution_secrets" {
  name = "read-tollgate-secrets"
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "secretsmanager:GetSecretValue"
      Resource = [
        data.aws_secretsmanager_secret.database_url.arn,
        data.aws_secretsmanager_secret.gemini_api_key.arn,
        data.aws_secretsmanager_secret.redis_url.arn,
      ]
    }]
  })
}

# --- Task role -----------------------------------------------------------------------------

resource "aws_iam_role" "task" {
  name               = "${var.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

# --- What the GitHub deploy role may do ------------------------------------------------------
# The role itself (and its trust in GitHub) lives in bootstrap. Its permissions live here,
# next to the resources they name, and disappear with them on destroy.

resource "aws_iam_role_policy" "github_deploy" {
  name = "deploy-tollgate"
  role = data.aws_iam_role.github_deploy.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "EcrLogin"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*" # this action has no resource to scope to
      },
      {
        Sid    = "EcrPushToThisRepository"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:CompleteLayerUpload",
          "ecr:DescribeImages",
          "ecr:DescribeImageScanFindings",
          "ecr:InitiateLayerUpload",
          "ecr:PutImage",
          "ecr:UploadLayerPart",
        ]
        Resource = data.aws_ecr_repository.tollgate.arn
      },
      {
        Sid    = "EcsReadAndRegister"
        Effect = "Allow"
        Action = [
          "ecs:DescribeServices",
          "ecs:DescribeTaskDefinition",
          "ecs:DescribeTasks",
          "ecs:ListTasks",
          "ecs:RegisterTaskDefinition",
        ]
        Resource = "*" # Describe* and RegisterTaskDefinition don't support resource scoping
      },
      {
        Sid      = "EcsDeployThisService"
        Effect   = "Allow"
        Action   = "ecs:UpdateService"
        Resource = aws_ecs_service.app.arn
      },
      {
        Sid      = "EcsRunMigrationTask"
        Effect   = "Allow"
        Action   = "ecs:RunTask"
        Resource = "arn:aws:ecs:${var.region}:${data.aws_caller_identity.current.account_id}:task-definition/${var.name}:*"
        Condition = {
          ArnEquals = { "ecs:cluster" = aws_ecs_cluster.main.arn }
        }
      },
      {
        # Registering a task definition that uses these roles requires permission to hand
        # them to ECS. Scoped to exactly these two roles, and only to ECS.
        Sid      = "PassTaskRoles"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = [aws_iam_role.execution.arn, aws_iam_role.task.arn]
        Condition = {
          StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
        }
      },
      {
        Sid      = "ReadMigrationLogs"
        Effect   = "Allow"
        Action   = ["logs:GetLogEvents", "logs:FilterLogEvents"]
        Resource = "${aws_cloudwatch_log_group.app.arn}:*"
      },
    ]
  })
}
