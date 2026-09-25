#!/usr/bin/env bash
# SessionStart: inject .claude/pm/_active.md, capped. Small file -> whole file.
# Large file -> only the header (everything before the first "- **slug**" lane line)
# plus how to read one lane by address. Never edits the file.
f=".claude/pm/_active.md"; [ -f "$f" ] || exit 0
cap=${ACTIVE_ROUTER_MAX_BYTES:-8192}
size=$(wc -c < "$f" | tr -d ' ')
echo "## Active work ($f)"
if [ "$size" -le "$cap" ]; then cat "$f"; exit 0; fi
awk '/^- \*\*[a-z0-9-]+\*\*/{exit} {print}' "$f"
lanes=$(grep -c '^- \*\*[a-z0-9-][a-z0-9-]*\*\*' "$f")
echo "…$f: $((size/1024)) КБ при норме ≤$((cap/1024)) КБ — $lanes строк лейнов не инжектированы. Строка своего лейна: rg -n '^- \*\*<slug>\*\*' $f (slug — из outcome map выше или стартового пакета)."
exit 0
