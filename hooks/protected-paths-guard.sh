#!/bin/bash
# PreToolUse guard (matcher: Edit|Write|NotebookEdit): subagents must not write
# directly to protected Claude-config paths (agents/skills/settings/CLAUDE.md/hooks).
# Detection: agent_id present in hook input => subagent.
# Override: PROTECTED_PATHS_ALLOW=1
input=$(cat)
agent=$(printf '%s' "$input" | jq -r '.agent_id // empty')
[ -z "$agent" ] && exit 0
[ "${PROTECTED_PATHS_ALLOW:-0}" = "1" ] && exit 0

path=$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')
[ -z "$path" ] && exit 0

resolved=$(realpath "$path" 2>/dev/null)
if [ -z "$resolved" ]; then
  # file doesn't exist yet (new Write) — resolve its parent dir instead so
  # ../ traversal is still caught, then re-append the filename.
  dir=$(dirname "$path")
  base=$(basename "$path")
  resolved_dir=$(realpath "$dir" 2>/dev/null)
  if [ -n "$resolved_dir" ]; then
    resolved="$resolved_dir/$base"
  else
    resolved="$path"
  fi
fi

claude_dir="$HOME/.claude"
case "$resolved" in
  "$claude_dir"/agents/*|"$claude_dir"/skills/*|"$claude_dir"/settings.json|"$claude_dir"/CLAUDE.md|"$claude_dir"/hooks/*)
    echo "Protected Claude-config path: subagents must PROPOSE this change in their report, not apply it. Main relays to user. Override: PROTECTED_PATHS_ALLOW=1." >&2
    exit 2
    ;;
esac
exit 0
