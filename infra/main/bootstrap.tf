# Things created by infra/bootstrap, looked up by name. Looking them up (instead of reading
# bootstrap's state file) keeps the two stacks independent: either can be planned alone.

data "aws_caller_identity" "current" {}

data "aws_route53_zone" "tollgate" {
  name = var.domain
}

data "aws_ecr_repository" "tollgate" {
  name = var.name
}

data "aws_ecr_image" "initial" {
  repository_name = data.aws_ecr_repository.tollgate.name
  image_tag       = var.image_tag
  most_recent     = var.image_tag == null ? true : null
}

data "aws_secretsmanager_secret" "database_url" {
  name = "${var.name}/database-url"
}

data "aws_secretsmanager_secret" "gemini_api_key" {
  name = "${var.name}/gemini-api-key"
}

data "aws_secretsmanager_secret" "redis_url" {
  name = "${var.name}/redis-url"
}

data "aws_secretsmanager_secret" "otel_headers" {
  name = "${var.name}/otel-headers"
}

data "aws_iam_role" "github_deploy" {
  name = "${var.name}-github-deploy"
}
