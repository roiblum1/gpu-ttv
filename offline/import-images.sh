#!/usr/bin/env bash
# Air-gapped side: verify the bundle's checksums, then push every archived image to the internal
# registry under the same path, tag and digest, and check the pinned digest resolves there.
#
#   offline/import-images.sh --registry registry.internal.example [--bundle DIR] [--dest-tls-verify=false]
#
# Credentials: log in first (skopeo login / podman login) or set REGISTRY_AUTH_FILE; never pass them
# here. Without a skopeo binary, `podman load -i DIR/tools/skopeo.tar` and set
# SKOPEO_IMAGE=localhost/gpu-ttv-tools/skopeo:latest.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=offline/lib.sh
. "$here/lib.sh"

registry=""
bundle="$here/bundle"
tls=()
while [ $# -gt 0 ]; do
  case "$1" in
    --registry) registry=${2%/}; shift 2 ;;
    --bundle) bundle=$2; shift 2 ;;
    --dest-tls-verify=*) tls=("$1"); shift ;;
    *) echo "usage: $0 --registry HOST[/PREFIX] [--bundle DIR] [--dest-tls-verify=false]" >&2; exit 2 ;;
  esac
done
[ -n "$registry" ] || { echo "import: --registry is required" >&2; exit 2; }
bundle=$(cd "$bundle" && pwd)
export SKOPEO_WORKDIR=$bundle

(cd "$bundle" && sha256_check SHA256SUMS)
echo "import: bundle checksums verified"

while read -r ref archive; do
  [ -n "$ref" ] || continue
  dest="$registry/$(ref_path "$ref")"
  run_skopeo copy --quiet --all --preserve-digests "${tls[@]+"${tls[@]}"}" \
    "oci-archive:$bundle/$archive" "docker://$dest:$(ref_tag "$ref")"
  tls_inspect=()
  [ ${#tls[@]} -eq 0 ] || tls_inspect=("--tls-verify=${tls[0]#--dest-tls-verify=}")
  run_skopeo inspect --raw "${tls_inspect[@]+"${tls_inspect[@]}"}" "docker://$dest@$(ref_digest "$ref")" > /dev/null
  echo "import: $dest@$(ref_digest "$ref")"
done < "$bundle/images/index.txt"
