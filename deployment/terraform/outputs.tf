# Output names are consumed by the render script — do not rename.

output "aws_region" {
  value = var.aws_region
}

output "cluster_name" {
  value = aws_eks_cluster.this.name
}

output "rds_host" {
  value = aws_db_instance.this.address
}

output "rds_password" {
  value     = random_password.db.result
  sensitive = true
}

# No kafka bootstrap output. The broker is an in-cluster StatefulSet
# (k8s/07-kafka.yaml), so its address is a fixed piece of cluster DNS —
# `kafka:9092` — not something terraform discovers. Adding it back as a
# hardcoded output would be a value the render script substitutes into a
# manifest that could just as well have written it literally.

# Carries both the Dagster compute logs (compute-logs/) and the S3 IO manager
# intermediates (dagster-io/) — one bucket, two prefixes.
output "compute_logs_bucket" {
  value = aws_s3_bucket.platform.bucket
}

# IRSA role assumed by the `dagster` service account in the app namespace.
output "dagster_role_arn" {
  value = aws_iam_role.dagster.arn
}

# IRSA role assumed by the `clickhouse` service account. Read-only on landing/,
# so the warehouse can open landed Parquet with the s3() table function.
output "clickhouse_role_arn" {
  value = aws_iam_role.clickhouse.arn
}

output "kubeconfig_command" {
  value = "aws eks update-kubeconfig --name ${aws_eks_cluster.this.name} --region ${var.aws_region}"
}
