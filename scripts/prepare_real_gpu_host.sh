#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "$0")/.." && pwd)
state_root="$repository_root/.inferdrome-gpu"
managed_capability_profile=""
host_pin_path="$repository_root/examples/real-gpu/host-pin.json"
qwen3_profile_path="$repository_root/campaigns/v1/profiles/managed-vllm-0.26-qwen3-8b-bf16-v1.json"
qwen3_dependency_pin_path="$repository_root/campaigns/v1/profiles/qwen3-8b-host-dependencies.json"
qwen3_model_manifest_path="$repository_root/campaigns/v1/profiles/qwen3-8b-model-files.json"
producer_pin_path="$repository_root/spikes/vllm-0.26.0/producer-pin.json"
host_python=python3.12
qwen3_profile_id=managed-vllm-0.26-qwen3-8b-bf16-v1

fail() {
  echo "prepare-real-gpu-host: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: ./scripts/prepare_real_gpu_host.sh [STATE_ROOT]
       ./scripts/prepare_real_gpu_host.sh --state-root PATH \
         --managed-capability-profile managed-vllm-0.26-qwen3-8b-bf16-v1

Without an explicit capability profile, the historical Qwen2.5 host pin is
preserved. The Qwen3 path must always be selected explicitly.
EOF
}

positional_state_root=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --state-root)
      [[ $# -ge 2 ]] || fail "--state-root requires a path"
      [[ $positional_state_root == false ]] || \
        fail "state root cannot be supplied twice"
      state_root=$2
      positional_state_root=true
      shift 2
      ;;
    --managed-capability-profile)
      [[ $# -ge 2 ]] || fail "--managed-capability-profile requires a value"
      [[ -z $managed_capability_profile ]] || \
        fail "managed capability profile cannot be supplied twice"
      managed_capability_profile=$2
      shift 2
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    --*) fail "unknown argument: $1" ;;
    *)
      [[ $positional_state_root == false ]] || \
        fail "state root cannot be supplied twice"
      state_root=$1
      positional_state_root=true
      shift
      ;;
  esac
done

if [[ -n $managed_capability_profile && \
      $managed_capability_profile != "$qwen3_profile_id" ]]; then
  fail "managed capability profile is unsupported"
fi

[[ $(uname -s) == Linux ]] || fail "a Linux NVIDIA host is required"
command -v "$host_python" >/dev/null || fail "python3.12 is required"
command -v curl >/dev/null || fail "curl is required"
command -v nvidia-smi >/dev/null || fail "nvidia-smi is required"
command -v sha256sum >/dev/null || fail "sha256sum is required"
python_include=$(
  "$host_python" -c 'import sysconfig; print(sysconfig.get_path("include") or "")'
)
[[ -n "$python_include" && -f "$python_include/Python.h" ]] || \
  fail "Python 3.12 development headers are required (install python3.12-dev)"
[[ -z ${CUDA_VISIBLE_DEVICES+x} ]] || \
  fail "CUDA_VISIBLE_DEVICES must be unset for physical GPU identity"
[[ -z ${NVIDIA_VISIBLE_DEVICES+x} ]] || \
  fail "NVIDIA_VISIBLE_DEVICES must be unset for physical GPU identity"

state_root=$(
  "$host_python" -c \
    'from pathlib import Path; import sys; print(Path(sys.argv[1]).absolute())' \
    "$state_root"
)

[[ -f "$host_pin_path" ]] || fail "real-GPU host pin is missing"
if [[ -n $managed_capability_profile ]]; then
  [[ -f "$qwen3_profile_path" ]] || fail "Qwen3 capability profile is missing"
  [[ -f "$qwen3_dependency_pin_path" ]] || \
    fail "Qwen3 host dependency pin is missing"
  [[ -f "$qwen3_model_manifest_path" ]] || \
    fail "Qwen3 model file manifest is missing"
fi
[[ -f "$producer_pin_path" ]] || fail "vLLM producer pin is missing"
[[ ! -e "$state_root" && ! -L "$state_root" ]] || \
  fail "state destination already exists: $state_root"

source_transport=""
source_archive_sha256=""
if command -v git >/dev/null 2>&1 && \
   git -C "$repository_root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  repository_status=$(
    git -C "$repository_root" status --porcelain --untracked-files=normal
  )
  [[ -z "$repository_status" ]] || fail "the Inferdrome checkout must be clean"
  repository_commit=$(git -C "$repository_root" rev-parse --verify HEAD)
  if [[ -n $managed_capability_profile ]]; then
    source_tree_sha256=$(
      git -C "$repository_root" archive --format=tar "$repository_commit" |
        sha256sum | cut -d ' ' -f 1
    )
    source_archive_sha256=sha256:$source_tree_sha256
    source_transport=git-archive-exact-head-tree-v1
  else
    source_transport=clean-git-checkout-v1
  fi
