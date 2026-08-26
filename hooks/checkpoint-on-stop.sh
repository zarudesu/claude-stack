#!/bin/bash
# Stop hook: атомарный git-checkpoint. `git stash create` строит stash-объект
# БЕЗ единого касания рабочего дерева/индекса (нет окна пустого дерева, staging
# не сбрасывается), затем `git stash store` регистрирует его в stash list.
# Восстановление: git stash list / git stash apply <ref>.
# Примечание: stash create НЕ включает untracked-файлы (плата за атомарность).
# Ротация: держим не больше KEEP последних claude-checkpoint (старые дропаются
# порциями по 50 за вызов, чужие/ручные stash не трогаем).
cwd=$(jq -r '.cwd // empty')
[ -n "$cwd" ] && cd "$cwd" 2>/dev/null
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

sha=$(git stash create 2>/dev/null)
if [ -n "$sha" ]; then
  git stash store -m "claude-checkpoint-$(date +%Y%m%d-%H%M%S)" "$sha" >/dev/null 2>&1
fi

KEEP=20
i=0
while [ "$i" -lt 50 ]; do
  count=$(git stash list 2>/dev/null | grep -c 'claude-checkpoint')
  [ "$count" -le "$KEEP" ] && break
  oldest=$(git stash list | grep 'claude-checkpoint' | tail -1 | cut -d: -f1)
  git stash drop "$oldest" >/dev/null 2>&1 || break
  i=$((i+1))
done
exit 0
