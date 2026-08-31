#!/usr/bin/env bash
set -euo pipefail

readonly uv_version="0.8.17"
readonly uv_archive_sha256="920cbcaad514cc185634f6f0dcd71df5e8f4ee4456d440a22e0f8c0f142a8203"
readonly uv_archive_url="https://github.com/astral-sh/uv/releases/download/${uv_version}/uv-x86_64-unknown-linux-gnu.tar.gz"

fail() {
  printf 'CI uv bootstrap failed: %s\n' "$1" >&2
  exit 1
}

if [[ "$#" -eq 2 && "$1" == "--extra" && "$2" == "dev" ]]; then
  :
elif [[
  "$#" -eq 4 && "$1" == "--extra" && "$2" == "dev" &&
    "$3" == "--extra" && "$4" == "dashboard"
]]; then
  :
else
  fail "expected '--extra dev' with optional trailing '--extra dashboard'"
fi

[[ "$(uname -s)" == "Linux" && "$(uname -m)" == "x86_64" ]] ||
  fail "the pinned uv archive requires Linux x86_64"

runner_temp=${RUNNER_TEMP:-}
if [[ -z "$runner_temp" || "$runner_temp" != /* || ! -d "$runner_temp" || -L "$runner_temp" ]]; then
  fail "RUNNER_TEMP must identify a real absolute directory"
fi

repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
lock_path="$repository_root/uv.lock"
[[ -f "$lock_path" && ! -L "$lock_path" ]] ||
  fail "the committed uv.lock must be a regular file"

python_bin=$(command -v python 2>/dev/null || true)
[[ -n "$python_bin" ]] || fail "setup-python did not provide python"
# Reuse setup-python's pinned cache integration without adding another action.
pip_cache_root=$("$python_bin" -m pip cache dir)
if [[ "$pip_cache_root" != /* || "$pip_cache_root" == *$'\n'* ]]; then
  fail "pip cache root must be one absolute path"
fi
export UV_CACHE_DIR="${pip_cache_root%/}/inferdrome-uv"
if [[ -e "$UV_CACHE_DIR" && ( ! -d "$UV_CACHE_DIR" || -L "$UV_CACHE_DIR" ) ]]; then
  fail "UV_CACHE_DIR must be a real directory"
fi
mkdir -p -- "$UV_CACHE_DIR"

bootstrap_root=$(mktemp -d "$runner_temp/inferdrome-ci-uv.XXXXXX")
archive_path="$bootstrap_root/uv.tar.gz"
curl \
  --fail \
  --location \
  --proto '=https' \
  --proto-redir '=https' \
  --tlsv1.2 \
  --silent \
  --show-error \
  --output "$archive_path" \
  "$uv_archive_url"
printf '%s  %s\n' "$uv_archive_sha256" "$archive_path" |
  sha256sum --check --strict -
tar -xzf "$archive_path" --strip-components=1 -C "$bootstrap_root"

uv_bin="$bootstrap_root/uv"
[[ -x "$uv_bin" && ! -L "$uv_bin" ]] ||
  fail "verified uv archive did not contain the expected executable"
[[ "$("$uv_bin" --version)" == "uv $uv_version" ]] ||
  fail "verified uv executable reported an unexpected version"

cd -- "$repository_root"
"$uv_bin" lock --check
"$uv_bin" sync --frozen --no-install-project "$@"
[[ -x .venv/bin/python ]] || fail "frozen sync did not create .venv/bin/python"
