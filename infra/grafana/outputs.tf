output "dashboard_url" {
  description = "The dashboard. Share a public snapshot of it from the share menu."
  value       = "${var.grafana_url}/d/${grafana_dashboard.tollgate.uid}"
}

output "alert_rule" {
  description = "The one alert, and what it watches."
  value       = "${grafana_rule_group.overhead.name}: overhead p99 > ${var.overhead_p99_threshold_ms}ms for ${var.alert_window_minutes}m"
}
