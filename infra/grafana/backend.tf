# Its own state, in the bucket the bootstrap stack created.
#
# A third stack rather than a third file in an existing one, because its lifetime differs
# from both: infra/main is destroyed between work sessions to stop the bill, and taking the
# dashboard and its alert down with it would break the published snapshot every time. This
# stack is applied rarely and left alone, like bootstrap, but it touches no AWS at all.
terraform {
  backend "s3" {
    bucket       = "tollgate-tfstate-111311033994"
    key          = "grafana/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}
