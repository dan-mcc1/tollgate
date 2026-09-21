# The dashboard and the one alert, applied from code rather than clicked together.
#
# The dashboard's source of truth is dashboards/tollgate.json at the repository root, so
# it can be read in a pull request and diffed like anything else. Terraform only carries
# it; editing the copy in Grafana's UI is drift, and the next apply puts it back.

resource "grafana_folder" "tollgate" {
  title = "Tollgate"
}

resource "grafana_dashboard" "tollgate" {
  folder      = grafana_folder.tollgate.uid
  config_json = file("${path.module}/../../dashboards/tollgate.json")
  overwrite   = true
}

# -----------------------------------------------------------------------------------------
# The alert
# -----------------------------------------------------------------------------------------

# Overhead, not total latency. Total latency is mostly the provider generating tokens,
# which this service cannot control and should not be woken for; overhead is the part it
# is answerable for. An alert on the wrong one of those two fires every time the provider
# has a slow afternoon, and is muted within a week.
resource "grafana_rule_group" "overhead" {
  name             = "tollgate-overhead"
  folder_uid       = grafana_folder.tollgate.uid
  interval_seconds = 60

  rule {
    name      = "Gateway overhead p99 is high"
    condition = "THRESHOLD"
    for       = "${var.alert_window_minutes}m"

    # No data means no traffic, not a broken gateway: the stack is deliberately taken
    # down between work sessions, and paging for that would train everyone to ignore it.
    no_data_state  = "OK"
    exec_err_state = "Error"

    data {
      ref_id         = "QUERY"
      datasource_uid = var.prometheus_datasource_uid
      relative_time_range {
        from = 600
        to   = 0
      }
      model = jsonencode({
        refId         = "QUERY"
        instant       = true
        editorMode    = "code"
        expr          = "histogram_quantile(0.99, sum by (le) (rate(tollgate_overhead_milliseconds_bucket[5m])))"
        intervalMs    = 60000
        maxDataPoints = 43200
      })
    }

    data {
      ref_id         = "THRESHOLD"
      datasource_uid = "__expr__"
      relative_time_range {
        from = 0
        to   = 0
      }
      model = jsonencode({
        refId      = "THRESHOLD"
        type       = "threshold"
        expression = "QUERY"
        conditions = [{
          type      = "query"
          evaluator = { type = "gt", params = [var.overhead_p99_threshold_ms] }
          operator  = { type = "and" }
          reducer   = { type = "last", params = [] }
          query     = { params = ["QUERY"] }
        }]
      })
    }

    annotations = {
      summary = "Tollgate is adding more than ${var.overhead_p99_threshold_ms}ms at the 99th percentile."
      description = join(" ", [
        "This is the gateway's own time, with the provider's excluded, so a slow model",
        "is not the cause. Open the Tollgate dashboard and compare the overhead panel",
        "with requests by outcome: a rise in both usually means the database or Redis,",
        "a rise in overhead alone usually means this service."
      ])
      runbook_url = "https://github.com/dan-mcc1/tollgate#observability"
    }

    labels = {
      service  = "tollgate"
      severity = "warning"
    }
  }
}

resource "grafana_contact_point" "email" {
  name = "tollgate-email"

  email {
    addresses               = [var.alert_email]
    single_email            = true
    subject                 = "{{ .CommonLabels.alertname }}"
    disable_resolve_message = false
  }
}

resource "grafana_notification_policy" "tollgate" {
  group_by      = ["alertname"]
  contact_point = grafana_contact_point.email.name

  policy {
    matcher {
      label = "service"
      match = "="
      value = "tollgate"
    }
    contact_point   = grafana_contact_point.email.name
    group_by        = ["alertname"]
    group_wait      = "30s"
    group_interval  = "5m"
    repeat_interval = "4h"
  }
}
