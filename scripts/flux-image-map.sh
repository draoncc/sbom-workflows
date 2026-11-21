#!/usr/bin/env bash
# Run step of the flux-image-map workflow: maps the images deployed from repo's
# Flux roots and prints them as a table, to stdout and the step summary.
# usage: flux-image-map.sh <tooling-dir> <repo-dir>
# env: ROOTS VARS KUBE_VERSION RESOLVE_DIGESTS STRICT RUNNER_TEMP GITHUB_STEP_SUMMARY
set -euo pipefail

tooling="$1" repo="$2"

args=("$repo" --format table)
while IFS= read -r root; do
  [[ -n "${root// }" ]] && args+=(--root "$root")
done <<< "$ROOTS"
# Without a root the script would discover roots instead.
(( ${#args[@]} > 3 )) || args+=(--root .)
if [[ -n "${VARS// }" ]]; then
  printf '%s\n' "$VARS" > "$RUNNER_TEMP/vars.env"
  args+=(--vars "$RUNNER_TEMP/vars.env")
fi
[[ -n "$KUBE_VERSION" ]] && args+=(--kube-version "$KUBE_VERSION")
[[ "$RESOLVE_DIGESTS" == true ]] && args+=(--resolve-digests)
[[ "$STRICT" == true ]] && args+=(--strict)

# Capture the exit code so the summary is written even when the script fails.
code=0
nix develop "./$tooling" --command uv run --locked --script "$tooling/scripts/image_map.py" "${args[@]}" \
  | tee "$RUNNER_TEMP/image-map.txt" || code=$?

{
  echo '```'
  cat "$RUNNER_TEMP/image-map.txt"
  echo '```'
} >> "$GITHUB_STEP_SUMMARY"
exit "$code"
