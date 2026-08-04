#!/usr/bin/env bash
#
# aws_down.sh — tear down the AWS deployment created by aws_up.sh.
#
# Usage:
#   ./aws_down.sh                  # interactive terraform destroy
#   ./aws_down.sh -auto-approve    # extra args are passed through to terraform destroy
#   ./aws_down.sh --force          # proceed even if the app namespace's EBS
#                                  # volumes cannot be confirmed deleted
#
# --force is consumed by this script and is NOT passed to terraform. Reach for
# it only when you already know the volumes are gone (or the cluster never came
# up) and step 2 simply has nothing left to verify against. The post-destroy
# sweep in step 4 still reports anything that survived either way.

set -euo pipefail

cd "$(dirname "$0")"

info() { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

NS=data-platform

FORCE=0
TF_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    *)       TF_ARGS+=("$arg") ;;
  esac
done
# ${TF_ARGS[@]+...} guards against bash 3.2 treating an empty array as unset
# under `set -u`; stock macOS still ships bash 3.2.
set -- ${TF_ARGS[@]+"${TF_ARGS[@]}"}

# --- 1. Identify the stack ---------------------------------------------------
# Read these BEFORE destroy: once the state is empty the outputs stop resolving,
# and the orphan sweep in step 4 needs the cluster name to build its tag filter.
info "Identifying the stack"
CLUSTER_NAME=$(terraform -chdir=terraform output -raw cluster_name 2>/dev/null || true)
AWS_REGION=$(terraform -chdir=terraform output -raw aws_region 2>/dev/null || true)
if [ -n "$CLUSTER_NAME" ] && [ -n "$AWS_REGION" ]; then
  echo "  cluster: ${CLUSTER_NAME}"
  echo "  region:  ${AWS_REGION}"
else
  warn "Could not read cluster_name / aws_region from terraform state. The orphan
       sweep in step 4 will be skipped, so this script will not be able to
       confirm that nothing is still billing."
fi

# --- 2. Delete the app namespace and its EBS-backed volumes ------------------
# This namespace DOES hold an AWS-billed resource of its own: the ClickHouse
# StatefulSet's PVC is backed by a real gp3 EBS volume, provisioned at runtime
# by the EBS CSI driver. Terraform has never seen that volume, so
# `terraform destroy` cannot reap it — it only disappears if the PVC is deleted
# while the cluster, and therefore the CSI driver, is still running. Destroy the
# node group first and the driver dies with the PV deletion still pending: the
# volume is orphaned and bills forever. So this step blocks and verifies rather
# than shrugging and carrying on.

# PVs are cluster-scoped, so `kubectl delete namespace` returning does NOT mean
# the backing volumes are gone — it only means the PVCs are. Track the PVs
# themselves. A PV object survives until the CSI driver's DeleteVolume call has
# actually succeeded, which makes "no PVs left" a sound proxy for "no EBS left".
pvs_bound_to_ns() {
  kubectl get pv \
    -o jsonpath="{range .items[?(@.spec.claimRef.namespace==\"${NS}\")]}{.metadata.name}{\" \"}{end}" \
    2>/dev/null || true
}

is_blank() { [ -z "$(printf '%s' "$1" | tr -d '[:space:]')" ]; }

NS_VOLUME_IDS=""
NS_VERIFIED=0   # 1 once we have actually looked at this namespace's volumes
if kubectl cluster-info --request-timeout=10s >/dev/null 2>&1; then
  NS_VERIFIED=1
  # Record the volume IDs now, while the PV objects still exist. Once they are
  # gone there is no way to map a PV back to an EBS volume, and these IDs are
  # what makes the step 4 sweep exact instead of a guess.
  NS_VOLUME_IDS=$(kubectl get pv \
    -o jsonpath="{range .items[?(@.spec.claimRef.namespace==\"${NS}\")]}{.spec.csi.volumeHandle}{\" \"}{end}" \
    2>/dev/null || true)
  if ! is_blank "$NS_VOLUME_IDS"; then
    echo "  EBS volumes currently backing ${NS}: ${NS_VOLUME_IDS}"
  fi

  info "Deleting namespace ${NS} (this deletes the ClickHouse and Kafka PVCs too)"
  echo "NOTE: this waits for the EBS CSI driver to actually delete the volumes."
  echo "      That is usually under a minute; it is allowed up to 10."
  if ! kubectl delete namespace "$NS" --ignore-not-found --timeout=600s; then
    warn "Namespace deletion did not finish within the timeout — checking the
       volumes directly below."
  fi

  info "Verifying no PersistentVolumes are left bound to ${NS}"
  PVS_GONE=0
  LEFTOVER_PVS=""
  for _ in $(seq 1 60); do   # up to ~5 minutes
    LEFTOVER_PVS=$(pvs_bound_to_ns)
    if is_blank "$LEFTOVER_PVS"; then
      PVS_GONE=1
      break
    fi
    sleep 5
  done

  if [ "$PVS_GONE" -eq 1 ]; then
    echo "  Confirmed: no PersistentVolumes remain bound to ${NS}."
  elif [ "$FORCE" -eq 1 ]; then
    warn "PersistentVolumes are STILL bound to ${NS}: ${LEFTOVER_PVS}
       Continuing because --force was given. Their EBS volumes will almost
       certainly be orphaned by the destroy below — step 4 will list them."
  else
    die "PersistentVolumes are still bound to ${NS} after waiting 5 minutes:
         ${LEFTOVER_PVS}
       Backing EBS volumes: ${NS_VOLUME_IDS:-unknown}

       Refusing to run 'terraform destroy'. Destroying the node group now would
       kill the EBS CSI driver with these deletions still pending, stranding the
       volumes so they bill indefinitely.

       Find out what is holding them:
         kubectl get pv
         kubectl -n ${NS} get pvc,pod
       A pod still mounting the volume is the usual cause. Once 'kubectl get pv'
       lists none of the above, re-run ./aws_down.sh.

       If you have already confirmed the volumes are gone and want to press on
       regardless, re-run with --force and read the step 4 sweep carefully."
  fi
