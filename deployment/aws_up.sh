#!/usr/bin/env bash
#
# aws_up.sh — bring the full data platform up on AWS (EKS + RDS + S3).
# Kafka is not a managed service here: it runs in-cluster as a StatefulSet,
# applied with the rest of the manifests in section 7.
#
# Usage:
#   ./aws_up.sh                  # interactive terraform apply
#   ./aws_up.sh -auto-approve    # extra args are passed through to terraform apply
#
# Image overrides (optional):
#   IMAGE_PREFIX=you/your-prefix IMAGE_TAG=1.1.0 ./aws_up.sh
#
# The default below must track VERSION in ../push_images.sh, which is what
# actually builds and pushes these tags. They are two files that have to agree
# and nothing enforces it: the k8s manifests carry ${IMAGE_TAG} placeholders
# rather than literal tags, so a stale default here deploys an old image
# cleanly and silently rather than failing on a missing tag.
#
# ClickHouse password overrides (optional — see section 5):
#   CLICKHOUSE_PASSWORD=... CLICKHOUSE_RO_PASSWORD=... ./aws_up.sh

set -euo pipefail

cd "$(dirname "$0")"

IMAGE_PREFIX="${IMAGE_PREFIX:-ajohnson0764/data-platform}"
IMAGE_TAG="${IMAGE_TAG:-1.1.0}"   # keep in step with VERSION in ../push_images.sh

info() { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# --- 1. Preflight ------------------------------------------------------------
info "Preflight checks"
for cmd in aws terraform kubectl python3 openssl; do
  command -v "$cmd" >/dev/null 2>&1 \
    || die "'$cmd' not found on PATH. Install it and re-run."
done
aws sts get-caller-identity >/dev/null 2>&1 \
  || die "AWS credentials are not working ('aws sts get-caller-identity' failed).
       Run 'aws configure', or export AWS_PROFILE / AWS_ACCESS_KEY_ID, then re-run."

# --- 2. Terraform ------------------------------------------------------------
if [ ! -d terraform/.terraform ]; then
  info "Initializing terraform"
  terraform -chdir=terraform init
fi

info "Applying terraform"
echo "NOTE: the EKS cluster takes ~10 minutes to create on first apply."
echo "      Total first-apply time is typically 15-20 minutes. This is normal."
terraform -chdir=terraform apply "$@"

# --- 3. Read terraform outputs -----------------------------------------------
info "Reading terraform outputs"
AWS_REGION=$(terraform -chdir=terraform output -raw aws_region)
CLUSTER_NAME=$(terraform -chdir=terraform output -raw cluster_name)
RDS_HOST=$(terraform -chdir=terraform output -raw rds_host)
RDS_PASSWORD=$(terraform -chdir=terraform output -raw rds_password)
# No kafka bootstrap to read: the broker is in-cluster, so its address is fixed
# cluster DNS written literally into the manifests, not a terraform output.
COMPUTE_LOGS_BUCKET=$(terraform -chdir=terraform output -raw compute_logs_bucket)
DAGSTER_ROLE_ARN=$(terraform -chdir=terraform output -raw dagster_role_arn)
CLICKHOUSE_ROLE_ARN=$(terraform -chdir=terraform output -raw clickhouse_role_arn)

# --- 4. Kubeconfig -----------------------------------------------------------
info "Updating kubeconfig for cluster '${CLUSTER_NAME}' in ${AWS_REGION}"
aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$AWS_REGION"

# --- 5. ClickHouse credentials -----------------------------------------------
# These two are the only rendered values that do NOT come from terraform, and
# they have to stay STABLE across re-runs of this script.
#
# ClickHouse resolves the users.xml `from_env` passwords once, when the server
# process starts, and its data volume survives a re-apply (CLICKHOUSE_DB only
# initialises an empty volume). Nothing in the StatefulSet's pod spec changes
# when the Secret's contents change, so `kubectl apply` updates the Secret
# WITHOUT restarting ClickHouse. Mint fresh passwords on a re-run and you get a
# split brain: the running server still accepts the old password while the code
# locations and the analytics API pick up the new one on their next restart —
# whenever that happens to be. The failure is delayed and non-deterministic,
# which is worse than an immediate one.
#
# So: resolve in this order — explicit environment variable, then whatever is
# already in the live cluster, and only generate as a last resort.
info "Resolving ClickHouse credentials"

# Reads one key back out of the live Secret. go-template rather than piping to
# `base64 --decode` because BSD base64 on macOS spells that flag differently.
read_live_secret() {
  local value
  value=$(kubectl -n data-platform get secret clickhouse-credentials \
    -o go-template="{{ index .data \"$1\" | base64decode }}" 2>/dev/null || true)
  # A missing key makes the template emit '<no value>' rather than fail, and a
  # value with whitespace in it would not survive the `read` below. Treat both
  # as "nothing usable in the cluster".
  case "$value" in
    *"<no value>"*|*[[:space:]]*) value="" ;;
  esac
  printf '%s' "$value"
}

# Echoes "<source> <value>" so the caller can report what it did.
resolve_ch_password() {
  local key="$1" override="$2" live
  live=$(read_live_secret "$key")
  if [ -n "$override" ]; then
    if [ -n "$live" ] && [ "$override" != "$live" ]; then
      printf 'override-conflict %s\n' "$override"
    else
      printf 'override %s\n' "$override"
    fi
  elif [ -n "$live" ]; then
    printf 'live %s\n' "$live"
  else
    printf 'generated %s\n' "$(openssl rand -hex 16)"
  fi
}

