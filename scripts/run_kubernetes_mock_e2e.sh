#!/usr/bin/env bash
# Explicit local-kind contract smoke for the synthetic Kubernetes Job.
#
# This wrapper never uses an ambient context.  It creates one unique kind
# cluster and namespace, retrieves exactly one bounded runner log payload,
# verifies/publishes it locally with no replacement, then removes only the
# resources it created.

set -Eeuo pipefail

repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
manifest="$repository_root/kubernetes/inferdrome-benchmark-mock-job.yaml"
mode=${1:-}
shift || true
confirmation=${INFERDROME_ALLOW_LOCAL_KUBERNETES:-}
output_dir=${INFERDROME_KUBERNETES_OUTPUT_DIR:-"$repository_root/.inferdrome-kubernetes/evidence"}

usage() {
  printf '%s\n' \
    "usage: scripts/run_kubernetes_mock_e2e.sh mock --confirm-local-cluster" \
    "       (requires local kind/kubectl and prebuilt development images)"
}

fail() {
  printf 'Kubernetes mock preflight failed: %s\n' "$1" >&2
  exit 2
}

while (($# > 0)); do
  case "$1" in
    --confirm-local-cluster)
      confirmation=1
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      fail "unsupported option"
      ;;
  esac
  shift
done

[[ "$mode" == "mock" && "$confirmation" == "1" ]] || {
  usage >&2
  exit 2
}
[[ -f "$manifest" && ! -L "$manifest" ]] || fail "mock manifest is unavailable"
[[ "$output_dir" = /* ]] || fail "output directory must be absolute"
if [[ -e "$output_dir" && ( ! -d "$output_dir" || -L "$output_dir" ) ]]; then
  fail "output directory must be a real directory"
fi
mkdir -p -- "$output_dir" || fail "output directory cannot be created"
[[ -d "$output_dir" && ! -L "$output_dir" ]] || fail "output directory is unsafe"
artifact="$output_dir/runner-output.json"
if [[ -e "$artifact" || -L "$artifact" ]]; then
  fail "output artifact already exists"
fi

if [[ -n "${INFERDROME_KUBERNETES_PYTHON:-}" ]]; then
  inferdrome_python=$INFERDROME_KUBERNETES_PYTHON
elif [[ -x "$repository_root/.venv/bin/python" ]]; then
  inferdrome_python="$repository_root/.venv/bin/python"
else
  inferdrome_python=$(command -v python3 2>/dev/null || true)
fi
[[ -n "$inferdrome_python" && -x "$inferdrome_python" ]] || \
  fail "a compatible Inferdrome Python interpreter is required"
"$inferdrome_python" -c \
  'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' \
  >/dev/null 2>&1 || fail "a compatible Inferdrome Python interpreter is required"
PYTHONPATH="$repository_root/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$inferdrome_python" -m inferdrome.kubernetes validate \
  --profile mock --manifest "$manifest" >/dev/null 2>&1 || \
  fail "mock manifest contract is invalid"

kind_bin=$(command -v kind 2>/dev/null || true)
kubectl_bin=$(command -v kubectl 2>/dev/null || true)
[[ -n "$kind_bin" ]] || fail "kind is unavailable"
[[ -n "$kubectl_bin" ]] || fail "kubectl is unavailable"

suffix="${BASHPID:-$$}-${RANDOM}"
cluster_name="inferdrome-k8s-${suffix}"
namespace="inferdrome-mock-${suffix}"
context="kind-${cluster_name}"
job_name="inferdrome-benchmark-mock"
stage=""
cluster_created=0
namespace_created=0
cleanup_status=0

kind=("$kind_bin")
kubectl=("$kubectl_bin" --context "$context")

cleanup() {
  set +e
  local result=0
  if ((namespace_created)); then
    "${kubectl[@]}" delete namespace "$namespace" --wait=true --timeout=60s \
      >/dev/null 2>&1 || result=1
  fi
  if ((cluster_created)); then
    "${kind[@]}" delete cluster --name "$cluster_name" >/dev/null 2>&1 || result=1
  fi
  if [[ -n "$stage" ]]; then
    rm -f -- "$stage" || result=1
  fi
  cleanup_status=$result
}

on_exit() {
  run_status=$?
  trap - EXIT INT TERM
  cleanup
  if ((cleanup_status != 0)); then
    printf '%s\n' "Kubernetes mock cleanup was not confirmed" >&2
    exit 70
  fi
  exit "$run_status"
}

trap on_exit EXIT
trap 'exit 130' INT TERM

cluster_created=1
"${kind[@]}" create cluster --name "$cluster_name" --wait 60s \
  >/dev/null 2>&1 || fail "local kind cluster could not be created"
"${kind[@]}" load docker-image inferdrome/compose-mock:development \
  inferdrome/runner:development --name "$cluster_name" >/dev/null 2>&1 || \
  fail "development images could not be loaded into kind"

namespace_created=1
"${kubectl[@]}" create namespace "$namespace" >/dev/null 2>&1 || \
  fail "unique Kubernetes namespace could not be created"
"${kubectl[@]}" apply --namespace "$namespace" --filename "$manifest" \
  >/dev/null 2>&1 || fail "mock Job could not be applied"
"${kubectl[@]}" wait --namespace "$namespace" --for=condition=complete \
  "job/$job_name" --timeout=300s >/dev/null 2>&1 || \
  fail "mock Job did not complete"

pod_name=$("${kubectl[@]}" get pods --namespace "$namespace" \
  --selector "job-name=$job_name" \
  --output 'jsonpath={.items[0].metadata.name}' 2>/dev/null) || \
  fail "mock Job pod could not be identified"
[[ "$pod_name" =~ ^inferdrome-benchmark-mock-[a-z0-9-]{1,63}$ ]] || \
  fail "mock Job pod identity is invalid"

stage=$(mktemp "$output_dir/.runner-output.XXXXXX") || \
  fail "synthetic output staging failed"
chmod 600 "$stage" || fail "synthetic output staging failed"
"${kubectl[@]}" logs --namespace "$namespace" "$pod_name" \
  --container synthetic-runner --tail=1 --limit-bytes=65536 >"$stage" 2>/dev/null || \
  fail "synthetic runner logs could not be retrieved"
stage_size=$(wc -c <"$stage" | tr -d '[:space:]') || \
  fail "synthetic runner logs could not be bounded"
[[ "$stage_size" =~ ^[0-9]+$ && "$stage_size" -le 65536 ]] || \
  fail "synthetic runner logs exceed their bound"
PYTHONPATH="$repository_root/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$inferdrome_python" -m inferdrome.kubernetes verify-output "$stage" \
  >/dev/null 2>&1 || fail "synthetic runner logs failed verification"
PYTHONPATH="$repository_root/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$inferdrome_python" -m inferdrome.kubernetes publish-output "$stage" "$artifact" \
  >/dev/null 2>&1 || fail "synthetic output publication failed"
rm -f -- "$stage" || fail "synthetic output staging cleanup failed"
stage=""
PYTHONPATH="$repository_root/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$inferdrome_python" -m inferdrome.kubernetes verify-output "$artifact" \
  >/dev/null 2>&1 || fail "published synthetic output failed read-back verification"
printf '%s\n' "Kubernetes mock contract smoke published synthetic output"
