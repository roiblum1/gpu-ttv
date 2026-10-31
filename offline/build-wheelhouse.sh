#!/usr/bin/env bash
# Connected side: download every Python wheel uv.lock pins, hash-checked, for the internal Python
# mirror (or for building the image straight from offline/wheelhouse/). See offline/README.md.
#
#   offline/build-wheelhouse.sh [--arch x86_64|aarch64] [--runtime-only] [--out DIR]
#
# Runs pip inside the pinned Python image (offline/images.lock) so environment markers are Linux's.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=$(dirname "$here")
# shellcheck source=offline/lib.sh
. "$here/lib.sh"

arch=x86_64
groups=(--all-groups)
out="$here/wheelhouse"
while [ $# -gt 0 ]; do
  case "$1" in
    --arch) arch=$2; shift 2 ;;
    --runtime-only) groups=(--no-dev); shift ;;
    --out) out=$2; shift 2 ;;
    *) echo "usage: $0 [--arch x86_64|aarch64] [--runtime-only] [--out DIR]" >&2; exit 2 ;;
  esac
done
case "$arch" in x86_64|aarch64) ;; *) echo "unsupported arch: $arch" >&2; exit 2 ;; esac

python_image=$(lock_refs "$here/images.lock" | grep -m1 '^docker.io/library/python:')
mkdir -p "$out"
out=$(cd "$out" && pwd)
uv export --project "$root" --quiet --frozen "${groups[@]}" --no-emit-project \
  --format requirements-txt > "$out/requirements.txt"

platforms=()
for p in manylinux_2_28 manylinux_2_17 manylinux2014; do platforms+=(--platform "${p}_${arch}"); done
podman run --rm -e PIP_DISABLE_PIP_VERSION_CHECK=1 -v "$out:/out" "$python_image" \
  python -m pip download --quiet --no-deps --require-hashes \
  --only-binary=:all: --implementation cp --python-version 3.12 --abi cp312 --abi abi3 --abi none \
  "${platforms[@]}" -r /out/requirements.txt -d /out

(cd "$out" && sha256_lines -- *.whl requirements.txt > SHA256SUMS)
echo "wheelhouse: $(find "$out" -name '*.whl' | wc -l | tr -d ' ') wheels for linux/$arch in $out"
