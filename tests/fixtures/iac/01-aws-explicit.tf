# Synthetic estimator input only. Placeholder IDs; do not deploy.
provider "aws" {
  region = "us-east-1"
}

resource "aws_instance" "web" {
  count         = 3
  ami           = "ami-placeholder"
  instance_type = "m5.large"
  root_block_device {
    volume_type = "gp3"
    volume_size = 30
  }
}

resource "aws_ebs_volume" "data" {
  count             = 2
  availability_zone = "us-east-1a"
  type              = "gp3"
  size              = 200
}
