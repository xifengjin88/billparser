# Synthetic estimator input only. Placeholder IDs; do not deploy.
provider "aws" {
  region = "us-west-2"
}

variable "worker_count" {
  type    = number
  default = 6
}

variable "worker_type" {
  type    = string
  default = "c6i.large"
}

locals {
  databases = {
    primary   = { storage_gb = 100 }
    reporting = { storage_gb = 250 }
  }
}

resource "aws_instance" "worker" {
  count         = var.worker_count
  ami           = "ami-placeholder"
  instance_type = var.worker_type
  root_block_device {
    volume_type = "gp3"
    volume_size = 20
  }
}

resource "aws_db_instance" "database" {
  for_each                    = local.databases
  identifier                  = "synthetic-${each.key}"
  engine                      = "postgres"
  instance_class              = "db.t3.medium"
  allocated_storage           = each.value.storage_gb
  storage_type                = "gp3"
  multi_az                    = false
  username                    = "syntheticadmin"
  manage_master_user_password = true
  skip_final_snapshot         = true
}
