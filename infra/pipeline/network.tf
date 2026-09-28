# A small VPC with public subnets and no NAT gateway (research R3): the task needs only
# outbound HTTPS, and a NAT gateway (~$32/month) would exceed the whole budget.

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "pipeline" {
  cidr_block           = "10.42.0.0/24"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "cloud-pricing-vpc-${local.env}" }
}

resource "aws_internet_gateway" "pipeline" {
  vpc_id = aws_vpc.pipeline.id
  tags   = { Name = "cloud-pricing-igw-${local.env}" }
}

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.pipeline.id
  cidr_block              = cidrsubnet(aws_vpc.pipeline.cidr_block, 1, count.index)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = false
  tags                    = { Name = "cloud-pricing-public-${count.index}-${local.env}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.pipeline.id
  tags   = { Name = "cloud-pricing-rt-public-${local.env}" }
}

resource "aws_route" "internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.pipeline.id
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# Free gateway endpoint: S3 traffic stays on the AWS network.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.pipeline.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id]
  tags              = { Name = "cloud-pricing-s3-endpoint-${local.env}" }
}

resource "aws_security_group" "task" {
  name        = "cloud-pricing-task-${local.env}"
  description = "Pipeline task: outbound only, no inbound"
  vpc_id      = aws_vpc.pipeline.id
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.task.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
  description       = "All outbound (pricing API, price-list files, ECR, SNS, logs)"
}
