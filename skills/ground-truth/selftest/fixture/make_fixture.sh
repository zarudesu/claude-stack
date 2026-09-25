#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: make_fixture.sh <target-dir>" >&2
  exit 1
}

[ $# -eq 1 ] || usage

target="$1"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -e "$target" ] && [ -n "$(ls -A "$target" 2>/dev/null)" ]; then
  echo "refusing to write into non-empty target: $target" >&2
  exit 1
fi

mkdir -p "$target"
cp -R "$here/repo/." "$target/"

cd "$target"
git init -q
git config user.name "fixture"
git config user.email "fixture@example.invalid"
git add -A
git commit -q -m "fixture: initial"

echo "$target"
