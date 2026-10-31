#!/usr/bin/env bash
# SBOMs for the release: the Python dependencies from uv.lock (CycloneDX, always), and every image
# archive given with syft (the local binary, else the pinned syft image under podman).
#
#   offline/sbom.sh --out DIR [--platform linux/amd64] [IMAGE_ARCHIVE.tar ...]
#
# Without syft or podman the image SBOMs are skipped with the command to run elsewhere:
#   syft scan oci-archive:<archive> --platform linux/amd64 -o spdx-json=<name>.spdx.json
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=$(dirname "$here")
# shellcheck source=offline/lib.sh
. "$here/lib.sh"

out=""
platform=linux/amd64
archives=()
while [ $# -gt 0 ]; do
  case "$1" in
    --out) out=$2; shift 2 ;;
    --platform) platform=$2; shift 2 ;;
    -*) echo "usage: $0 --out DIR [--platform OS/ARCH] [ARCHIVE ...]" >&2; exit 2 ;;
    *) archives+=("$1"); shift ;;
  esac
done
[ -n "$out" ] || { echo "sbom: --out is required" >&2; exit 2; }
mkdir -p "$out"
out=$(cd "$out" && pwd)

uv export --project "$root" --quiet --frozen --no-dev --format cyclonedx1.5 \
  --preview-features sbom-export > "$out/gpu-ttv-python.cdx.json"
echo "sbom: $out/gpu-ttv-python.cdx.json (Python runtime dependencies from uv.lock)"

[ ${#archives[@]} -gt 0 ] || exit 0
syft_image=$(lock_refs "$here/images.lock" | grep -m1 '^docker.io/anchore/syft:')
for archive in "${archives[@]}"; do
  dir=$(cd "$(dirname "$archive")" && pwd)
  name=$(basename "$archive" .tar)
  args=(scan "oci-archive:$dir/$name.tar" --platform "$platform" -o "spdx-json=$out/$name.spdx.json")
  if command -v syft >/dev/null 2>&1; then
    syft "${args[@]}"
  elif command -v podman >/dev/null 2>&1; then
    relabel=""
    [ "$(uname -s)" = Linux ] && relabel=":z"
    podman run --rm -v "$dir:$dir:ro${relabel:+,z}" -v "$out:$out$relabel" "$syft_image" "${args[@]}"
  else
    echo "sbom: no syft or podman; run elsewhere: syft ${args[*]}" >&2
    continue
  fi
  echo "sbom: $out/$name.spdx.json"
done
