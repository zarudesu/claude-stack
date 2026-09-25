#!/usr/bin/env python3
"""Stop-хук doc-model: write-back как механизм, а не дисциплина.

Сессия за ход изменила файлы репозитория, но ни один дом заполнения (план/handoff в pm-каталоге,
канон статуса — `writeback.homes` в config.json) не тронут → один раз за сессию хук не даёт
закончить ход и просит write-back (`additionalContext`: не ошибка, а подсказка). Второй раз не
спрашивает; `stop_hook_active` — не спрашивает; правки других окон отсекаются по времени старта
сессии (маркер `.git/doc-model/session-<id>.start` пишет orient.sh) — не идеально, поэтому только
один раз и только подсказка. Отключить: DOC_MODEL_NO_WRITEBACK=1 или "writeback": {"enabled": false}.
"""
import fnmatch, json, os, subprocess, sys

DEFAULT_HOMES = [".claude/pm/**"]
SKIP = [".git/**", ".claude/doc-model/**", "**/__pycache__/**", "**/.DS_Store"]


def load(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def matches(path, patterns):
    return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(path, p.replace("/**", "/*")) for p in patterns)


def main():
    if os.environ.get("DOC_MODEL_NO_WRITEBACK"):
        return 0
    hook = load_stdin()
    if hook.get("stop_hook_active"):
        return 0
    root = os.environ.get("CLAUDE_PROJECT_DIR") or git_root(hook.get("cwd") or os.getcwd())
    if not root:
        return 0
    cfg = load(os.path.join(root, ".claude", "doc-model", "config.json"), {})
    wb = cfg.get("writeback") or {}
    if wb.get("enabled") is False:
        return 0
    homes = DEFAULT_HOMES + list(wb.get("homes", []))
    sid = hook.get("session_id") or "unknown"
    gitdir = git_dir(root)   # в worktree .git — файл; маркер — в git-dir этого worktree (как в orient.sh)
    if not gitdir:
        return 0
    mark_dir = os.path.join(gitdir, "doc-model")
    start = os.path.join(mark_dir, f"session-{sid}.start")
    nudged = os.path.join(mark_dir, f"session-{sid}.nudged")
    if os.path.exists(nudged):
        return 0
    if not os.path.exists(start):
        os.makedirs(mark_dir, exist_ok=True)
        open(start, "w").close()   # без старта окно правок неизвестно — считаем с этого момента
        return 0
    since = os.path.getmtime(start)
    try:
        out = subprocess.run(["git", "-C", root, "status", "--porcelain", "--untracked-files=all"],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    changed = []
    for line in out.splitlines():
        p = line[3:].split(" -> ")[-1].strip().strip('"')
        full = os.path.join(root, p)
        if matches(p, SKIP) or not os.path.exists(full) or os.path.getmtime(full) < since:
            continue
        changed.append(p)
    work = [p for p in changed if not matches(p, homes)]
    filled = [p for p in changed if matches(p, homes)]
    if not work or filled:
        return 0
    os.makedirs(mark_dir, exist_ok=True)
    open(nudged, "w").close()
    shown = ", ".join(work[:5]) + (f" и ещё {len(work) - 5}" if len(work) > 5 else "")
    reason = ("doc-model write-back: за сессию изменены файлы репозитория (" + shown + "), "
              "но ни один дом заполнения не обновлён (" + ", ".join(homes) + "). "
              "Если правки твои — обнови шапку Next action плана своего scope или handoff и заверши; "
              "если это чужие окна или правки план не требуют — просто заверши ход. Второй раз не спрошу.")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": reason}}, ensure_ascii=False))
    return 0


def load_stdin():
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {}


def git_root(cwd):
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""



def git_dir(cwd):
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--absolute-git-dir"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


if __name__ == "__main__":
    sys.exit(main())