report_ch_source() {
  case "$2" in
    override)
      echo "  $1: taken from the environment." ;;
    live)
      echo "  $1: reused the value already in the cluster's clickhouse-credentials Secret." ;;
    generated)
      echo "  $1: generated a fresh value (no live Secret to reuse)." ;;
    override-conflict)
      warn "$1 was set in the environment but differs from the value in the live
       clickhouse-credentials Secret. Going with the environment value.
       The running ClickHouse server keeps serving the OLD password until its
       pod restarts, so clients will fail to authenticate until you do:
         kubectl -n data-platform delete pod clickhouse-0" ;;
  esac
}

CH_PW_OVERRIDE="${CLICKHOUSE_PASSWORD:-}"
CH_RO_OVERRIDE="${CLICKHOUSE_RO_PASSWORD:-}"
read -r CH_PW_SOURCE CLICKHOUSE_PASSWORD \
  <<<"$(resolve_ch_password CLICKHOUSE_PASSWORD "$CH_PW_OVERRIDE")"
read -r CH_RO_SOURCE CLICKHOUSE_RO_PASSWORD \
  <<<"$(resolve_ch_password CLICKHOUSE_RO_PASSWORD "$CH_RO_OVERRIDE")"

report_ch_source CLICKHOUSE_PASSWORD "$CH_PW_SOURCE"
report_ch_source CLICKHOUSE_RO_PASSWORD "$CH_RO_SOURCE"

# --- 6. Render manifests -----------------------------------------------------
info "Rendering k8s manifests into k8s/.rendered/"
rm -rf k8s/.rendered
mkdir -p k8s/.rendered

# python3 instead of envsubst: envsubst is not available on stock macOS.
export RDS_HOST RDS_PASSWORD COMPUTE_LOGS_BUCKET DAGSTER_ROLE_ARN \
       CLICKHOUSE_ROLE_ARN AWS_REGION CLICKHOUSE_PASSWORD CLICKHOUSE_RO_PASSWORD \
       IMAGE_PREFIX IMAGE_TAG
python3 - <<'PYEOF'
import glob, os, sys

names = ["RDS_HOST", "RDS_PASSWORD", "COMPUTE_LOGS_BUCKET",
         "DAGSTER_ROLE_ARN", "CLICKHOUSE_ROLE_ARN", "AWS_REGION",
         "CLICKHOUSE_PASSWORD", "CLICKHOUSE_RO_PASSWORD",
         "IMAGE_PREFIX", "IMAGE_TAG"]
values = {name: os.environ[name] for name in names}

files = sorted(glob.glob("k8s/*.yaml"))
if not files:
    sys.exit("ERROR: no manifests found in k8s/*.yaml")

for path in files:
    with open(path) as f:
        text = f.read()
    for name, value in values.items():
        text = text.replace("${%s}" % name, value)
    out_path = os.path.join("k8s", ".rendered", os.path.basename(path))
    with open(out_path, "w") as f:
        f.write(text)
    print("  rendered %s" % out_path)
PYEOF

# Any surviving ${...} means a placeholder the contract doesn't cover — stop.
if grep -Rn '\${' k8s/.rendered/; then
  die "Unreplaced \${...} placeholders remain in k8s/.rendered/ (see lines above)."
fi

# --- 7. Apply manifests ------------------------------------------------------
info "Applying manifests to the cluster"
kubectl apply -f k8s/.rendered/

# --- 8. Wait for the workloads -----------------------------------------------
# ClickHouse first: it is a StatefulSet, not a Deployment, and both code
# locations fail their first ClickHouse call if it isn't serving yet. Its EBS
# volume is provisioned on first pod schedule, so it is also the slowest.
#
# Kafka is waited on for the same reason it is a StatefulSet rather than a
# managed cluster: its volume is provisioned on first schedule too, and both the
# producer and the Spark consumer spend their startup retrying a broker that
# isn't listening yet. Neither is fatal -- both retry -- but a wait here turns a
# confusing few minutes of CrashLoopBackOff-adjacent log noise into a rollout
# that either succeeds or reports why it didn't.
for target in statefulset/clickhouse \
              statefulset/kafka \
              deployment/dagster-web \
              deployment/dagster-daemon \
              deployment/code-location-etl \
              deployment/code-location-basic-ml; do
  info "Waiting for ${target} (up to 5 minutes)"
  if ! kubectl -n data-platform rollout status "$target" --timeout=5m; then
    warn "${target} is not ready yet — continuing anyway.
         Inspect it with: kubectl -n data-platform get pods"
  fi
done

# --- 9. Print the UI URL -----------------------------------------------------
info "Looking up the ingress load balancer hostname"
NLB_HOSTNAME=""
for _ in $(seq 1 36); do   # up to ~3 minutes
  NLB_HOSTNAME=$(kubectl -n ingress-nginx get svc ingress-nginx-controller \
    -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)
  [ -n "$NLB_HOSTNAME" ] && break
  sleep 5
done

echo
if [ -n "$NLB_HOSTNAME" ]; then
  echo "Dagster UI:  http://${NLB_HOSTNAME}/"
  echo "NL-to-SQL:   http://${NLB_HOSTNAME}/nl2sql/"
  echo "(The NLB's DNS record can take a few minutes to propagate — if the URL"
  echo " doesn't resolve yet, wait a bit and retry.)"
else
  warn "Load balancer hostname not available after ~3 minutes.
       Check it later with:
       kubectl -n ingress-nginx get svc ingress-nginx-controller"
fi

echo
echo "Done. This stack bills ~\$0.45-0.50/hour — run ./aws_down.sh when you're finished."
