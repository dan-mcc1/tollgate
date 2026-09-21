terraform {
  required_version = ">= 1.10"

  required_providers {
    grafana = {
      source  = "grafana/grafana"
      version = "~> 3.18"
    }
  }
}

provider "grafana" {
  url = var.grafana_url
  # A Grafana service account token, not a user's password. Passed as a variable rather
  # than read from Secrets Manager, because this stack deliberately has no AWS provider:
  # it manages nothing in AWS and should not need AWS credentials to plan.
  auth = var.grafana_auth
}