else
  warn "kubectl cannot reach a cluster — skipping namespace cleanup.
       If the EKS cluster is in fact still up, its ClickHouse EBS volume will be
       orphaned by the destroy below. Step 4 is the backstop, but without a
       cluster it cannot collect the volume IDs, so it can only match on tags —
       the verdict at the end will say so rather than claim an all-clear."
fi

# --- 3. Destroy all AWS infrastructure ---------------------------------------
info "Destroying terraform-managed infrastructure (EKS, RDS, S3, VPC, ingress)"
echo "NOTE: EKS takes a while to delete — expect 15-20 minutes total."
terraform -chdir=terraform destroy "$@"

# --- 4. Post-destroy orphan sweep --------------------------------------------
# terraform reporting success only covers what terraform manages. It says
# nothing about resources the cluster created for itself at runtime: the
# CSI-provisioned EBS volumes and the ingress-nginx load balancer. Ask AWS.
ORPHANS=0
SWEEP_OK=0   # flipped to 1 only once every query below has actually run

sweep_note() {
  ORPHANS=$((ORPHANS + 1))
  printf '\n  STILL BILLING: %s\n' "$1"
  printf '  Delete it with:\n    %s\n' "$2"
}

if [ -n "$CLUSTER_NAME" ] && [ -n "$AWS_REGION" ]; then
  info "Sweeping for resources the cluster created at runtime"
  SWEEP_OK=1

  # (a) Anything still carrying the cluster ownership tag.
  VOLS=$(aws ec2 describe-volumes --region "$AWS_REGION" \
    --filters "Name=tag:kubernetes.io/cluster/${CLUSTER_NAME},Values=owned" \
    --query 'Volumes[].VolumeId' --output text) || { SWEEP_OK=0; VOLS=""; }

  # (b) The exact volumes step 2 saw backing the namespace. The EKS EBS CSI
  #     add-on does not always apply the kubernetes.io/cluster tag, so (a) on
  #     its own can miss them; by ID there is no ambiguity. --filters rather
  #     than --volume-ids so an already-deleted ID comes back empty instead of
  #     raising InvalidVolume.NotFound.
  if ! is_blank "$NS_VOLUME_IDS"; then
    # shellcheck disable=SC2086  # deliberate word splitting into one ID per line
    IDS_CSV=$(printf '%s\n' $NS_VOLUME_IDS | awk '/^vol-/{printf "%s%s", sep, $0; sep=","}')
    if [ -n "$IDS_CSV" ]; then
      VOLS_BY_ID=$(aws ec2 describe-volumes --region "$AWS_REGION" \
        --filters "Name=volume-id,Values=${IDS_CSV}" \
        --query 'Volumes[].VolumeId' --output text) || { SWEEP_OK=0; VOLS_BY_ID=""; }
      VOLS="${VOLS} ${VOLS_BY_ID}"
    fi
  fi

  # shellcheck disable=SC2086  # deliberate word splitting
  for vol in $(printf '%s\n' $VOLS | awk '/^vol-/' | sort -u); do
    sweep_note "EBS volume ${vol}" \
      "aws ec2 delete-volume --region ${AWS_REGION} --volume-id ${vol}"
  done

  # (c) Load balancers. The ingress-nginx NLB is created by the cloud controller
  #     in response to a Service, not by terraform. Neither ELB API supports a
  #     tag filter on the list call, so list then match on tags.
  LB_ARNS=$(aws elbv2 describe-load-balancers --region "$AWS_REGION" \
    --query 'LoadBalancers[].LoadBalancerArn' --output text) || { SWEEP_OK=0; LB_ARNS=""; }
  if ! is_blank "$LB_ARNS"; then
    # shellcheck disable=SC2086  # deliberate word splitting into separate args
    ORPHAN_LBS=$(aws elbv2 describe-tags --region "$AWS_REGION" \
      --resource-arns $LB_ARNS \
      --query "TagDescriptions[?Tags[?Key=='kubernetes.io/cluster/${CLUSTER_NAME}' && Value=='owned']].ResourceArn" \
      --output text) || { SWEEP_OK=0; ORPHAN_LBS=""; }
    for lb in $ORPHAN_LBS; do
      # An empty JMESPath result can surface as the literal 'None'.
      case "$lb" in arn:*) ;; *) continue ;; esac
      sweep_note "Load balancer ${lb}" \
        "aws elbv2 delete-load-balancer --region ${AWS_REGION} --load-balancer-arn ${lb}"
    done
  fi

  # Classic ELBs live in a separate API. The chart asks for an NLB, but a
  # dropped annotation silently yields a classic ELB instead, so check both.
  ELB_NAMES=$(aws elb describe-load-balancers --region "$AWS_REGION" \
    --query 'LoadBalancerDescriptions[].LoadBalancerName' --output text) \
    || { SWEEP_OK=0; ELB_NAMES=""; }
  if ! is_blank "$ELB_NAMES"; then
    # shellcheck disable=SC2086  # deliberate word splitting into separate args
    ORPHAN_ELBS=$(aws elb describe-tags --region "$AWS_REGION" \
      --load-balancer-names $ELB_NAMES \
      --query "TagDescriptions[?Tags[?Key=='kubernetes.io/cluster/${CLUSTER_NAME}' && Value=='owned']].LoadBalancerName" \
      --output text) || { SWEEP_OK=0; ORPHAN_ELBS=""; }
    for elb in $ORPHAN_ELBS; do
      case "$elb" in None) continue ;; esac
      sweep_note "Classic load balancer ${elb}" \
        "aws elb delete-load-balancer --region ${AWS_REGION} --load-balancer-name ${elb}"
    done
  fi

  if [ "$SWEEP_OK" -eq 1 ] && [ "$ORPHANS" -eq 0 ]; then
    echo "  Nothing left tagged to ${CLUSTER_NAME}: no EBS volumes, no load balancers."
  fi
