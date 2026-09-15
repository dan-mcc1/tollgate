# State lives in the bucket this stack created. It started as a local file (the bucket
# didn't exist yet) and was moved here with `terraform init -migrate-state`.
#
# Backend blocks can't use variables, so the bucket name is written out in full.
terraform {
  backend "s3" {
    bucket       = "tollgate-tfstate-111311033994"
    key          = "bootstrap/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true # S3-native locking: two applies can't run at once
  }
}
