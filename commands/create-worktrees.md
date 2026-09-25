---
description: Создать git worktrees для всех открытых PR (или для веток-аргументов) и убрать stale-worktrees
argument-hint: "[branch1 branch2 ... | --prs]"
allowed-tools: Bash(git*), Bash(gh*)
---

Задача: подготовить изолированные git worktrees для параллельной работы.

Контекст:
- Текущие worktrees: !`git worktree list`
- Текущая ветка: !`git rev-parse --abbrev-ref HEAD`
- Открытые PR (если есть gh): !`gh pr list --limit 30 --json number,headRefName,title 2>/dev/null || echo "gh недоступен"`

Инструкции:
1. Если аргументы `$ARGUMENTS` пусты или содержат `--prs` — создай worktree под каждый открытый PR: `git worktree add ../<repo>-<branch> <branch>` (ветку PR сначала зафетчи). Если переданы имена веток — создавай под них.
2. Имя директории worktree: `../<repo-name>-<slug-ветки>`. Не создавай дубль, если worktree уже существует (сверься со списком выше).
3. Для новых веток используй `git worktree add -b feat/<slug> ../<repo>-<slug>`.
4. После создания — покажи `git worktree list` и убери stale-записи через `git worktree prune`.
5. Не трогай worktree-и, занятые другими сессиями (см. .claude/pm/_active.md если есть).

Выведи итоговую таблицу: ветка → путь worktree → статус (created/exists/skipped).
