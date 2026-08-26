#!/bin/bash
# PreToolUse gate: main thread is decider-only — edits go through subagents.
# Subagent calls carry agent_id in hook input and pass through untouched.
# Disable for a session: DECIDER_MODE=0 claude
input=$(cat)
agent=$(printf '%s' "$input" | jq -r '.agent_id // empty')
[ -n "$agent" ] && exit 0
[ "${DECIDER_MODE:-1}" = "0" ] && exit 0
tool=$(printf '%s' "$input" | jq -r '.tool_name')
echo "Main thread is decider-only: delegate this $tool to a subagent (sonnet/haiku specialist from ~/.claude/agents or general-purpose). Edit directly only if the user explicitly asked for a hands-on change in this session (then rerun with DECIDER_MODE=0)." >&2
exit 2
