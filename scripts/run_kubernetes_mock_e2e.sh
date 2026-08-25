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
PYTHONPATH="$repository_root/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$inferdrome_python" -m inferdrome.kubernetes preflight-output-dir "$output_dir" \
  >/dev/null 2>&1 || fail "output directory is unsafe"
mkdir -p -- "$output_dir" || fail "output directory cannot be created"
[[ -d "$output_dir" && ! -L "$output_dir" ]] || fail "output directory is unsafe"
PYTHONPATH="$repository_root/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$inferdrome_python" -m inferdrome.kubernetes preflight-output "$artifact" \
  >/dev/null 2>&1 || fail "output destination is unsafe or already exists"

kind_bin=$(command -v kind 2>/dev/null || true)
kubectl_bin=$(command -v kubectl 2>/dev/null || true)
docker_bin=$(command -v docker 2>/dev/null || true)
[[ -n "$kind_bin" ]] || fail "kind is unavailable"
[[ -n "$kubectl_bin" ]] || fail "kubectl is unavailable"
[[ -n "$docker_bin" ]] || fail "Docker is unavailable"

canonical_kind_node_image=$(
  PYTHONPATH="$repository_root/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$inferdrome_python" -c \
    'from inferdrome.kubernetes import KIND_NODE_IMAGE_REFERENCE; print(KIND_NODE_IMAGE_REFERENCE)' \
    2>/dev/null
) || fail "the pinned kind node image policy is unavailable"
kind_node_image=${INFERDROME_KIND_NODE_IMAGE:-$canonical_kind_node_image}
[[ "$kind_node_image" == "$canonical_kind_node_image" ]] || \
  fail "the pinned kind node image is not approved"
"$docker_bin" image inspect "$kind_node_image" >/dev/null 2>&1 || \
  fail "the pinned kind node image is not present locally"

suffix=$(
  "$inferdrome_python" -c 'import secrets; print(secrets.token_hex(16))' \
    2>/dev/null
) || fail "private Kubernetes identity could not be generated"
[[ "$suffix" =~ ^[0-9a-f]{32}$ ]] || fail "private Kubernetes identity is invalid"
cluster_name="inferdrome-k8s-${suffix}"
namespace="inferdrome-mock-${suffix}"
context="kind-${cluster_name}"
job_name="inferdrome-benchmark-mock"
if [[ "${INFERDROME_KUBERNETES_TEST_MODE:-0}" == "1" ]]; then
  export INFERDROME_K8S_EXPECTED_CLUSTER_NAME="$cluster_name"
fi
stage=""
state_dir=""
kubeconfig=""
kind_version_file=""
server_version_file=""
cluster_create_attempted=0
cluster_owned=0
namespace_create_attempted=0
namespace_owned=0
cleanup_status=0

kind=()
kubectl=()

