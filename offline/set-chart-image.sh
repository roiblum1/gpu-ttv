#!/usr/bin/env bash
# Set the image a gpu-ttv chart's values.yaml points at, keeping its comments (the release does this
# before packaging). Fails unless each of the three lines was found and changed.
#
#   offline/set-chart-image.sh VALUES_YAML REGISTRY REPOSITORY sha256:<64 hex>
set -euo pipefail
[ $# -eq 4 ] || { echo "usage: $0 VALUES_YAML REGISTRY REPOSITORY DIGEST" >&2; exit 2; }
values=$1 registry=$2 repository=$3 digest=$4
[[ $digest =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "set-chart-image: bad digest: $digest" >&2; exit 2; }
for v in "$registry" "$repository"; do
  [[ $v =~ ^[A-Za-z0-9._/:-]+$ ]] || { echo "set-chart-image: bad value: $v" >&2; exit 2; }
done
tmp=$(mktemp)
sed -e "s|^  registry: .*|  registry: $registry|" \
    -e "s|^  repository: .*|  repository: $repository|" \
    -e "s|^  digest: .*|  digest: \"$digest\"|" "$values" > "$tmp"
for line in "  registry: $registry" "  repository: $repository" "  digest: \"$digest\""; do
  [ "$(grep -cxF -- "$line" "$tmp")" = 1 ] || { echo "set-chart-image: expected one '$line'" >&2; rm -f "$tmp"; exit 1; }
done
cat "$tmp" > "$values" && rm -f "$tmp"
