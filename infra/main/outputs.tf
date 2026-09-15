output "url" {
  value = "https://${var.domain}"
}

output "alb_dns_name" {
  description = "Direct load balancer address, useful if DNS is being difficult."
  value       = aws_lb.main.dns_name
}

output "cluster" {
  value = aws_ecs_cluster.main.name
}

output "service" {
  value = aws_ecs_service.app.name
}

output "task_family" {
  value = aws_ecs_task_definition.app.family
}

output "log_group" {
  value = aws_cloudwatch_log_group.app.name
}

output "initial_image" {
  value = local.initial_image
}
