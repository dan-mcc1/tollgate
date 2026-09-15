terraform {
  backend "s3" {
    bucket       = "tollgate-tfstate-111311033994"
    key          = "main/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}