fi

# --- 5. Remove rendered manifests --------------------------------------------
# k8s/.rendered/ contains the rendered copies of the manifests, including the
# RDS and ClickHouse passwords — remove them now that the stack is gone.
if [ -d k8s/.rendered ]; then
  info "Removing k8s/.rendered/ (contains rendered secrets)"
  rm -rf k8s/.rendered
fi

# --- 6. Verdict --------------------------------------------------------------
echo
if [ "$ORPHANS" -gt 0 ]; then
  echo "TEARDOWN INCOMPLETE — ${ORPHANS} resource(s) survived terraform destroy and are"
  echo "still billing. Run the delete command printed under each one above, then run"
  echo "./aws_down.sh again to re-check."
  exit 1
elif [ "$SWEEP_OK" -eq 0 ]; then
  echo "Terraform destroy finished, but the orphan sweep could not be completed, so"
  echo "this script cannot promise that nothing is billing. Check by hand:"
  echo
  echo "  aws ec2 describe-volumes --region <region> \\"
  echo "    --filters Name=status,Values=available --query 'Volumes[].VolumeId'"
  echo "  aws elbv2 describe-load-balancers --region <region> \\"
  echo "    --query 'LoadBalancers[].LoadBalancerArn'"
  exit 1
elif [ "$NS_VERIFIED" -eq 0 ]; then
  # The sweep can only match on the cluster ownership tag here, and the EKS EBS
  # CSI add-on does not reliably apply it — so "found nothing" is weaker than
  # usual. Say so instead of promising an all-clear we cannot back up.
  echo "Terraform destroy finished and nothing tagged to ${CLUSTER_NAME} is left, but"
  echo "step 2 never reached the cluster, so the ClickHouse EBS volume was not"
  echo "positively accounted for. Confirm no untagged volume survived:"
  echo
  echo "  aws ec2 describe-volumes --region ${AWS_REGION} \\"
  echo "    --filters Name=status,Values=available Name=tag-key,Values=CSIVolumeName \\"
  echo "    --query 'Volumes[].{Id:VolumeId,Size:Size,Created:CreateTime}' --output table"
  echo
  echo "Anything listed there from this stack is billing; delete it with:"
  echo "  aws ec2 delete-volume --region ${AWS_REGION} --volume-id <vol-id>"
  exit 1
else
  echo "Teardown complete. Nothing from this deployment is billing anymore."
fi
