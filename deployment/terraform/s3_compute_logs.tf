# One S3 bucket, two disposable jobs for the aws environment:
#   compute-logs/ — Dagster's S3ComputeLogManager step stdout/stderr
#   dagster-io/   — the s3_pickle IO manager's intermediate values
# Sharing a bucket is deliberate: it avoids a second bucket and a second IRSA
# policy, and both prefixes hold throwaway data with the same lifetime.

# Bucket names are globally unique across every AWS account, so the project
# prefix alone would collide with anyone else running this stack.
resource "random_id" "bucket_suffix" {
  byte_length = 4
}

resource "aws_s3_bucket" "platform" {
  bucket = "${var.project_name}-platform-${random_id.bucket_suffix.hex}"

  # Nothing may keep billing after teardown. terraform destroy cannot delete a
  # non-empty bucket, and the resulting BucketNotEmpty error aborts the destroy
  # partway through, leaving a wrecked half-destroyed stack that still costs
  # money. Everything in here is disposable, so let destroy empty it.
  force_destroy = true

  tags = { Name = "${var.project_name}-platform" }
}

# Versioning is intentionally NOT enabled: noncurrent versions outlive the
# expiration rule below and turn teardown into a paid archaeology exercise.

resource "aws_s3_bucket_public_access_block" "platform" {
  bucket = aws_s3_bucket.platform.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Compute logs and pickled intermediates are only interesting while a run is
# recent, so expire everything after a week — that keeps the bucket near-free
# while the platform is up. Aborting stale multipart uploads matters too: their
# orphaned parts are billable and can block the bucket from being deleted.
resource "aws_s3_bucket_lifecycle_configuration" "platform" {
  bucket = aws_s3_bucket.platform.id

  rule {
    id     = "expire-everything"
    status = "Enabled"

    filter {}

    expiration {
      days = 7
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# IRSA for the `dagster` service account in the app namespace — dagster-web,
# dagster-daemon and both code locations run under it. The namespace is
# hardcoded because it comes from the k8s manifests, not from var.project_name.
resource "aws_iam_role" "dagster" {
  name = "${var.project_name}-dagster-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.eks.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_issuer}:sub" = "system:serviceaccount:data-platform:dagster"
          "${local.oidc_issuer}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })

  tags = { Name = "${var.project_name}-dagster-role" }
}

# IRSA for the `clickhouse` service account. The warehouse reads landed Parquet
# straight out of S3 with the s3() table function, so it needs its own identity
# rather than borrowing the dagster role.
#
# Deliberately a second role and not a second binding on the first one. dagster
# has read/write over the whole bucket because it also writes compute logs and
# io manager pickles; ClickHouse only ever reads landing/, and giving a database
# that runs LLM-generated SQL (the analytics_ro path) write access to the
# platform bucket would be an odd thing to do on purpose.
resource "aws_iam_role" "clickhouse" {
  name = "${var.project_name}-clickhouse-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.eks.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_issuer}:sub" = "system:serviceaccount:data-platform:clickhouse"
          "${local.oidc_issuer}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })

  tags = { Name = "${var.project_name}-clickhouse-role" }
}

# Read-only, and only under landing/.
resource "aws_iam_role_policy" "clickhouse_s3" {
  name = "${var.project_name}-clickhouse-s3"
  role = aws_iam_role.clickhouse.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.platform.arn}/landing/*"
      },
      {
        # Needed if a source expression ever widens from one object to a glob.
        # Conditioned on the prefix so it cannot enumerate compute-logs/ or
        # dagster-io/.
        Effect    = "Allow"
        Action    = ["s3:ListBucket"]
        Resource  = aws_s3_bucket.platform.arn
        Condition = { StringLike = { "s3:prefix" = ["landing/*"] } }
      },
    ]
  })
}

# Scoped to this bucket only: the pods have no business reading anything else.
resource "aws_iam_role_policy" "dagster_s3" {
  name = "${var.project_name}-dagster-s3"
  role = aws_iam_role.dagster.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.platform.arn
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:AbortMultipartUpload",
        ]
        Resource = "${aws_s3_bucket.platform.arn}/*"
      },
    ]
  })
}