cleanup() {
  set +e
  local result=0
  if ((namespace_owned)) && ((${#kubectl[@]} > 0)); then
    "${kubectl[@]}" delete namespace "$namespace" --wait=true --timeout=60s \
      >/dev/null 2>&1 || result=1
  fi
  if ((cluster_owned)) && ((${#kind[@]} > 0)); then
    "${kind[@]}" delete cluster --name "$cluster_name" >/dev/null 2>&1 || result=1
  fi
  if [[ -n "$stage" ]]; then
    rm -f -- "$stage" || result=1
  fi
  if ((cluster_create_attempted && !cluster_owned)); then
    result=1
  fi
  if ((namespace_create_attempted && !namespace_owned)); then
    result=1
  fi
  if [[ -n "$kubeconfig" ]]; then
    rm -f -- "$kubeconfig" "$kind_version_file" "$server_version_file" || result=1
  fi
  if [[ -n "$state_dir" ]]; then
    rmdir -- "$state_dir" || result=1
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

temp_root=$(cd -- "${TMPDIR:-/tmp}" 2>/dev/null && pwd -P) || \
  fail "private Kubernetes temporary root is unavailable"
[[ "$temp_root" = /* && -d "$temp_root" && ! -L "$temp_root" ]] || \
  fail "private Kubernetes temporary root is unsafe"
state_dir=$(mktemp -d "$temp_root/inferdrome-k8s.XXXXXX") || \
  fail "private Kubernetes state directory could not be created"
[[ "$state_dir" = /* && -d "$state_dir" && ! -L "$state_dir" ]] || \
  fail "private Kubernetes state directory is unsafe"
kubeconfig="$state_dir/kubeconfig"
kind_version_file="$state_dir/kind-version.txt"
server_version_file="$state_dir/server-version.json"
(umask 077 && : >"$kubeconfig") || fail "private kubeconfig could not be created"
chmod 600 "$kubeconfig" || fail "private kubeconfig could not be secured"
export KUBECONFIG="$kubeconfig"
# Do not inherit kind's alternate provider or network override.  The Docker
# image inspection above and every kind operation must use the same provider.
export KIND_EXPERIMENTAL_PROVIDER=docker
unset KIND_EXPERIMENTAL_DOCKER_NETWORK

kind=("$kind_bin")
kubectl=("$kubectl_bin" --kubeconfig "$kubeconfig" --context "$context")

# Capture only a bounded prefix before validating the executable.  A version
# command that fails, emits too much data, or emits an invalid line cannot
# reach kind inventory or cluster creation.
set +e
"$kind_bin" version 2>/dev/null | head -c 513 >"$kind_version_file"
kind_statuses=("${PIPESTATUS[@]}")
kind_version_status=${kind_statuses[0]}
head_status=${kind_statuses[1]}
set -e
if ((kind_version_status != 0 || head_status != 0)); then
  fail "kind version could not be read"
fi
PYTHONPATH="$repository_root/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$inferdrome_python" -m inferdrome.kubernetes kind-version \
  "$kind_version_file" >/dev/null 2>&1 || fail "kind version is unsupported"

cluster_list=$("${kind[@]}" get clusters 2>/dev/null) || \
  fail "kind cluster inventory could not be read"
printf '%s\n' "$cluster_list" | grep -Fx "$cluster_name" >/dev/null && \
  fail "private Kubernetes cluster identity is already in use"

cluster_create_attempted=1
if "${kind[@]}" create cluster --name "$cluster_name" \
  --image "$kind_node_image" --wait 60s \
  --kubeconfig "$kubeconfig" \
  >/dev/null 2>&1; then
  # A successful create is ownership evidence even if the read-only
  # confirmation below is temporarily unavailable.
  cluster_owned=1
else
  # A provider-side failure can occur after kind has created the cluster.
  # Reconcile the exact previously-absent name before deciding whether it is
  # safe to delete; an unresolved result remains cleanup-blocked.
  cluster_list=$("${kind[@]}" get clusters 2>/dev/null) || \
    fail "local kind cluster ownership is ambiguous"
  if printf '%s\n' "$cluster_list" | grep -Fx "$cluster_name" >/dev/null; then
    cluster_owned=1
  fi
  fail "local kind cluster could not be created"
fi
cluster_list=$("${kind[@]}" get clusters 2>/dev/null) || \
  fail "kind cluster ownership could not be confirmed"
printf '%s\n' "$cluster_list" | grep -Fx "$cluster_name" >/dev/null || \
  fail "kind cluster ownership could not be confirmed"
"${kind[@]}" load docker-image inferdrome/compose-mock:development \
  inferdrome/runner:development --name "$cluster_name" >/dev/null 2>&1 || \
  fail "development images could not be loaded into kind"

server_version_file="$state_dir/server-version.json"
"${kubectl[@]}" version --output=json >"$server_version_file" 2>/dev/null || \
  fail "Kubernetes server version could not be read"
PYTHONPATH="$repository_root/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$inferdrome_python" -m inferdrome.kubernetes server-version \
  "$server_version_file" >/dev/null 2>&1 || \
  fail "Kubernetes server version is unsupported"

namespace_create_attempted=1
"${kubectl[@]}" create namespace "$namespace" >/dev/null 2>&1 || \
  fail "unique Kubernetes namespace could not be created"
namespace_owned=1
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
