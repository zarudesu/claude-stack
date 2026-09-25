#!/bin/zsh
# per-repo status: dirty files, unpushed, branch, last commit age, worktrees, stashes
# Scans repos one and two levels deep under $PROJECTS_DIR (default ~/Projects).
export LC_ALL=C
ROOT=${PROJECTS_DIR:-~/Projects}
cd "$ROOT" || exit 1
repos=()
for d in */ */*/; do
  [ -d "$d/.git" ] || continue
  case "$d" in _archive/*|*/.worktrees/*) continue;; esac
  repos+=("${d%/}")
done
printf "%-40s %-32s %5s %6s %5s %4s %s\n" repo branch dirty ahead wt stash last
for r in ${(u)repos}; do
  cd "$ROOT/$r"
  br=$(git symbolic-ref --short HEAD 2>/dev/null || echo "DETACHED@$(git rev-parse --short HEAD 2>/dev/null)")
  dirty=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
  up=$(git rev-parse --abbrev-ref @{u} 2>/dev/null)
  if [ -n "$up" ]; then ahead=$(git rev-list --count @{u}..HEAD 2>/dev/null); behind=$(git rev-list --count HEAD..@{u} 2>/dev/null); a="+$ahead/-$behind"; else a="noup"; fi
  wt=$(( $(git worktree list 2>/dev/null | wc -l) - 1 ))
  st=$(git stash list 2>/dev/null | wc -l | tr -d ' ')
  last=$(git log -1 --format='%cs' 2>/dev/null)
  printf "%-40s %-32s %5s %6s %5s %4s %s\n" "$r" "$br" "$dirty" "$a" "$wt" "$st" "$last"
done
