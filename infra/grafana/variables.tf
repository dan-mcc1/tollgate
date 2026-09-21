variable "grafana_url" {
  description = "Grafana Cloud stack URL, e.g. https://yourname.grafana.net"
  type        = string
}

variable "grafana_auth" {
  description = "Service account token with dashboard and alert-rule write access."
  type        = string
  sensitive   = true
}

variable "prometheus_datasource_uid" {
  description = "UID of the Prometheus data source the OTLP metrics land in."
  type        = string
}

variable "alert_email" {
  description = "Where the overhead alert goes."
  type        = string
}

variable "overhead_p99_threshold_ms" {
  description = "Gateway overhead at the 99th percentile that counts as a problem."
  type        = number
  # Measured overhead is about 14 ms at the 99th percentile, so this is roughly seven
  # times normal: high enough that ordinary variation never reaches it, low enough that
  # it fires well before a caller would describe the gateway as broken.
  default = 100
}

variable "alert_window_minutes" {
  description = "How long overhead must stay above the threshold before anyone is told."
  type        = number
  default     = 5
}
