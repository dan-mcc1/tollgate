terraform {
  required_version = ">= 1.10" # 1.10+ can lock state in S3 without a DynamoDB table

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region

  # Stamped on every resource this stack creates, so the billing console can show
  # what Tollgate costs and which stack each resource belongs to.
  default_tags {
    tags = {
      Project   = "tollgate"
      Stack     = "bootstrap"
      ManagedBy = "terraform"
    }
  }
}
