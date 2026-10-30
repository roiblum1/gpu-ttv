#!/usr/bin/env bash
# Runs every check and keeps going after a failure, so one pass reports all problems.
set -u
cd "$(dirname "$0")/../../.." || exit 2

if [ ! -f pyproject.toml ]; then
  echo "gate: no pyproject.toml yet; the project is not scaffolded."
  exit 2
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "gate: uv is not installed (brew install uv)."
  exit 2
fi

run_tests=1
for arg in "$@"; do
  [ "$arg" = "--no-tests" ] && run_tests=0
done

declare -a names results
step() {
  local name="$1"; shift
  echo "=== $name"
  "$@"
  local rc=$?
  if [ "$rc" = 0 ]; then results+=("PASS")
  elif [ "$name" = "pytest" ] && [ "$rc" = 5 ]; then results+=("NO TESTS")
  else results+=("FAIL"); fi
  names+=("$name")
}

step "ruff check" uv run ruff check .
step "ruff format --check" uv run ruff format --check .
step "ty check" uv run ty check src tests
[ "$run_tests" = 1 ] && step "pytest" uv run pytest -q

echo
echo "=== summary"
failed=0
for i in "${!names[@]}"; do
  printf '%-22s %s\n' "${names[$i]}" "${results[$i]}"
  [ "${results[$i]}" = "FAIL" ] && failed=1
done
exit "$failed"
