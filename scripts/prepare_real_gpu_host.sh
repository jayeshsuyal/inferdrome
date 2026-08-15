#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "$0")/.." && pwd)
state_root=${1:-"$repository_root/.inferdrome-gpu"}
host_pin_path="$repository_root/examples/real-gpu/host-pin.json"
producer_pin_path="$repository_root/spikes/vllm-0.26.0/producer-pin.json"
host_python=python3.12

fail() {
  echo "prepare-real-gpu-host: $*" >&2
  exit 1
}

[[ $(uname -s) == Linux ]] || fail "a Linux NVIDIA host is required"
command -v "$host_python" >/dev/null || fail "python3.12 is required"
command -v curl >/dev/null || fail "curl is required"
command -v git >/dev/null || fail "git is required"
command -v nvidia-smi >/dev/null || fail "nvidia-smi is required"
command -v sha256sum >/dev/null || fail "sha256sum is required"
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
[[ -f "$producer_pin_path" ]] || fail "vLLM producer pin is missing"
[[ ! -e "$state_root" && ! -L "$state_root" ]] || \
  fail "state destination already exists: $state_root"

repository_status=$(git -C "$repository_root" status --porcelain --untracked-files=normal)
[[ -z "$repository_status" ]] || fail "the Inferdrome checkout must be clean"
repository_commit=$(git -C "$repository_root" rev-parse --verify HEAD)
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

nvidia-smi \
  --query-gpu=index,name,uuid,driver_version \
  --format=csv,noheader,nounits

downloads_directory="$state_root/downloads"
virtual_environment="$state_root/venv"
model_root="$state_root/models/$model_directory"
wheel_path="$downloads_directory/$wheel_filename"

mkdir -p "$downloads_directory" "$state_root/models"
curl --fail --location --proto '=https' --proto-redir '=https' --tlsv1.2 \
  --output "$wheel_path" "$wheel_url"
actual_wheel_sha256=$(sha256sum "$wheel_path" | cut -d ' ' -f 1)
[[ "$actual_wheel_sha256" == "$wheel_sha256" ]] || \
  fail "downloaded vLLM wheel failed SHA-256 verification"
chmod a-w "$wheel_path"

"$host_python" -m venv "$virtual_environment"
environment_python="$virtual_environment/bin/python"
"$environment_python" -m pip install "$wheel_path"
"$environment_python" -m pip install \
  "pandas==$pandas_version" "$repository_root[dashboard]"
"$environment_python" -m pip check

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
  "$model_root" <<'PY'
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
) = sys.argv[1:]
state_root = Path(state_root_text)
packages = state_root / "python-packages.txt"
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
echo "Next: $virtual_environment/bin/python scripts/run_real_gpu_demo.py"
echo "Comparison: $virtual_environment/bin/python scripts/run_real_gpu_demo.py --comparison"
