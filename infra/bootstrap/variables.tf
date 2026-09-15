variable "region" {
  description = "AWS region for everything Tollgate runs in."
  type        = string
  default     = "us-east-1"
}

variable "github_repository" {
  description = "owner/name of the only GitHub repository allowed to deploy."
  type        = string
  default     = "dan-mcc1/tollgate"
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
