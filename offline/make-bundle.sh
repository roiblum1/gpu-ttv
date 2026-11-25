#!/usr/bin/env bash
# Connected side: assemble the offline bundle for one release (offline/README.md, "Connected side").
#
#   offline/make-bundle.sh [--out DIR] [--arch x86_64|aarch64] [--image REF]
#
# Contents: third-party images (offline/images.lock, digest-preserving archives), the Python
# wheelhouse, both Helm charts, the source tree, SBOMs, and SHA256SUMS over all of it.
# --image adds the gpu-ttv image: a pushed `<host>/<path>@sha256:…` (copied with its registry
# digest, as the GitHub release does) or a local podman image (saved, so it gets a new digest).
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
    *) echo "usage: $0 [--out DIR] [--arch x86_64|aarch64] [--image REF]" >&2; exit 2 ;;
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
  # An OCI archive first, which syft reads for the SBOM; the bundle keeps the dir: archive.
  oci="$out/.sbom-input/gpu-ttv.tar"
  mkdir -p "$out/.sbom-input"
  case "$image" in
    */*@sha256:*)
      base="${image%%/*}/$(ref_path "$image")"
      run_skopeo copy --quiet --preserve-digests "docker://$image" "oci-archive:$oci"
      ;;
    *)
      base=local/gpu-ttv/gpu-ttv
      podman save --quiet --format oci-archive -o "$oci" "$image"
      ;;
  esac
  archive_image "oci-archive:$oci" "$out/images/gpu-ttv.tar"
  digest=$(archive_digest "$out/images/gpu-ttv.tar")
  case "$image" in
    *@sha256:*) [ "$digest" = "$(ref_digest "$image")" ] || {
      echo "bundle: $image archived as $digest" >&2; exit 1; } ;;
  esac
  ref="$base:$version@$digest"
  printf '%s %s\n' "$ref" "images/gpu-ttv.tar" >> "$out/images/index.txt"
  printf '%s gpu-ttv release image, bundled by make-bundle.sh at %s\n' "$ref" "$revision" >> "$out/images.lock"
  printf 'image:\n  repository: %s\n  digest: "%s"\n' "$(ref_path "$ref")" "$digest" > "$out/charts/values-image.yaml"
  sbom_archives+=("$oci")
fi
"$here/sbom.sh" --out "$out/sbom" --platform "$platform" "${sbom_archives[@]+"${sbom_archives[@]}"}"
rm -rf "$out/.sbom-input"

(cd "$out" && find . -type f ! -name SHA256SUMS | LC_ALL=C sort | sed 's|^\./||' \
  | while read -r f; do sha256_lines "$f"; done > SHA256SUMS)
echo "bundle: $out ($(du -sh "$out" | cut -f1)), $(wc -l < "$out/SHA256SUMS" | tr -d ' ') files checksummed"
