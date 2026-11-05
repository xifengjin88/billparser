# Synthetic estimator input only. Placeholder IDs; do not deploy.
variable "region" {
  type = string
}

variable "instance_type" {
  type = string
}

variable "replicas" {
  type = number
}

provider "aws" {
  region = var.region
}

resource "aws_instance" "application" {
  count         = var.replicas
  ami           = "ami-placeholder"
  instance_type = var.instance_type
}

resource "aws_s3_bucket" "archive" {
  bucket = "synthetic-estimator-archive-not-for-deployment"
}

resource "aws_dynamodb_table" "events" {
  name         = "synthetic-events"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"
  attribute {
    name = "id"
    type = "S"
  }
}
