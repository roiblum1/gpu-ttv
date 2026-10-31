#!/usr/bin/env bash
# Connected side: assemble the offline bundle for one release (offline/README.md, "Connected side").
#
#   offline/make-bundle.sh [--out DIR] [--arch x86_64|aarch64] [--image LOCAL_REF]
#
# Contents: third-party images (offline/images.lock, digest-preserving archives), the Python
# wheelhouse, both Helm charts, the source tree, SBOMs, and SHA256SUMS over all of it.
# --image adds a gpu-ttv image built here (podman build) instead of building it in internal GitLab CI.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=$(dirname "$here")
# shellcheck source=offline/lib.sh
. "$here/lib.sh"

out="$here/bundle"
arch=x86_64
image=""
while [ $# -gt 0 ]; do
  case "$1" in
    --out) out=$2; shift 2 ;;
    --arch) arch=$2; shift 2 ;;
    --image) image=$2; shift 2 ;;
    *) echo "usage: $0 [--out DIR] [--arch x86_64|aarch64] [--image LOCAL_REF]" >&2; exit 2 ;;
  esac
done
case "$arch" in x86_64) platform=linux/amd64 ;; aarch64) platform=linux/arm64 ;; *) exit 2 ;; esac
version=$(sed -n 's/^version = "\(.*\)"/\1/p' "$root/pyproject.toml" | head -1)
revision=$(git -C "$root" rev-parse --short=12 HEAD)
mkdir -p "$out"/{images,charts,source,sbom,tools}
out=$(cd "$out" && pwd)
export SKOPEO_WORKDIR=$out

"$here/mirror-images.sh" --out "$out"
"$here/build-wheelhouse.sh" --arch "$arch" --out "$out/wheelhouse"

helm package "$root/deploy/helm/gpu-ttv" -d "$out/charts" >/dev/null
helm package "$root/deploy/gpu-cluster-rbac" -d "$out/charts" >/dev/null
git -C "$root" archive --format=tar.gz --prefix="gpu-ttv-$revision/" -o "$out/source/gpu-ttv-$revision.tar.gz" HEAD
cp "$here/images.lock" "$out/images.lock"
cp "$here/README.md" "$out/README.md"
mkdir -p "$out/offline"
cp "$here"/*.sh "$here/images.lock" "$out/offline/"

# The import side may lack skopeo: carry the pinned skopeo image for the target architecture.
skopeo_ref=$(lock_refs "$here/images.lock" | grep -m1 '^quay.io/skopeo/stable:')
podman pull --quiet --platform "$platform" "$(ref_pinned "$skopeo_ref")" >/dev/null
podman tag "$(ref_pinned "$skopeo_ref")" localhost/gpu-ttv-tools/skopeo:latest
podman save --quiet -o "$out/tools/skopeo.tar" localhost/gpu-ttv-tools/skopeo:latest

sbom_archives=()
if [ -n "$image" ]; then
  podman save --quiet --format oci-archive -o "$out/images/gpu-ttv.tar" "$image"
  digest="sha256:$(run_skopeo inspect --raw "oci-archive:$out/images/gpu-ttv.tar" | sha256_stdin)"
  ref="local/gpu-ttv/gpu-ttv:$version@$digest"
  printf '%s %s\n' "$ref" "images/gpu-ttv.tar" >> "$out/images/index.txt"
  printf '%s gpu-ttv release image, built by make-bundle.sh at %s\n' "$ref" "$revision" >> "$out/images.lock"
  printf 'image:\n  repository: gpu-ttv/gpu-ttv\n  digest: "%s"\n' "$digest" > "$out/charts/values-image.yaml"
  sbom_archives+=("$out/images/gpu-ttv.tar")
fi
"$here/sbom.sh" --out "$out/sbom" --platform "$platform" "${sbom_archives[@]+"${sbom_archives[@]}"}"

(cd "$out" && find . -type f ! -name SHA256SUMS | LC_ALL=C sort | sed 's|^\./||' \
  | while read -r f; do sha256_lines "$f"; done > SHA256SUMS)
echo "bundle: $out ($(du -sh "$out" | cut -f1)), $(wc -l < "$out/SHA256SUMS" | tr -d ' ') files checksummed"
