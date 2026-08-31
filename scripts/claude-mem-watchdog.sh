#!/bin/bash
# Kills claude-mem headless worker children (spawned by worker-service.cjs)
# that have been running longer than THRESHOLD_SECONDS. Never touches the
# daemon itself or any `claude` process whose parent isn't worker-service.cjs.
# Run from cron every few minutes, e.g.: */5 * * * * $HOME/.claude/scripts/claude-mem-watchdog.sh

LOG_FILE="$HOME/.claude/logs/claude-mem-watchdog.log"
mkdir -p "$(dirname "$LOG_FILE")"

THRESHOLD_SECONDS=$((15 * 60))

# Convert a `ps -o etime=` value ([[dd-]hh:]mm:ss) to seconds.
etime_to_seconds() {
    local etime="$1" days=0 hh=0 mm=0 ss=0 rest="$1" colons

    if [[ "$rest" == *-* ]]; then
        days="${rest%%-*}"
        rest="${rest#*-}"
    fi

    colons=$(tr -cd ':' <<< "$rest" | wc -c)
    if [[ "$colons" -eq 2 ]]; then
        IFS=: read -r hh mm ss <<< "$rest"
    else
        IFS=: read -r mm ss <<< "$rest"
    fi

    echo $(( 10#$days * 86400 + 10#$hh * 3600 + 10#$mm * 60 + 10#$ss ))
}

ps -eo pid=,ppid=,etime=,args= | while read -r pid ppid etime rest; do
    [[ "$rest" =~ \.local/bin/claude ]] || continue

    ppid_cmd=$(ps -o args= -p "$ppid" 2>/dev/null)
    [[ "$ppid_cmd" =~ worker-service\.cjs ]] || continue

    seconds=$(etime_to_seconds "$etime")
    if (( seconds > THRESHOLD_SECONDS )); then
        kill -TERM "$pid" 2>/dev/null
        sleep 5
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null
        fi
        printf '%s pid=%s etime=%s killed\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$pid" "$etime" >> "$LOG_FILE"
    fi
done