else
  source_export_path="$repository_root/.inferdrome-source-export.json"
  [[ -f "$source_export_path" && ! -L "$source_export_path" ]] || \
    fail "Inferdrome source provenance is unavailable"
  mapfile -t source_export < <(
    "$host_python" - "$source_export_path" <<'PY'
import json
from pathlib import Path
import re
import sys

value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if set(value) != {
    "repository_commit",
    "schema_version",
    "source_archive_sha256",
    "transport",
}:
    raise SystemExit("source export marker has an unexpected shape")
if value["schema_version"] != "inferdrome.source-tree-export.v1":
    raise SystemExit("source export marker version is unsupported")
if value["transport"] != "git-archive-exact-head-tree-v1":
    raise SystemExit("source export transport is unsupported")
if re.fullmatch(r"[0-9a-f]{40}", value["repository_commit"]) is None:
    raise SystemExit("source export commit is invalid")
if re.fullmatch(r"sha256:[0-9a-f]{64}", value["source_archive_sha256"]) is None:
    raise SystemExit("source export digest is invalid")
print(value["repository_commit"])
print(value["transport"])
print(value["source_archive_sha256"])
PY
  )
  [[ ${#source_export[@]} -eq 3 ]] || fail "source export marker is invalid"
  repository_commit=${source_export[0]}
  source_transport=${source_export[1]}
  source_archive_sha256=${source_export[2]}
fi
[[ $repository_commit =~ ^[0-9a-f]{40}$ ]] || \
  fail "the Inferdrome commit cannot be resolved"
repository_version=$(
  "$host_python" - "$repository_root/src/inferdrome/__init__.py" <<'PY'
import ast
from pathlib import Path
import sys

module = ast.parse(Path(sys.argv[1]).read_text(encoding="utf-8"))
versions = []
for statement in module.body:
    if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
        continue
    targets = (
        statement.targets
        if isinstance(statement, ast.Assign)
        else [statement.target]
    )
    if not any(
        isinstance(target, ast.Name) and target.id == "__version__"
        for target in targets
    ):
        continue
    value = statement.value
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        versions.append(value.value)
if len(versions) != 1 or not versions[0] or "\n" in versions[0]:
    raise SystemExit("Inferdrome source version is invalid")
print(versions[0])
PY
)
[[ -n "$repository_version" ]] || fail "Inferdrome source version cannot be read"

if [[ -z $managed_capability_profile ]]; then
  mapfile -t host_pin < <(
    "$host_python" - "$host_pin_path" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text(encoding="utf-8"))
expected = {
    "model_directory",
    "model_id",
    "pandas_version",
    "revision",
    "schema_version",
    "vllm_version",
}
if set(value) != expected:
    raise SystemExit("real-GPU host pin has an unexpected shape")
if value["schema_version"] != "inferdrome.real-gpu-host-pin.v1":
    raise SystemExit("real-GPU host pin version is unsupported")
for key in (
    "model_directory",
    "model_id",
    "pandas_version",
    "revision",
    "vllm_version",
):
    item = value[key]
    if not isinstance(item, str) or not item or "\n" in item:
        raise SystemExit(f"real-GPU host pin field is invalid: {key}")
if pathlib.PurePath(value["model_directory"]).name != value["model_directory"]:
    raise SystemExit("real-GPU model directory name is invalid")
if len(value["revision"]) != 40 or any(
    character not in "0123456789abcdef" for character in value["revision"]
):
    raise SystemExit("real-GPU model revision is invalid")
if value["vllm_version"] != "0.26.0":
    raise SystemExit("real-GPU vLLM version is unsupported")
print(value["model_directory"])
print(value["model_id"])
print(value["pandas_version"])
print(value["revision"])
print(value["vllm_version"])
PY
  )
else
  mapfile -t host_pin < <(
    "$host_python" - "$qwen3_profile_path" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text(encoding="utf-8"))
expected = {
    "benchmark_invocation",
    "campaign_id",
    "claims_boundary",
    "digest_policy",
    "implementation_state",
    "model",
    "producer",
    "profile_id",
    "schema_version",
    "server_invocation",
    "workload_binding",
}
if set(value) != expected:
    raise SystemExit("Qwen3 capability profile has an unexpected shape")
if value["schema_version"] != "inferdrome.campaign-managed-vllm-profile.v1":
    raise SystemExit("Qwen3 capability profile version is unsupported")
if value["profile_id"] != "managed-vllm-0.26-qwen3-8b-bf16-v1":
    raise SystemExit("Qwen3 capability profile identity is unsupported")
if value["campaign_id"] != "qwen-gpu-capability-campaign-v1":
    raise SystemExit("Qwen3 campaign identity is unsupported")
if value["implementation_state"] != "LOCALLY_CONFORMANT_RUNTIME_UNPROVEN":
    raise SystemExit("Qwen3 capability state is unsupported")
model = value["model"]
producer = value["producer"]
if not isinstance(model, dict) or not isinstance(producer, dict):
    raise SystemExit("Qwen3 capability profile pins are invalid")
if model != {
    "activation_dtype": "bfloat16",
    "checkpoint_precision": "BF16",
    "model_id": "Qwen/Qwen3-8B",
    "model_revision": "b968826d9c46dd6066d109eabc6255188de91218",
    "snapshot_identity_sha256": "sha256:588d19e9e489cccdad793718d8c5efbad0738be717369f9eacb94ce514992d2c",
    "snapshot_manifest_sha256": "sha256:ef291a8dd0f21604c8da3025f5112bd6641e891a3401c8550584725eaabe55cc",
    "tokenizer_revision": "b968826d9c46dd6066d109eabc6255188de91218",
}:
    raise SystemExit("Qwen3 model pin is unsupported")
if producer != {
    "adapter_name": "vllm_bench_serve",
    "adapter_version": "1.0.0",
    "host_dependencies_sha256": "sha256:4ae954afc7b7fec4db21c62d50c9f5262ad1e261270ce433840586c8410239a5",
    "name": "vllm",
    "version": "0.26.0",
}:
    raise SystemExit("Qwen3 producer pin is unsupported")
print("Qwen3-8B-b968826d9c46dd6066d109eabc6255188de91218")
print(model["model_id"])
print("2.3.3")
print(model["model_revision"])
print(producer["version"])
PY
  )
fi
[[ ${#host_pin[@]} -eq 5 ]] || fail "real-GPU host pin could not be read"
model_directory=${host_pin[0]}
model_id=${host_pin[1]}
pandas_version=${host_pin[2]}
model_revision=${host_pin[3]}
vllm_version=${host_pin[4]}

machine=$(uname -m)
case "$machine" in
  x86_64 | aarch64) ;;
  *) fail "unsupported Linux architecture: $machine" ;;
esac

mapfile -t wheel_pin < <(
  "$host_python" - "$producer_pin_path" "$machine" "$vllm_version" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
machine = sys.argv[2]
version = sys.argv[3]
value = json.loads(path.read_text(encoding="utf-8"))
producer = value.get("producer")
if not isinstance(producer, dict) or producer.get("version") != version:
    raise SystemExit("producer pin version disagrees with the host pin")
suffix = f"_{machine}.whl"
matches = [
    item
    for item in value.get("distributions", [])
    if isinstance(item, dict)
    and item.get("kind") == "wheel"
    and isinstance(item.get("filename"), str)
    and item["filename"].endswith(suffix)
]
if len(matches) != 1:
    raise SystemExit("no unique pinned vLLM wheel exists for this architecture")
wheel = matches[0]
for key in ("filename", "sha256", "url"):
    item = wheel.get(key)
    if not isinstance(item, str) or not item or "\n" in item:
        raise SystemExit(f"producer wheel pin is invalid: {key}")
if pathlib.PurePath(wheel["filename"]).name != wheel["filename"]:
    raise SystemExit("producer wheel filename is invalid")
if len(wheel["sha256"]) != 64 or any(
    character not in "0123456789abcdef" for character in wheel["sha256"]
):
    raise SystemExit("producer wheel SHA-256 is invalid")
if not wheel["url"].startswith("https://files.pythonhosted.org/"):
    raise SystemExit("producer wheel URL is not an approved PyPI file URL")
print(wheel["filename"])
print(wheel["sha256"])
print(wheel["url"])
PY
)
[[ ${#wheel_pin[@]} -eq 3 ]] || fail "vLLM wheel pin could not be read"
wheel_filename=${wheel_pin[0]}
wheel_sha256=${wheel_pin[1]}
wheel_url=${wheel_pin[2]}

tokenizers_wheel_filename=""
tokenizers_wheel_sha256=""
tokenizers_wheel_url=""
if [[ -n $managed_capability_profile ]]; then
  mapfile -t tokenizers_pin < <(
    "$host_python" - "$qwen3_dependency_pin_path" "$machine" <<'PY'
import json
from pathlib import Path, PurePath
import re
import sys

value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if set(value) != {"distributions", "schema_version"}:
    raise SystemExit("Qwen3 host dependency pin has an unexpected shape")
if value["schema_version"] != "inferdrome.qwen3-host-dependencies.v1":
    raise SystemExit("Qwen3 host dependency pin version is unsupported")
matches = [
    item
    for item in value["distributions"]
    if isinstance(item, dict)
    and item.get("architecture") == sys.argv[2]
    and item.get("name") == "tokenizers"
    and item.get("version") == "0.22.1"
]
if len(matches) != 1:
    raise SystemExit("Qwen3 tokenizers wheel pin is unavailable")
wheel = matches[0]
if set(wheel) != {
    "architecture",
    "filename",
    "name",
    "sha256",
    "size_bytes",
    "url",
    "version",
}:
    raise SystemExit("Qwen3 tokenizers wheel pin has an unexpected shape")
if PurePath(wheel["filename"]).name != wheel["filename"]:
    raise SystemExit("Qwen3 tokenizers wheel filename is invalid")
if re.fullmatch(r"sha256:[0-9a-f]{64}", wheel["sha256"]) is None:
    raise SystemExit("Qwen3 tokenizers wheel digest is invalid")
if not isinstance(wheel["size_bytes"], int) or not 1 <= wheel["size_bytes"] <= 10_000_000:
    raise SystemExit("Qwen3 tokenizers wheel size is invalid")
if not wheel["url"].startswith("https://files.pythonhosted.org/"):
    raise SystemExit("Qwen3 tokenizers wheel URL is not approved")
print(wheel["filename"])
print(wheel["sha256"].removeprefix("sha256:"))
print(wheel["url"])
print(wheel["size_bytes"])
PY
  )
  [[ ${#tokenizers_pin[@]} -eq 4 ]] || \
    fail "Qwen3 tokenizers wheel pin could not be read"
  tokenizers_wheel_filename=${tokenizers_pin[0]}
  tokenizers_wheel_sha256=${tokenizers_pin[1]}
  tokenizers_wheel_url=${tokenizers_pin[2]}
  tokenizers_wheel_size=${tokenizers_pin[3]}
fi

nvidia-smi \
  --query-gpu=index,name,uuid,driver_version \
  --format=csv,noheader,nounits

downloads_directory="$state_root/downloads"
virtual_environment="$state_root/venv"
model_root="$state_root/models/$model_directory"
wheel_path="$downloads_directory/$wheel_filename"
tokenizers_wheel_path="$downloads_directory/$tokenizers_wheel_filename"

mkdir -p "$downloads_directory" "$state_root/models"
curl --fail --location --proto '=https' --proto-redir '=https' --tlsv1.2 \
  --output "$wheel_path" "$wheel_url"
actual_wheel_sha256=$(sha256sum "$wheel_path" | cut -d ' ' -f 1)
[[ "$actual_wheel_sha256" == "$wheel_sha256" ]] || \
  fail "downloaded vLLM wheel failed SHA-256 verification"
chmod a-w "$wheel_path"

if [[ -n $managed_capability_profile ]]; then
  curl --fail --location --proto '=https' --proto-redir '=https' --tlsv1.2 \
    --output "$tokenizers_wheel_path" "$tokenizers_wheel_url"
  [[ $(stat -c %s "$tokenizers_wheel_path") == "$tokenizers_wheel_size" ]] || \
    fail "downloaded tokenizers wheel has an unexpected size"
  actual_tokenizers_sha256=$(sha256sum "$tokenizers_wheel_path" | cut -d ' ' -f 1)
  [[ $actual_tokenizers_sha256 == "$tokenizers_wheel_sha256" ]] || \
    fail "downloaded tokenizers wheel failed SHA-256 verification"
  chmod a-w "$tokenizers_wheel_path"
fi

"$host_python" -m venv "$virtual_environment"
environment_python="$virtual_environment/bin/python"
"$environment_python" -m pip install "$wheel_path"
if [[ -n $managed_capability_profile ]]; then
  "$environment_python" -m pip install \
    "pandas==$pandas_version" "$repository_root[dashboard]"
  "$environment_python" -m pip install --force-reinstall --no-deps \
    "$tokenizers_wheel_path"
else
  "$environment_python" -m pip install \
    "pandas==$pandas_version" "$repository_root[dashboard]"
fi
"$environment_python" -m pip check
if [[ -n $managed_capability_profile ]]; then
  "$environment_python" - \
    "$tokenizers_wheel_path" "$tokenizers_wheel_sha256" <<'PY'
import importlib.metadata
import json
from pathlib import Path
import sys

distribution = importlib.metadata.distribution("tokenizers")
if distribution.version != "0.22.1":
    raise SystemExit("installed tokenizers version differs from the pin")
direct_url = distribution.read_text("direct_url.json")
if direct_url is None:
    raise SystemExit("installed tokenizers distribution omits direct_url.json")
value = json.loads(direct_url)
expected_url = Path(sys.argv[1]).resolve(strict=True).as_uri()
expected_sha256 = sys.argv[2]
if (
    set(value) != {"archive_info", "url"}
    or value["url"] != expected_url
    or value["archive_info"].get("hash") != f"sha256={expected_sha256}"
    or value["archive_info"].get("hashes", {}).get("sha256")
    != expected_sha256
):
    raise SystemExit("installed tokenizers distribution differs from its source wheel")
PY
fi
[[ -x "$virtual_environment/bin/ninja" ]] || \
  fail "the pinned vLLM environment does not provide ninja"

installed_vllm_version=$(
  "$environment_python" -c \
    'import importlib.metadata; print(importlib.metadata.version("vllm"))'
)
[[ "$installed_vllm_version" == "$vllm_version" ]] || \
  fail "installed vLLM version differs from the pin"
installed_inferdrome_version=$(
  "$environment_python" -c \
    'import importlib.metadata; print(importlib.metadata.version("inferdrome"))'
)
[[ "$installed_inferdrome_version" == "$repository_version" ]] || \
  fail "installed Inferdrome version differs from the checkout"
installed_inferdrome_source=$(
  "$environment_python" -c \
    'import inferdrome; print(inferdrome.__file__)'
)
[[ $installed_inferdrome_source == "$virtual_environment"/* ]] || \
  fail "Inferdrome was not installed into the prepared environment"
[[ -x "$virtual_environment/bin/hf" ]] || \
  fail "the installed environment does not provide the hf downloader"

"$virtual_environment/bin/hf" download "$model_id" \
  --revision "$model_revision" \
  --local-dir "$model_root"

model_link=$(
  find "$model_root" -type l -print -quit
)
[[ -z "$model_link" ]] || fail "downloaded model snapshot contains a symlink"
chmod -R a-w "$model_root"

campaign_profile_sha256=""
host_dependencies_sha256=""
model_snapshot_json=""
model_manifest_sha256=""
if [[ -n $managed_capability_profile ]]; then
  "$environment_python" "$repository_root/scripts/generate_qwen3_launch_profile.py" \
    --check
  "$environment_python" \
    "$repository_root/scripts/verify_qwen3_workload_tokenization.py" \
    "$model_root"
  mapfile -t campaign_bindings < <(
    "$environment_python" - "$model_root" "$model_revision" <<'PY'
import json
from pathlib import Path
import sys

from inferdrome.execution.managed_vllm import snapshot_directory_identity
from inferdrome.qwen3_campaign import (
    qwen3_expected_snapshot_sha256,
    qwen3_host_dependencies_sha256,
    qwen3_model_manifest,
    qwen3_model_manifest_sha256,
    qwen3_profile_sha256,
)

snapshot = snapshot_directory_identity(
    Path(sys.argv[1]),
    kind="model",
    revision=sys.argv[2],
)
manifest = qwen3_model_manifest()
if (
    snapshot.sha256 != qwen3_expected_snapshot_sha256()
    or snapshot.file_count != manifest["file_count"]
    or snapshot.total_bytes != manifest["total_bytes"]
):
    raise SystemExit("Qwen3 model snapshot differs from the frozen file manifest")
print(qwen3_profile_sha256())
print(qwen3_host_dependencies_sha256())
print(qwen3_model_manifest_sha256())
print(json.dumps(snapshot.model_dump(mode="json"), separators=(",", ":"), sort_keys=True))
PY
  )
  [[ ${#campaign_bindings[@]} -eq 4 ]] || \
    fail "Qwen3 campaign bindings cannot be resolved"
  campaign_profile_sha256=${campaign_bindings[0]}
  host_dependencies_sha256=${campaign_bindings[1]}
  model_manifest_sha256=${campaign_bindings[2]}
  model_snapshot_json=${campaign_bindings[3]}
  [[ $campaign_profile_sha256 == sha256:* ]] || \
    fail "Qwen3 capability profile digest cannot be resolved"
  [[ $host_dependencies_sha256 == sha256:* ]] || \
    fail "Qwen3 host dependency digest cannot be resolved"
fi

"$environment_python" - <<'PY'
import torch

if not torch.cuda.is_available() or torch.cuda.device_count() < 1:
    raise SystemExit("the pinned environment cannot access a CUDA GPU")
if not isinstance(torch.version.cuda, str) or not torch.version.cuda:
    raise SystemExit("the pinned environment has no CUDA runtime version")
print(
    f"torch={torch.__version__} cuda={torch.version.cuda} "
    f"gpu_count={torch.cuda.device_count()}"
)
PY

"$environment_python" -m pip freeze --all | LC_ALL=C sort \
  > "$state_root/python-packages.txt"
"$virtual_environment/bin/vllm" --version > "$state_root/vllm-version.txt"
"$virtual_environment/bin/inferdrome" --version \
  > "$state_root/inferdrome-version.txt"

"$environment_python" - \
  "$state_root" \
  "$repository_commit" \
  "$machine" \
  "$wheel_filename" \
  "$wheel_sha256" \
  "$model_id" \
  "$model_revision" \
  "$model_root" \
  "$managed_capability_profile" \
  "$campaign_profile_sha256" \
  "$host_dependencies_sha256" \
  "$model_manifest_sha256" \
  "$model_snapshot_json" \
  "$tokenizers_wheel_filename" \
  "$tokenizers_wheel_sha256" \
  "$source_transport" \
  "$source_archive_sha256" <<'PY'
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sys

(
    state_root_text,
    repository_commit,
    machine,
    wheel_filename,
    wheel_sha256,
    model_id,
    model_revision,
    model_root,
    managed_capability_profile,
    campaign_profile_sha256,
    host_dependencies_sha256,
    model_manifest_sha256,
    model_snapshot_json,
    tokenizers_wheel_filename,
    tokenizers_wheel_sha256,
    source_transport,
    source_archive_sha256,
) = sys.argv[1:]
state_root = Path(state_root_text)
packages = state_root / "python-packages.txt"
if managed_capability_profile:
    from inferdrome.qwen3_tokenizer import verify_qwen3_tokenizer_files

    tokenizer_files = verify_qwen3_tokenizer_files(Path(model_root))
    model_snapshot = json.loads(model_snapshot_json)
    source_provenance = {
        "repository_commit": repository_commit,
        "transport": source_transport,
    }
    if source_archive_sha256:
        source_provenance["source_archive_sha256"] = source_archive_sha256
    receipt = {
        "architecture": machine,
        "campaign_id": "qwen-gpu-capability-campaign-v1",
        "model_directory": model_root,
        "model_id": model_id,
        "model_revision": model_revision,
        "model_manifest_sha256": model_manifest_sha256,
        "model_snapshot": model_snapshot,
        "prepared_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "profile_id": managed_capability_profile,
        "profile_sha256": campaign_profile_sha256,
        "python_packages_sha256": "sha256:"
        + hashlib.sha256(packages.read_bytes()).hexdigest(),
        "repository_commit": repository_commit,
        "schema_version": "inferdrome.qwen3-host-preparation.v1",
        "source_provenance": source_provenance,
        "host_dependencies_sha256": host_dependencies_sha256,
        "tokenizer_files": tokenizer_files.model_dump(mode="json"),
        "tokenizer_revision": model_revision,
        "tokenizers_wheel_filename": tokenizers_wheel_filename,
        "tokenizers_wheel_sha256": f"sha256:{tokenizers_wheel_sha256}",
        "vllm_wheel_filename": wheel_filename,
        "vllm_wheel_sha256": f"sha256:{wheel_sha256}",
    }
else:
    receipt = {
        "architecture": machine,
        "model_directory": model_root,
        "model_id": model_id,
        "model_revision": model_revision,
        "prepared_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "python_packages_sha256": "sha256:"
        + hashlib.sha256(packages.read_bytes()).hexdigest(),
        "repository_commit": repository_commit,
        "schema_version": "inferdrome.real-gpu-host-preparation.v1",
        "vllm_wheel_filename": wheel_filename,
        "vllm_wheel_sha256": f"sha256:{wheel_sha256}",
    }
(state_root / "host-preparation.json").write_text(
    json.dumps(receipt, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY

echo "Prepared pinned GPU environment: $state_root"
if [[ -n $managed_capability_profile ]]; then
  echo "Prepared explicit capability profile: $managed_capability_profile"
else
  echo "Next: $virtual_environment/bin/python scripts/run_real_gpu_demo.py"
  echo "Comparison: $virtual_environment/bin/python scripts/run_real_gpu_demo.py --comparison"
fi
