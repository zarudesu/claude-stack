#!/bin/bash
# PreToolUse guard (matcher: Bash): subagents must not use shell commands to
# write to protected Claude-config paths (agents/skills/hooks, settings.json,
# CLAUDE.md). Companion to protected-paths-guard.sh (Edit/Write/NotebookEdit).
# Blocks only when a write-capable operation TARGETS a protected path
# (redirect into it, cp/mv/ln/install with it as destination, sed -i/tee/rm/
# chmod/chown/truncate/dd on it, python/heredoc opening it for write, or
# `cd` into a protected dir followed by any write operator).
# Read-only mentions (ls/cat/rg with 2>&1, 2>/dev/null, cp FROM it, python -c
# reading it, heredoc that only reads) pass.
# Detection: agent_id present in hook input => subagent.
# Override: PROTECTED_PATHS_ALLOW=1 in the environment of the claude process.
input=$(cat)
agent=$(printf '%s' "$input" | jq -r '.agent_id // empty')
[ -z "$agent" ] && exit 0
[ "${PROTECTED_PATHS_ALLOW:-0}" = "1" ] && exit 0

cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // empty')
[ -z "$cmd" ] && exit 0

esc_home="${HOME//./\\.}"
home="(${esc_home}/\.claude|~/\.claude|\"?\\\$\\{?HOME\\}?\"?/\.claude)"
prot="${home}/(agents|skills|hooks)(/[^[:space:];|&'\"]*)?|${home}/settings\.json[^[:space:];|&'\"]*|${home}/CLAUDE\.md"
P="(${prot})"
Q="['\"]?"

# 1. redirect whose target is a protected path: > P, >> P, 2> P, &> P
r1="(^|[^<])>>?[[:space:]]*${Q}${P}"
# 2. cp/mv/ln/install/rsync with protected path as LAST argument (destination), trailing redirects allowed
r2="(^|[;|&(][[:space:]]*|[[:space:]])(cp|mv|ln|install|rsync)[[:space:]][^;|&]*[[:space:]]${Q}${P}${Q}[[:space:]]*([0-9]*>|;|\||&|$)"
# 2t. protected path given via a target/output option: cp -t, --target-directory, curl -o, wget -O, tar -C, unzip -d
r2t="(^|[;|&(][[:space:]]*|[[:space:]])((cp|mv|ln|install|rsync)[[:space:]][^;|&]*(-t|--target-directory)|curl[[:space:]][^;|&]*(-o|--output)|wget[[:space:]][^;|&]*(-O|--output-document)|tar[[:space:]][^;|&]*(-C|--directory)|unzip[[:space:]][^;|&]*-d)([[:space:]]+|=)${Q}${P}"
# 3. in-place/destructive tools with a protected path anywhere in the same simple command
r3="(^|[;|&(][[:space:]]*|[[:space:]])(sed[[:space:]]+-[a-zA-Z]*i|perl[[:space:]]+-[a-zA-Z]*i|tee([[:space:]]+-[a-z]+)*|truncate|chmod|chown|rm|dd|shred|touch)[[:space:]][^;|&]*${P}"
# 4. scripted write: protected path opened for write / write_text / unlink / rename in the same line
r4="${P}${Q}[[:space:]]*,[[:space:]]*(mode=)?['\"](w|a|wb|ab|w\+|a\+|r\+)['\"]|(write_text|write_bytes|unlink|os\.remove|os\.rename|os\.replace|shutil\.(copy|copy2|copyfile|move)|rename|remove)\([^)]*${P}"
# 5. cd into ~/.claude (or protected subdir) + any write operator afterwards (relative-path writes)
r5a="(^|[;|&][[:space:]]*)cd[[:space:]]+${Q}${home}(/(agents|skills|hooks))?/?${Q}[[:space:]]*(;|&&|\|\||$)"
r5b="((^|[^<>&0-9])>>?[[:space:]]*[^&/[:space:]]|(^|[^<>&0-9])>>?[[:space:]]*/[^d]|[[:space:]]tee[[:space:]]|sed[[:space:]]+-[a-zA-Z]*i|(^|[;|&(][[:space:]]*|[[:space:]])(mv|cp|rm|chmod|chown|install|ln|truncate|dd)[[:space:]])"

# 6. inline script (heredoc / -c / -e) that mentions a protected path AND opens
#    something for write — catches `p='...'; open(p,'w')` where the path is in a variable
r6w="['\"](w|a|wb|ab|w\\+|a\\+)['\"]|write_text|write_bytes|os\\.(remove|rename|replace)|shutil\\.(copy|copy2|copyfile|move)|\\.unlink\\("
r6s="<<|python[23]?[[:space:]]+-c|perl[[:space:]]+-e|ruby[[:space:]]+-e"

block=0
for re in "$r1" "$r2" "$r2t" "$r3" "$r4"; do
  printf '%s' "$cmd" | grep -Eq "$re" && block=1 && break
done
if [ "$block" = 0 ] && printf '%s' "$cmd" | grep -Eq "$r5a" && printf '%s' "$cmd" | grep -Eq "$r5b"; then
  block=1
fi
if [ "$block" = 0 ] && printf '%s' "$cmd" | grep -Eq "$P" && printf '%s' "$cmd" | grep -Eq "$r6s" && printf '%s' "$cmd" | grep -Eq "$r6w"; then
  block=1
fi

if [ "$block" = 1 ]; then
  echo "Protected Claude-config path: subagents must not WRITE under ~/.claude/{agents,skills,hooks}, settings.json, CLAUDE.md — propose the change in your report; main relays to user. Read-only access is fine: use the Read tool or plain cat/ls/rg. (PROTECTED_PATHS_ALLOW=1 works only in the environment of the claude process, not as a command prefix.)" >&2
  exit 2
fi
exit 0
