#!/bin/zsh
# One-shot grepai index resync for ~/Projects.
# `grepai watch --background` (0.37) kills its child if the 2.9 GB gob index isn't loaded within 30 s,
# and a permanently running watcher pins several GB of RAM, so we run the foreground watcher
# just long enough to finish the incremental scan, then stop it.
export LC_ALL=C
ROOT=~/Projects
LOG=~/Library/Logs/grepai/grepai-resync-$(date +%Y-%m-%d-%H%M).log
MAX=${GREPAI_RESYNC_MAX:-14400}   # seconds; full rebuild takes ~2.5 h, incremental sync minutes
cd "$ROOT" || exit 1
if pgrep -f "grepai watch" >/dev/null; then echo "grepai watch already running (pid $(pgrep -f 'grepai watch' | head -1))"; exit 0; fi
rm -f .grepai/index.gob.lock .grepai/symbols.gob.lock
nohup grepai watch --no-ui > "$LOG" 2>&1 &
PID=$!
echo "$(date '+%F %T') resync started pid=$PID log=$LOG"
t=0
while (( t < MAX )); do
  sleep 15; t=$((t+15))
  kill -0 $PID 2>/dev/null || { echo "$(date '+%F %T') watcher died — see $LOG"; tail -5 "$LOG"; exit 1; }
  if tr '\r' '\n' < "$LOG" | rg -q "Watching for changes"; then
    kill $PID; sleep 3
    echo "$(date '+%F %T') resync done in ${t}s; index: $(du -h .grepai/index.gob | cut -f1)"
    tr '\r' '\n' < "$LOG" | rg -o 'Found [0-9]+ files|Embedding.*100%.*' | tail -2
    exit 0
  fi
done
kill $PID; echo "$(date '+%F %T') resync hit MAX=${MAX}s, stopped"; exit 1
