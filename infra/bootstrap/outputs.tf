output "state_bucket" {
  description = "Put this in the backend block of both stacks."
  value       = aws_s3_bucket.state.bucket
}

output "zone_id" {
  value = aws_route53_zone.tollgate.zone_id
}

output "name_servers" {
  description = "Add one NS record per entry in Porkbun, host 'tollgate'."
  value       = aws_route53_zone.tollgate.name_servers
}

output "ecr_repository_url" {
  description = "Image name to tag and push, e.g. <url>:<git sha>."
  value       = aws_ecr_repository.tollgate.repository_url
}

output "secret_names" {
  description = "Set each value once with `aws secretsmanager put-secret-value`."
  value = [
    aws_secretsmanager_secret.database_url.name,
    aws_secretsmanager_secret.gemini_api_key.name,
    aws_secretsmanager_secret.redis_url.name,
  ]
}

output "github_deploy_role_arn" {
  description = "Used by the deploy workflow's configure-aws-credentials step."
  value       = aws_iam_role.github_deploy.arn
}
