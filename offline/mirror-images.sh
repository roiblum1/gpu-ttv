#!/usr/bin/env bash
# Connected side: copy every image of offline/images.lock, all architectures, into tar archives of
# skopeo's dir: layout that keep the index digest, so the internal registry serves the pinned digests.
#
#   offline/mirror-images.sh [--out DIR] [--only SUBSTRING]
#
# Writes DIR/images/<name>.tar and DIR/images/index.txt (`<ref> <archive>` per line). Each archive's
# top-level manifest is hashed and compared with the pinned digest; a mismatch stops the run.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=offline/lib.sh
. "$here/lib.sh"

out="$here/bundle"
only=""
while [ $# -gt 0 ]; do
  case "$1" in
    --out) out=$2; shift 2 ;;
    --only) only=$2; shift 2 ;;
    *) echo "usage: $0 [--out DIR] [--only SUBSTRING]" >&2; exit 2 ;;
  esac
done
mkdir -p "$out/images"
out=$(cd "$out" && pwd)
export SKOPEO_WORKDIR=$out
index="$out/images/index.txt"
touch "$index"

lock_refs "$here/images.lock" | while read -r ref; do
  case "$ref" in *"$only"*) ;; *) continue ;; esac
  archive="images/$(ref_file "$ref").tar"
  echo "mirror: $ref"
  rm -f "$out/$archive"
  archive_image "docker://$(ref_pinned "$ref")" "$out/$archive"
  got=$(archive_digest "$out/$archive")
  if [ "$got" != "$(ref_digest "$ref")" ]; then
    echo "mirror: $ref archived as $got, not the pinned digest" >&2
    exit 1
  fi
  grep -v "^$ref " "$index" > "$index.tmp" || true
  printf '%s %s\n' "$ref" "$archive" >> "$index.tmp"
  mv "$index.tmp" "$index"
done
echo "mirror: $(wc -l < "$index" | tr -d ' ') images listed in $index"
