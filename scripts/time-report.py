#!/usr/bin/env python3
"""Отчёт «работа/ожидание» по Claude Code и Codex, с разрезом по проектам.

Источники:
  Claude Code — ~/.claude/time-tracking.jsonl (хуки track-time.sh):
                turn = UserPromptSubmit → Stop.
  Codex       — ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl (его собственные логи):
                turn = task_started → task_complete. Хуки Codex не используются —
                он исполняет только доверенные, доверие выдаётся интерактивно.

Что чистится:
  * дубли турнов Codex — resume/fork копируют историю в новый rollout-файл,
    один и тот же turn_id встречается в нескольких файлах;
  * турны длиннее TURN_CAP — это сон ноутбука или ожидание апрува внутри турна,
    а не работа модели; обрезаются, объём обрезки печатается явно;
  * паузы между турнами длиннее WAIT_CAP — «отошёл», в wait не идут.

usage: time-report.py [--days N] [--sessions] [--app claude|codex]
"""
import argparse
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

CLAUDE_LOG = Path.home() / ".claude" / "time-tracking.jsonl"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
WAIT_CAP = 3600        # пауза между турнами дольше часа = «отошёл»
TURN_CAP = 3600        # турн дольше часа = внутри был сон/ожидание апрува
OPEN_TURN_MAX = 7200   # незакрытый турн старше 2ч = оборванная сессия
SCAN_SLACK_DAYS = 7    # запас сканирования, чтобы поймать оригиналы resume-сессий
# служебные блоки, которые Codex шлёт как user-сообщения перед настоящим промптом
SERVICE_BLOCKS = ("<recommended_plugins", "<environment_context", "<permissions",
                  "<user_instructions", "<available_skills")


def fmt(sec):
    sec = int(sec)
    if sec >= 3600:
        return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"
    if sec >= 60:
        return f"{sec // 60}m"
    return f"{sec}s"


def clean(text):
    return " ".join(str(text).split())[:200]


def strip_tags(text):
    """Промпт companion-задач обёрнут в теги (<task>…</task>) — достаём текст."""
    out, depth = [], 0
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return clean("".join(out))


