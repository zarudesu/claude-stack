#!/bin/bash
# PreToolUse guard (matcher: Bash): subagents must not use shell commands to
# write to protected Claude-config paths (agents/skills/hooks, settings.json,
# CLAUDE.md). Companion to protected-paths-guard.sh (Edit/Write/NotebookEdit) —
# this one closes the Bash-writes gap (cat >, sed -i, tee, mv, cp, rm, chmod,
# heredocs, etc).
# Detection mirrors decider-gate.sh: agent_id present in hook input => subagent.
# Override: PROTECTED_PATHS_ALLOW=1
input=$(cat)
agent=$(printf '%s' "$input" | jq -r '.agent_id // empty')
[ -z "$agent" ] && exit 0
[ "${PROTECTED_PATHS_ALLOW:-0}" = "1" ] && exit 0

cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // empty')
[ -z "$cmd" ] && exit 0

path_re="($HOME/\.claude/|~/\.claude/|\\\$HOME/\.claude/)(agents/|skills/|hooks/|settings\.json|CLAUDE\.md)"
write_re='(>|tee[[:space:]]|sed[[:space:]]+-i|mv[[:space:]]|cp[[:space:]]|rm[[:space:]]|chmod[[:space:]]|install[[:space:]]|python3[[:space:]]+-c|<<|truncate|ln[[:space:]])'

if printf '%s' "$cmd" | grep -Eq "$path_re" && printf '%s' "$cmd" | grep -Eq "$write_re"; then
  echo "Protected Claude-config path: subagents must PROPOSE this change in their report, not apply it. Main relays to user. Override: PROTECTED_PATHS_ALLOW=1." >&2
  exit 2
fi
exit 0
