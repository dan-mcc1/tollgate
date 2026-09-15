variable "region" {
  type    = string
  default = "us-east-1"
}

variable "name" {
  description = "Prefix for resource names. Also the ECR repository and secret prefix from bootstrap."
  type        = string
  default     = "tollgate"
}

variable "domain" {
  description = "Public hostname. Its Route 53 zone is created by bootstrap."
  type        = string
  default     = "tollgate.danmccabe.dev"
}

variable "image_tag" {
  description = "Image tag to run on first create. Null means the most recently pushed image. After creation the deploy pipeline owns which image runs."
  type        = string
  default     = null
}

variable "upstream_base_url" {
  type    = string
  default = "https://generativelanguage.googleapis.com"
}

variable "gemini_model" {
  type    = string
  default = "gemini-3.7-flash"
}

variable "cpu" {
  description = "Fargate task CPU units (256 = 0.25 vCPU)."
  type        = number
  default     = 256
}

variable "memory" {
  description = "Fargate task memory in MiB."
  type        = number
  default     = 512
}

variable "desired_count" {
  type    = number
  default = 1
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "log_retention_days" {
  type    = number
  default = 14
}
