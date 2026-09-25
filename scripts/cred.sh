#!/bin/bash
# cred.sh — hand a credentials/*.env value to a child process's env, never
# print it. Sources: <CRED_DIR>/*.env plus <CRED_DIR>/*-openrc.sh (KEY=value /
# export KEY=value lines). CRED_DIR defaults to ~/.config/credentials.
#
# Usage:
#   cred.sh list                          — key names + source file, no values
#   cred.sh run KEY[,KEY2,...] -- CMD...  — export values, exec CMD
set -euo pipefail
CRED_DIR="${CRED_DIR:-$HOME/.config/credentials}"

usage() {
  echo "usage: cred.sh list | cred.sh run KEY[,KEY2,...] -- CMD..." >&2
  exit 1
}

_files() {
  local f
  for f in "$CRED_DIR"/*.env "$CRED_DIR"/*-openrc.sh; do
    [ -f "$f" ] && printf '%s\n' "$f"
  done
}

_find_value() {
  local key="$1" f line
  while IFS= read -r f; do
    line=$(grep -E "^(export[[:space:]]+)?${key}=" "$f" | head -n1) || true
    if [ -n "$line" ]; then
      line="${line#export }"
      line="${line#"${key}"=}"
      line="${line%\"}"; line="${line#\"}"
      line="${line%\'}"; line="${line#\'}"
      printf '%s' "$line"
      return 0
    fi
  done < <(_files)
  return 1
}

case "${1:-}" in
  list)
    _files | while IFS= read -r f; do
      grep -oE '^(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*=' "$f" \
        | sed -E 's/^export[[:space:]]+//; s/=$//' \
        | while IFS= read -r k; do printf '%-40s (%s)\n' "$k" "$(basename "$f")"; done
    done | sort -u
    ;;
  run)
    shift
    keys_csv="${1:-}"
    [ -z "$keys_csv" ] && usage
    shift
    [ "${1:-}" = "--" ] || usage
    shift
    [ "$#" -eq 0 ] && usage
    IFS=',' read -r -a keys <<< "$keys_csv"
    for k in "${keys[@]}"; do
      v=$(_find_value "$k") || { echo "cred.sh: key not found: $k" >&2; exit 1; }
      export "$k=$v"
    done
    exec "$@"
    ;;
  *)
    usage
    ;;
esac
