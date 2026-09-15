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

output "github_deploy_role_arn" {
  description = "Used by the deploy workflow's configure-aws-credentials step."
  value       = aws_iam_role.github_deploy.arn
}
