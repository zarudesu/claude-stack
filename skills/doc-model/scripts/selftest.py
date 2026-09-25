#!/usr/bin/env python3
"""Самопроверка doclint: фикстура → каждая проверка красная там, где должна → bless → зелёная → мутация → красная."""
import json, os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
LINT = os.path.join(HERE, "doclint.py")
DUP = "Это нормативное правило длиннее шестидесяти символов, которое повторяется в двух файлах подряд."


def w(root, path, text):
    p = os.path.join(root, path); os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh: fh.write(text)


def run(root, *extra):
    r = subprocess.run([sys.executable, LINT, "--root", root, "--today", "2026-09-22", *extra],
                       capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def expect(out, needle, present=True):
    ok = (needle in out) == present
    print(("ok  " if ok else "FAIL") + f"  {'есть' if present else 'нет'}: {needle}")
    return ok


def main():
    root = tempfile.mkdtemp(prefix="doclint-fx-")
    subprocess.run(["git", "-C", root, "init", "-q"], check=True)
    w(root, ".gitignore", "build/\n")
    w(root, "build/out.txt", "артефакт сборки, дома в каноне не требует\n")
    w(root, "CLAUDE.md", f"# L0\n\nКаталоги: `management/`, `docs/`.\n\n{DUP}\n\n"
                         "См. [статус](management/README.md#Статус), [нет](management/README.md#Нет-такого), `docs/missing.md`.\n"
                         "Плейсхолдеры не проверяются: `.claude/pm/<slug>/plan.md`, `org/call-*`, `~/.config/x/`.\n"
                         "Внешние/спец-пути: `git.example.io/x/y`, `archive/work-logs-YYYY-MM/`, `internal/db`, "
                         "`nowhere/`, [abs](/nonexistent-abs-path).\n")
    w(root, "management/CLAUDE.md", f"# L1\n\n{DUP}\n")
    w(root, "management/README.md", "# Портфель\n\n## Статус\n\nтекст\n")
    w(root, "management/ext.md", "внешний факт\n")
    w(root, "management/ext2.md", "внешний факт без срока\n")
    w(root, "docs/a.md", "a\n")
    w(root, "orphan/x.txt", "x\n")
    os.makedirs(os.path.join(root, "nested", ".git"))
    w(root, "nested/internal/db", "внутренний файл вложенного репо\n")
    w(root, ".claude/pm/_active.md", "1\n2\n3\n4\n5\n")
    w(root, ".claude/doc-model/config.json", json.dumps({"derived": [{"path": ".claude/pm/_active.md", "lines": 3}],
                                                          "link_ignore_prefixes": ["foo/"], "pm": {"plan_lines": 10}}))
    w(root, ".claude/pm/good/plan.md", "# good\n\nNext action: шаг\n\n## Лог\n")
    w(root, ".claude/pm/bad/plan.md", "# bad\n" + "строка лога\n" * 12)
    w(root, ".claude/pm/orphan-dir/notes.md", "x\n")
    w(root, ".claude/pm/_management/d2/notes.md", "x\n")
    w(root, ".claude/pm/_management/d1/handoff-2026-09-01.md", "h1\n")
    w(root, ".claude/pm/_management/d1/handoff-2026-09-02.md", "h2\n")
    w(root, ".claude/pm/_templates/plan.md", "служебный каталог, не единица\n")
    w(root, ".claude/rules/ok.md", "---\npaths:\n  - \"docs/**\"\n---\nправило\n")
    w(root, ".claude/rules/nopaths.md", "# правило без frontmatter\n")
    w(root, ".claude/doc-model/freshness/management__README.md.json", json.dumps({"doc": "management/README.md", "sources": ["docs/*.md"], "review_after": 14}))
    w(root, ".claude/doc-model/freshness/management__ext.md.json", json.dumps({"doc": "management/ext.md", "sources": "external", "review_after": 7, "reviewed": "2026-09-01"}))
    w(root, ".claude/doc-model/freshness/management__ext2.md.json", json.dumps({"doc": "management/ext2.md", "sources": "external"}))
    w(root, ".claude/doc-model/freshness/claude__pm___active.md.json", json.dumps({"doc": ".claude/pm/_active.md", "sources": "external", "review_after": 7}))  # путь с ведущей точкой: имя файла не должно стать скрытым

    results = []
    rc, out = run(root)
    print(f"--- базовый прогон (rc={rc}) ---")
    results += [
        expect(out, "derived `.claude/pm/_active.md`: 5 строк"),
        expect(out, "`docs/missing.md` не существует"),
        expect(out, "раздела нет в management/README.md"),
        expect(out, "без дома: `orphan/`"),
        expect(out, "без дома: `build/`", False), expect(out, "по .gitignore пропущено: build"),
        expect(out, "вложенный репозиторий `nested/`"),
        expect(out, "дубль:"),
        expect(out, ".claude/pm/bad/plan.md: в шапке (30 строк) нет `Next action`"),
        expect(out, ".claude/pm/bad/plan.md: 13 строк (лимит 10)"),
        expect(out, ".claude/pm/good/plan.md", False),
        expect(out, ".claude/pm/orphan-dir/: нет plan.md — один план на направление, каталог-сирота"),
        expect(out, ".claude/pm/_management/d2/: ни plan.md, ни handoff-*.md — закрытая работа или сирота, решает владелец"),
        expect(out, ".claude/pm/_management/d1/: handoff-файлов 2 (лимит 1)"),
        expect(out, "_templates", False),
        expect(out, "правило .claude/rules/nopaths.md без `paths:`"),
        expect(out, "правило .claude/rules/ok.md", False),
        expect(out, "stale management/README.md — никогда не ревьюился; hash: входы ни разу не зафиксированы"),
        expect(out, "stale management/ext.md — ttl: reviewed 2026-09-01, срок 7 дн"),
        expect(out, "stale management/ext2.md — никогда не ревьюился; external без review_after"),
        expect(out, "stale .claude/pm/_active.md — никогда не ревьюился"),
        expect(out, "<slug>", False), expect(out, "call-*", False), expect(out, "~/.config", False),
        # A: пользовательский link_ignore_prefixes дополняет дефолты, а не затирает — "/" (абсолютный путь) всё ещё скипается
        expect(out, "/nonexistent-abs-path` не существует", False),
        # B1: host/module-путь — не файл, не красный
        expect(out, "git.example.io/x/y` не существует", False),
        # B2: плейсхолдер YYYY — не красный
        expect(out, "archive/work-logs-YYYY-MM", False),
        # B3: путь разрешается внутри вложенного репозитория nested/
        expect(out, "internal/db` не существует", False),
        # B4: одиночный сегмент со слэшем — существующий каталог зелёный, несуществующий красный
        expect(out, "nowhere/` не существует"),
        expect(out, "docs/` не существует", False),
        rc == 0,
    ]
    rc, _ = run(root, "--exit-code"); results.append(expect(str(rc), "1"))

    rc, out = run(root, "--bless", "management/README.md"); print(f"--- bless (rc={rc}) ---"); print(out.strip())
    rc, out = run(root)
    results.append(expect(out, "OK   management/README.md — свежий (reviewed 2026-09-22, до 2026-10-06)"))

    # артефакты внутри sources не считаются входом: их появление и смена не красят документ
    w(root, "docs/__pycache__/a.cpython-313.pyc", "байткод\n"); w(root, "docs/.DS_Store", "finder\n")
    rc, out = run(root); print("--- артефакты в sources ---")
    results.append(expect(out, "OK   management/README.md — свежий"))

    w(root, "docs/a.md", "a изменён\n"); w(root, "docs/b.md", "новый\n")
    rc, out = run(root); print("--- мутация входов ---")
    results.append(expect(out, "stale management/README.md — hash: изменились docs/a.md, docs/b.md"))

    rc, out = run(root, "--summary"); print("--- summary ---"); print(out.strip())
    results.append(expect(out, "doclint: "))

    # вложенный репозиторий со своей моделью: дубль с родительским L0 и --tree
    w(root, "nested/CLAUDE.md", f"# nested L0\n\n{DUP}\n")
    w(root, "nested/.claude/doc-model/config.json", json.dumps({"parent_canon": ["../CLAUDE.md"]}))
    rc, out = run(os.path.join(root, "nested")); print("--- вложенный репозиторий ---")
    results.append(expect(out, "дубли сверяются и с родительским каноном: ../CLAUDE.md"))
    results.append(expect(out, "дубль:"))
    rc, out = run(root, "--tree"); print("--- --tree ---"); print(out.strip().splitlines()[-1])
    results.append(expect(out, "nested/: doclint: "))

    # bless соседнего документа не теряет чужой bless (перечитывание перед записью)
    rc, out = run(root, "--bless", "management/ext.md")
    data = json.load(open(os.path.join(root, ".claude/doc-model/freshness/management__README.md.json"), encoding="utf-8"))
    results.append(expect(str("hashes" in data), "True"))
    # C: bless документа sources=external печатает срок review_after, а не "входов зафиксировано"
    results.append(expect(out, "внешняя природа, срок review_after 7 дн"))

    print(f"\nитог: {sum(results)}/{len(results)} проверок прошли; фикстура {root}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
