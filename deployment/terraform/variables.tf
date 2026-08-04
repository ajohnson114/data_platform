variable "project_name" {
  description = "Prefix for all AWS resources"
  type        = string
  default     = "data-platform"
}

variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.20.0.0/16"
}

variable "eks_version" {
  description = "EKS Kubernetes version"
  type        = string
  default     = "1.31"
}

variable "node_instance_type" {
  description = "EC2 instance type for the EKS worker nodes"
  type        = string
  default     = "t3.large"
}

variable "node_desired_count" {
  description = "Desired number of EKS worker nodes"
  type        = number
  default     = 2
}

variable "db_instance_class" {
  description = "RDS instance class for the Dagster/platform Postgres"
  type        = string
  default     = "db.t3.micro"
}

# No kafka variables. Kafka runs in-cluster as a StatefulSet, so its version is
# the container image tag and its sizing is the pod's resource block — both live
# in k8s/07-kafka.yaml, which is where you change them.
