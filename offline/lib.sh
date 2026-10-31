# Shared helpers for the offline scripts (sourced, not run). See offline/README.md.

# Print the SHA-256 lines of the given files, `<hex>  <name>`, as sha256sum does.
sha256_lines() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$@"; else shasum -a 256 "$@"; fi
}

# Print the SHA-256 hex digest of stdin.
sha256_stdin() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum | cut -d' ' -f1; else shasum -a 256 | cut -d' ' -f1; fi
}

# Check a SHA256SUMS file from the current directory; fails on any mismatch or missing file.
sha256_check() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum -c --quiet "$1"; else shasum -a 256 -c --quiet "$1"; fi
}

# Print every pinned reference of an images.lock file (first column, comments skipped).
lock_refs() {
  awk '!/^#/ && NF { print $1 }' "$1"
}

# Path of a reference without registry host, tag and digest: docker.io/library/python:3.12@sha256:x → library/python.
ref_path() {
  local ref=${1%@*}
  ref=${ref#*/}
  printf '%s\n' "${ref%:*}"
}

# Tag of a reference (latest when it has none).
ref_tag() {
  local ref=${1%@*}
  local last=${ref##*/}
  case "$last" in *:*) printf '%s\n' "${last##*:}" ;; *) printf 'latest\n' ;; esac
}

# The reference without its tag, as skopeo needs it: <host>/<path>@<digest>.
ref_pinned() {
  local ref=${1%@*}
  printf '%s/%s@%s\n' "${ref%%/*}" "$(ref_path "$1")" "$(ref_digest "$1")"
}

# Digest of a reference: …@sha256:x → sha256:x.
ref_digest() {
  printf '%s\n' "${1##*@}"
}

# A file-system-safe archive name for a reference.
ref_file() {
  printf '%s_%s\n' "$(ref_path "$1" | tr '/' '_')" "$(ref_digest "$1" | cut -c8-19)"
}

# Run skopeo: the local binary, else SKOPEO_IMAGE (default: the pinned skopeo image) under podman,
# with SKOPEO_WORKDIR and REGISTRY_AUTH_FILE mounted at the same paths.
run_skopeo() {
  if command -v skopeo >/dev/null 2>&1; then
    skopeo "$@"
    return
  fi
  if ! command -v podman >/dev/null 2>&1; then
    echo "offline: needs skopeo, or podman to run the skopeo image" >&2
    return 2
  fi
  local image=${SKOPEO_IMAGE:-$(lock_refs "$(dirname "${BASH_SOURCE[0]}")/images.lock" | grep -m1 '^quay.io/skopeo/stable:')}
  local relabel=""
  [ "$(uname -s)" = Linux ] && relabel=":z"
  local mounts=(-v "${SKOPEO_WORKDIR:?}:${SKOPEO_WORKDIR}${relabel}")
  if [ -n "${REGISTRY_AUTH_FILE:-}" ]; then
    mounts+=(-v "$REGISTRY_AUTH_FILE:$REGISTRY_AUTH_FILE:ro" -e "REGISTRY_AUTH_FILE=$REGISTRY_AUTH_FILE")
  fi
  podman run --rm --network host "${mounts[@]}" "$image" "$@"
}
