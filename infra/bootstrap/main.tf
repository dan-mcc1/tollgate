# Bootstrap: the few things that must exist before the main stack can run, and that
# must survive every `terraform destroy` of it. Applied by hand, rarely.
#
#   1. S3 bucket holding Terraform state for both stacks
#   2. Route 53 zone for tollgate.danmccabe.dev (its name servers are entered in Porkbun
#      once; recreating the zone would change them)
#   3. GitHub OIDC trust + the role GitHub Actions deploys with (trust only; the main
#      stack attaches permissions, next to the resources they apply to)
#   4. ECR repository. Images are build artifacts like state: they must outlive the stack
#      that runs them, so a destroyed stack can be rebuilt from the last image pushed.
#   5. Secrets Manager entries (containers only; values are set by hand with the CLI, so
#      they never appear in code or Terraform state). A deleted secret's name stays
#      reserved for up to 30 days, so these can't live in a stack that's destroyed often.
#   6. The billing alarm. It belongs here so it keeps watching while the main stack is down:
#      forgetting to destroy is exactly when it's needed.

data "aws_caller_identity" "current" {}

locals {
  account_id   = data.aws_caller_identity.current.account_id
  state_bucket = "tollgate-tfstate-${local.account_id}" # bucket names are global; the account id makes it unique
}

# -----------------------------------------------------------------------------------------
# 1. Terraform state bucket
# -----------------------------------------------------------------------------------------

resource "aws_s3_bucket" "state" {
  bucket = local.state_bucket

  lifecycle {
    prevent_destroy = true # losing state means Terraform forgets everything it manages
  }
}

# Every write keeps the previous version, so a corrupted or bad state can be rolled back.
resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# State can contain sensitive values. It must never be publicly readable.
resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Refuse any request that isn't over HTTPS.
resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

# Old state versions are only useful for a while. Expire them so the bucket stays tiny.
resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}

# -----------------------------------------------------------------------------------------
# 2. DNS zone for the delegated subdomain
# -----------------------------------------------------------------------------------------

resource "aws_route53_zone" "tollgate" {
  name    = var.domain
  comment = "Delegated from Porkbun by NS records on danmccabe.dev"

  lifecycle {
    prevent_destroy = true # a new zone gets new name servers, which means editing Porkbun again
  }
}

# Resolvers cache "this name doesn't exist" for the SOA record's TTL or its last field,
# whichever is smaller. Route 53 defaults both high (900 s and 86400 s), so a lookup made
# while the main stack is destroyed keeps failing for 15 minutes after it's rebuilt. The
# main stack is destroyed and rebuilt routinely, so keep that window to a minute.
resource "aws_route53_record" "soa" {
  zone_id         = aws_route53_zone.tollgate.zone_id
  name            = aws_route53_zone.tollgate.name
  type            = "SOA"
  ttl             = 60
  allow_overwrite = true # every zone is born with an SOA record; take over the existing one

  # primary server, admin contact, serial, refresh, retry, expire, negative-cache TTL
  records = ["${aws_route53_zone.tollgate.primary_name_server}. awsdns-hostmaster.amazon.com. 1 7200 900 1209600 60"]
}

# -----------------------------------------------------------------------------------------
# 3. GitHub Actions -> AWS, with no stored keys
# -----------------------------------------------------------------------------------------

# Tells IAM to trust tokens signed by GitHub's OIDC issuer. One per account.
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

# The role a deploy workflow becomes. The trust policy is the security boundary: only a
# token from this repository, on this branch, for AWS, can assume it.
resource "aws_iam_role" "github_deploy" {
  name                 = "tollgate-github-deploy"
  description          = "Assumed by GitHub Actions on ${var.github_repository}@${var.deploy_branch}"
  max_session_duration = 3600

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          # Exact match: no wildcards, so forks, other branches and pull requests can't deploy.
          "token.actions.githubusercontent.com:sub" = "${var.github_subject_prefix}:ref:refs/heads/${var.deploy_branch}"
        }
      }
    }]
  })
}

# -----------------------------------------------------------------------------------------
# 4. Container registry
# -----------------------------------------------------------------------------------------

resource "aws_ecr_repository" "tollgate" {
  name = "tollgate"

  # Images are tagged with the git commit SHA. Immutable means a tag, once pushed, always
  # points at the same image: "commit abc123 is running" can never quietly become false.
  image_tag_mutability = "IMMUTABLE"

  # Free basic scan for known CVEs in OS packages on every push.
  image_scanning_configuration {
    scan_on_push = true
  }

  lifecycle {
    prevent_destroy = true
  }
}

# Storage is billed per GB. Keep enough history to roll back, drop the rest.
resource "aws_ecr_lifecycle_policy" "tollgate" {
  repository = aws_ecr_repository.tollgate.name
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Delete untagged images (left behind by failed or replaced pushes)"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the 30 most recent images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 30
        }
        action = { type = "expire" }
      },
    ]
  })
}

# -----------------------------------------------------------------------------------------
# 5. Secrets (values set outside Terraform)
# -----------------------------------------------------------------------------------------

resource "aws_secretsmanager_secret" "database_url" {
  name        = "tollgate/database-url"
  description = "Neon connection string, postgresql+asyncpg://...?ssl=require"

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_secretsmanager_secret" "gemini_api_key" {
  name        = "tollgate/gemini-api-key"
  description = "Provider credential. Only the gateway ever reads it."

  lifecycle {
    prevent_destroy = true
  }
}

# Upstash rather than ElastiCache: serverless, free at this volume, and reachable over
# TLS from a public subnet, so it needs no VPC endpoint and no NAT gateway. ElastiCache
# would be about ten dollars a month and several more Terraform resources, for a store
# whose entire contents are rate-limit buckets that rebuild themselves in seconds.
resource "aws_secretsmanager_secret" "redis_url" {
  name        = "tollgate/redis-url"
  description = "Upstash connection string, rediss://default:...@....upstash.io:6379"

  lifecycle {
    prevent_destroy = true
  }
}

# The OTLP authorization header, which is a credential for the telemetry backend. It is a
# secret rather than plain configuration for the same reason the provider key is: anyone
# who can read a task definition can read its environment block.
resource "aws_secretsmanager_secret" "otel_headers" {
  name        = "tollgate/otel-headers"
  description = "OTLP headers for Grafana Cloud, e.g. Authorization=Basic <base64>"

  lifecycle {
    prevent_destroy = true
  }
}

# -----------------------------------------------------------------------------------------
# 6. Billing alarm
# -----------------------------------------------------------------------------------------

# An AWS Budget rather than a CloudWatch billing alarm: no SNS topic to confirm, forecast
# alerts built in, and the first two budgets in an account are free. AWS refreshes the
# numbers a few times a day, so alerts lag spend by hours, not seconds.
resource "aws_budgets_budget" "monthly" {
  name         = "tollgate-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Early warning: half the budget already spent.
  notification {
    notification_type          = "ACTUAL"
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    subscriber_email_addresses = [var.alert_email]
  }

  # On current pace, the month will end over budget. Usually the first sign the main
  # stack was left running.
  notification {
    notification_type          = "FORECASTED"
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    subscriber_email_addresses = [var.alert_email]
  }

  # Over budget.
  notification {
    notification_type          = "ACTUAL"
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    subscriber_email_addresses = [var.alert_email]
  }
}