def claude_turns(now):
    """[(start, end, cwd, prompt, session)] по событиям хуков."""
    sessions = {}
    if not CLAUDE_LOG.exists():
        return []
    with CLAUDE_LOG.open() as f:
        for line in f:
            try:
                e = json.loads(line)
                if e.get("app") != "claude":
                    continue
                ts = datetime.strptime(e["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            s = sessions.setdefault(e.get("session"), {"events": [], "cwd": None, "prompt": None})
            s["events"].append((ts, e["event"]))
            if s["cwd"] is None and e.get("cwd"):
                s["cwd"] = e["cwd"]
            if s["prompt"] is None and e.get("prompt"):
                s["prompt"] = clean(e["prompt"])

    turns = []
    for key, s in sessions.items():
        open_start = None
        for ts, ev in sorted(s["events"]):
            if ev == "UserPromptSubmit" and open_start is None:
                open_start = ts
            elif ev in ("Stop", "SessionEnd") and open_start is not None:
                turns.append((open_start, ts, s["cwd"], s["prompt"] or "", f"cc:{key}"))
                open_start = None
        if open_start is not None and (now - open_start).total_seconds() <= OPEN_TURN_MAX:
            turns.append((open_start, now, s["cwd"], s["prompt"] or "", f"cc:{key}"))
    return turns


def codex_turns(scan_from):
    """[(start, end, cwd, prompt, session)] из rollout-логов, с дедупом по turn_id."""
    if not CODEX_SESSIONS.exists():
        return []
    files = []
    for path in CODEX_SESSIONS.glob("*/*/*/rollout-*.jsonl"):
        try:
            y, m, d = path.parent.parts[-3:]
            if datetime(int(y), int(m), int(d)).date() >= scan_from:
                files.append(path)
        except (ValueError, IndexError):
            continue

    seen = set()
    turns = []
    # по имени = хронологически: оригинал сессии встречается раньше её resume-копий
    for path in sorted(files):
        cwd = prompt = None
        local = []
        try:
            with path.open() as f:
                for line in f:
                    if '"session_meta"' in line:
                        cwd = json.loads(line).get("payload", {}).get("cwd") or cwd
                    elif '"task_complete"' in line:
                        p = json.loads(line).get("payload", {})
                        if p.get("type") != "task_complete":
                            continue
                        start, done = p.get("started_at"), p.get("completed_at")
                        if not (start and done):
                            continue
                        tid = p.get("turn_id") or f"{start}-{done}"
                        if tid in seen:
                            continue
                        seen.add(tid)
                        local.append((start, done))
                    elif prompt is None and '"role":"user"' in line:
                        p = json.loads(line).get("payload", {})
                        if p.get("role") != "user":
                            continue
                        txt = "".join(c.get("text", "") for c in p.get("content", []))
                        txt = txt.lstrip()
                        if not txt or txt.startswith(SERVICE_BLOCKS):
                            continue
                        # у companion-задач промпт завёрнут в теги (<task>…)
                        prompt = strip_tags(txt) if txt.startswith("<") else clean(txt)
                        prompt = prompt or None
        except (OSError, json.JSONDecodeError):
            continue
        for start, done in local:
            turns.append((datetime.fromtimestamp(start, timezone.utc),
                          datetime.fromtimestamp(done, timezone.utc),
                          cwd, prompt or "", f"cx:{path.name}"))
    return turns


def main():
    ap = argparse.ArgumentParser(description="Работа/ожидание: Claude Code + Codex")
    ap.add_argument("--days", type=int, default=7, help="период в днях (default 7)")
    ap.add_argument("--sessions", action="store_true", help="детализация по сессиям")
    ap.add_argument("--app", choices=["claude", "codex"], help="фильтр по инструменту")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    local_tz = datetime.now().astimezone().tzinfo
    cutoff = (datetime.now(local_tz) - timedelta(days=args.days - 1)).date()

    raw = []
    if args.app != "codex":
        raw += [t + ("claude",) for t in claude_turns(now)]
    if args.app != "claude":
        raw += [t + ("codex",) for t in codex_turns(cutoff - timedelta(days=SCAN_SLACK_DAYS))]

    # обрезка аномальных турнов + отбор по окну
    turns, capped_n, capped_sec = [], 0, 0.0
    for start, end, cwd, prompt, session, app in raw:
        if start.astimezone(local_tz).date() < cutoff:
            continue
        dur = (end - start).total_seconds()
        if dur > TURN_CAP:
            capped_n += 1
            capped_sec += dur - TURN_CAP
            dur = TURN_CAP
        turns.append({"date": start.astimezone(local_tz).date(), "start": start, "end": end,
                      "work": dur, "cwd": cwd, "prompt": prompt, "session": session, "app": app})
    if not turns:
        print(f"Нет данных за {args.days} дн.")
        return

    # ожидание = пауза между соседними турнами внутри одной сессии
    waits = defaultdict(float)
    by_session = defaultdict(list)
    for t in turns:
        by_session[t["session"]].append(t)
    for key, items in by_session.items():
        items.sort(key=lambda t: t["start"])
        for prev, cur in zip(items, items[1:]):
            gap = (cur["start"] - prev["end"]).total_seconds()
            if 0 <= gap <= WAIT_CAP:
                waits[id(cur)] = gap
    for t in turns:
        t["wait"] = waits.get(id(t), 0.0)

    by_day = defaultdict(list)
    for t in turns:
        by_day[t["date"]].append(t)

    tw = tt = 0.0
    for day in sorted(by_day, reverse=True):
        day_turns = by_day[day]
        print(f"── {day} ──")
        groups = defaultdict(lambda: {"work": 0.0, "wait": 0.0, "turns": 0, "sess": set()})
        for t in day_turns:
            g = groups[(Path(t["cwd"]).name if t["cwd"] else "?", t["app"])]
            g["work"] += t["work"]
            g["wait"] += t["wait"]
            g["turns"] += 1
            g["sess"].add(t["session"])
        for (proj, app), g in sorted(groups.items(), key=lambda kv: -kv[1]["work"]):
            print(f"  {app:6} {proj:24} work {fmt(g['work']):>7}  wait {fmt(g['wait']):>7}  "
                  f"{g['turns']} turns, {len(g['sess'])} sess")
        if args.sessions:
            per_sess = defaultdict(lambda: {"work": 0.0, "wait": 0.0, "n": 0, "first": None, "last": None,
                                            "cwd": None, "prompt": ""})
            for t in day_turns:
                s = per_sess[t["session"]]
                s["work"] += t["work"]; s["wait"] += t["wait"]; s["n"] += 1
                s["first"] = min(s["first"] or t["start"], t["start"])
                s["last"] = max(s["last"] or t["end"], t["end"])
                s["cwd"] = s["cwd"] or t["cwd"]
                s["prompt"] = s["prompt"] or t["prompt"]
            for key, s in sorted(per_sess.items(), key=lambda kv: kv[1]["first"]):
                app = "claude" if key.startswith("cc:") else "codex"
                proj = Path(s["cwd"]).name if s["cwd"] else "?"
                print(f"    {s['first'].astimezone(local_tz):%H:%M}–{s['last'].astimezone(local_tz):%H:%M} "
                      f"[{app}] {proj}: work {fmt(s['work'])}, wait {fmt(s['wait'])}, {s['n']}t  "
                      f"«{s['prompt'][:70]}»")
        dw = sum(t["work"] for t in day_turns)
        dt_ = sum(t["wait"] for t in day_turns)
        tw += dw; tt += dt_
        print(f"  день: work {fmt(dw)}, wait {fmt(dt_)}, {len(day_turns)} turns")

    sess_total = len({t["session"] for t in turns})
    print(f"\nИТОГО {args.days} дн.: work {fmt(tw)}, wait {fmt(tt)}, {len(turns)} turns, {sess_total} сессий")
    if capped_n:
        print(f"({capped_n} турнов длиннее {TURN_CAP // 60} мин обрезаны — это сон ноутбука "
              f"или ожидание апрува внутри турна; отброшено {fmt(capped_sec)})")


if __name__ == "__main__":
    main()
