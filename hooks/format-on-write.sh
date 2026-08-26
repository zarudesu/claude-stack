#!/bin/bash
# PostToolUse (Edit|Write): автоформат только что записанного файла по расширению,
# только если форматтер установлен. Best-effort, никогда не блокирует.
path=$(jq -r '.tool_input.file_path // empty')
[ -z "$path" ] && exit 0
[ -f "$path" ] || exit 0
case "$path" in
  *.tf|*.tfvars)  command -v terraform >/dev/null 2>&1 && terraform fmt "$path" >/dev/null 2>&1 ;;
  *.sh|*.bash)    command -v shfmt >/dev/null 2>&1 && shfmt -w "$path" >/dev/null 2>&1 ;;
  *.py)           if command -v ruff >/dev/null 2>&1; then ruff format "$path" >/dev/null 2>&1
                  elif command -v black >/dev/null 2>&1; then black -q "$path" >/dev/null 2>&1; fi ;;
  *.js|*.jsx|*.ts|*.tsx|*.json|*.css|*.scss|*.html|*.md|*.yml|*.yaml)
                  command -v prettier >/dev/null 2>&1 && prettier --write "$path" >/dev/null 2>&1 ;;
esac
exit 0
