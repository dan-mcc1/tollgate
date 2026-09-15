variable "region" {
  description = "AWS region for everything Tollgate runs in."
  type        = string
  default     = "us-east-1"
}

variable "github_repository" {
  description = "owner/name of the only GitHub repository allowed to deploy (used in descriptions)."
  type        = string
  default     = "dan-mcc1/tollgate"
}

variable "github_subject_prefix" {
  description = <<-EOT
    The start of the `sub` claim in this repository's GitHub OIDC tokens. The repository uses
    immutable subjects, which include the owner's and repository's numeric IDs, so a renamed
    or re-created repository with the same name can never match. Look it up with:
      gh api repos/dan-mcc1/tollgate/actions/oidc/customization/sub --jq .sub_claim_prefix
  EOT
  type        = string
  default     = "repo:dan-mcc1@117699367/tollgate@1370587408"
}

variable "alert_email" {
  description = "Where budget alerts go. Set in terraform.tfvars (gitignored), not in code."
  type        = string
}

variable "monthly_budget_usd" {
  description = "Monthly AWS spend at which alerts fire."
  type        = number
  default     = 25
}

variable "deploy_branch" {
  description = "The only branch whose workflows may assume the deploy role."
  type        = string
  default     = "main"
}

variable "domain" {
  description = "Subdomain delegated from Porkbun to Route 53."
  type        = string
  default     = "tollgate.danmccabe.dev"
}
