# Synthetic estimator input only. Placeholder IDs; do not deploy.
provider "aws" {
  region = "us-east-1"
}

resource "aws_instance" "api" {
  count         = 4
  ami           = "ami-placeholder"
  instance_type = "m6i.xlarge"
  root_block_device {
    volume_type = "gp3"
    volume_size = 50
  }
}

resource "aws_db_instance" "database" {
  identifier              = "synthetic-database"
  engine                  = "postgres"
  instance_class          = "db.m6i.large"
  allocated_storage       = 500
  storage_type            = "gp3"
  multi_az                = true
  username                = "syntheticadmin"
  manage_master_user_password = true
  backup_retention_period = 7
  skip_final_snapshot     = true
}

resource "aws_elasticache_cluster" "cache" {
  cluster_id           = "synthetic-cache"
  engine               = "memcached"
  node_type            = "cache.m6g.large"
  num_cache_nodes      = 2
  parameter_group_name = "default.memcached1.6"
  port                 = 11211
}

resource "aws_lb" "public" {
  name               = "synthetic-alb"
  load_balancer_type = "application"
  subnets            = ["subnet-placeholder-a", "subnet-placeholder-b"]
}

resource "aws_nat_gateway" "egress" {
  allocation_id = "eipalloc-placeholder"
  subnet_id     = "subnet-placeholder-a"
}

resource "aws_s3_bucket" "assets" {
  bucket = "synthetic-estimator-assets-not-for-deployment"
}

resource "aws_cloudwatch_log_group" "application" {
  name              = "/synthetic/application"
  retention_in_days = 30
}

resource "aws_vpc" "network" {
  cidr_block = "10.42.0.0/16"
}

resource "aws_security_group" "application" {
  name   = "synthetic-application"
  vpc_id = aws_vpc.network.id
}
