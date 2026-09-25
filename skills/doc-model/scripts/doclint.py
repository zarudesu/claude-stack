#!/usr/bin/env python3
"""doclint — проверка структуры док-модели и свежести канона.

Только stdlib. Отчёт всегда печатается; код возврата 0, если не задан --exit-code.

  doclint.py --root <repo>                       полный отчёт
  doclint.py --root <repo> --summary             одна строка для стартового скрипта
  doclint.py --root <repo> --bless <doc> [...]   после смыслового ревью: пересчитать хеши, поставить дату
  doclint.py --root <repo> --exit-code           для CI: 1 при красных

Конфиг: <root>/.claude/doc-model/config.json, свежесть: каталог <root>/.claude/doc-model/freshness/
(файл на документ, чтобы ветки сливались без конфликтов; пути переопределяются --config / --freshness). Схемы — references/freshness.md рядом со скиллом.
"""
import argparse
import datetime as dt
import fnmatch
import glob
import hashlib
import json, subprocess
import os
import re
import sys
import unicodedata

DEFAULT_CONFIG = {
    "l0": "CLAUDE.md",
    "l0_limits": {"bytes": 10240, "lines": 200},
    "canon": ["CLAUDE.md", "*/CLAUDE.md", ".claude/rules/*.md"],
    "derived": [],
    "ignore_dirs": [".git", "node_modules", ".worktrees", "_archive", "__pycache__"],
    "coverage_ignore": ["*.png", "*.jpg", "*.jpeg", "*.gif", "*.pdf", "*.zip", "package.json", "package-lock.json", "*.lock"],
    "link_ignore_prefixes": ["http://", "https://", "mailto:", "~", "$", "/"],
    "duplicate_min_len": 60,
    "parent_canon": [],
    "pm": {"root": ".claude/pm", "plan": "plan.md", "handoff_glob": "handoff-*.md", "containers": ["_management", "_research"],
           "header_lines": 30, "header_keys": ["Next action"], "plan_lines": 600, "handoffs_max": 1},
    "rules_dir": ".claude/rules",
}

LIMITS_NOTE = ("Границы: линтер видит структуру указателей и изменение объявленных входов; "
               "он не доказывает истинность текста, полноту связей и состояние прода. "
               "Снятие stale — только смысловое ревью, затем --bless.")

LIST_MERGE_KEYS = ("link_ignore_prefixes", "ignore_dirs", "coverage_ignore")


def merge_config(default, user):
    """Списки LIST_MERGE_KEYS дополняют дефолты, а не заменяют: сначала дефолты, потом новые без дублей."""
    cfg = {**default, **user}
    for key in LIST_MERGE_KEYS:
        merged = list(default[key])
        for v in user.get(key, []):
            if v not in merged:
                merged.append(v)
        cfg[key] = merged
    return cfg


class Report:
    def __init__(self):
        self.fail, self.warn, self.ok, self.info = [], [], [], []

    def f(self, section, msg): self.fail.append((section, msg))
    def w(self, section, msg): self.warn.append((section, msg))
    def o(self, section, msg): self.ok.append((section, msg))
    def i(self, section, msg): self.info.append((section, msg))


# ---------- helpers ----------

def load_json(path, default):
    if not os.path.exists(path):
        return default, False
    with open(path, encoding="utf-8") as fh:
        return json.load(fh), True


def fresh_file(fresh_dir, doc):
    return os.path.join(fresh_dir, doc.replace("/", "__").lstrip(".") + ".json")  # без ведущей точки: скрытый файл пропускают glob и ls


def load_freshness(fresh_dir):
    """Каталог с файлом на документ → {doc: meta}. Возвращает (docs, каталог существует)."""
    if not os.path.isdir(fresh_dir):
        return {}, False
    docs = {}
    for p in sorted(glob.glob(os.path.join(fresh_dir, "*.json"))):
        with open(p, encoding="utf-8") as fh:
            meta = json.load(fh)
        doc = meta.get("doc")
        if doc:
            docs[doc] = meta
    return docs, True


def read_text(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def rel(root, path):
    return os.path.relpath(path, root)


def is_nested_repo(path):
    return os.path.exists(os.path.join(path, ".git"))


ARTIFACT_DIRS = {"__pycache__", "node_modules", ".git", ".venv", ".mypy_cache", ".pytest_cache"}
ARTIFACT_FILES = {".DS_Store"}
ARTIFACT_SUFFIXES = (".pyc", ".pyo")


def is_artifact(path):
    """Сборочный мусор входом документа не считается: иначе каждый запуск python
    или чистый checkout красит документ в stale без единого изменения по смыслу."""
    parts = path.replace(os.sep, "/").split("/")
    return (bool(ARTIFACT_DIRS.intersection(parts[:-1]))
            or parts[-1] in ARTIFACT_FILES
            or parts[-1].endswith(ARTIFACT_SUFFIXES))


def expand(root, patterns):
    """Глобы относительно root → отсортированный список существующих файлов."""
    out = []
    for pat in patterns:
        for p in sorted(glob.glob(os.path.join(root, pat), recursive=True)):
            if os.path.isfile(p) and not is_artifact(p):
                out.append(p)
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p); uniq.append(p)
    return uniq


def slugify(heading):
    """Якорь заголовка в стиле GitHub: lower, пунктуация вон, пробелы → '-'. Кириллица сохраняется."""
    h = unicodedata.normalize("NFKC", heading.strip().lower())
    h = re.sub(r"[`*_~\[\]()]", "", h)
    h = "".join(ch if (ch.isalnum() or ch in " -") else "" for ch in h)
    return re.sub(r"\s+", "-", h.strip())


def headings(path):
    hs = set()
    for line in read_text(path).splitlines():
        m = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if m:
            hs.add(m.group(1).strip()); hs.add(slugify(m.group(1)))
    return hs


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    # префикс делает значение непохожим на ключ API: без него сканеры секретов
    # (gitleaks generic-api-key) блокируют коммит на паре «путь со словом key/token» + hex
    return "sha256:" + h.hexdigest()


# ---------- структура ----------

MD_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
# второй вариант альтернативы — одиночный сегмент с завершающим слэшем (`incidents/`): дом из routing-таблицы,
# а не файл; без слэша (`CLAUDE.md`) не матчится нигде и остаётся непроверяемым, как раньше.
TICK_PATH = re.compile(r"`([A-Za-z0-9_.@\-]+(?:/[A-Za-z0-9_.@\-]+)+/?|[A-Za-z0-9_.@\-]+/)`")


def check_limits(root, cfg, rep):
    l0 = os.path.join(root, cfg["l0"])
    if not os.path.exists(l0):
        rep.f("структура", f"L0 `{cfg['l0']}` не найден"); return
    lim = cfg["l0_limits"]
    b, n = os.path.getsize(l0), len(read_text(l0).splitlines())
    msg = f"L0 `{cfg['l0']}`: {b} B / {n} строк (лимит {lim['bytes']} B / {lim['lines']})"
    (rep.f if (b > lim["bytes"] or n > lim["lines"]) else rep.o)("структура", msg)
    for d in cfg.get("derived", []):
        p = os.path.join(root, d["path"])
        if not os.path.exists(p):
            rep.w("структура", f"derived `{d['path']}` не найден"); continue
        n = len(read_text(p).splitlines())
        msg = f"derived `{d['path']}`: {n} строк (лимит {d.get('lines', 25)})"
        (rep.f if n > d.get("lines", 25) else rep.o)("структура", msg)


def find_nested_repos(root, ignore_dirs):
    """Вложенные git-репозитории верхнего уровня, не из ignore_dirs — доп. кандидаты разрешения путей ссылок."""
    out = []
    for name in sorted(os.listdir(root)):
        if name.startswith(".") or name in ignore_dirs:
            continue
        full = os.path.join(root, name)
        if os.path.isdir(full) and is_nested_repo(full):
            out.append(full)
    return out


def check_links(root, cfg, canon_files, rep, nested_repos):
    skip = tuple(cfg["link_ignore_prefixes"])
    count = 0
    for f in canon_files:
        text = read_text(f)
        for ln, line in enumerate(text.splitlines(), 1):
            targets = [(m.group(1), "ссылка") for m in MD_LINK.finditer(line)]
            targets += [(m.group(1), "путь") for m in TICK_PATH.finditer(line)]
            for raw, kind in targets:
                if raw.startswith(skip) or raw.startswith("#"):
                    continue
                if any(ch in raw for ch in "<>*?{}") or "YYYY" in raw or ":" in raw.split("#")[0]:
                    continue  # плейсхолдер (в т.ч. YYYY), глоб, file:line, scheme
                first_seg = raw.split("/", 1)[0]
                if "." in first_seg and not first_seg.startswith(".") and "/" in raw:
                    continue  # host/module-путь (git.example.com/org/backend, example.com/backend), не файл
                path, _, anchor = raw.partition("#")
                path = path.rstrip("/") if path != "/" else path
                cands = [os.path.normpath(os.path.join(os.path.dirname(f), path)),
                         os.path.normpath(os.path.join(root, path))]
                cands += [os.path.normpath(os.path.join(nr, path)) for nr in nested_repos]
                hit = next((c for c in cands if os.path.exists(c)), None)
                count += 1
                if hit is None:
                    rep.f("структура", f"{kind}: {rel(root, f)}:{ln} → `{raw}` не существует"); continue
                if anchor and hit.endswith(".md") and os.path.isfile(hit):
                    hs = headings(hit)
                    if anchor not in hs and slugify(anchor) not in hs:
                        rep.f("структура", f"ссылка: {rel(root, f)}:{ln} → `{raw}`: раздела нет в {rel(root, hit)}")
    rep.i("структура", f"проверено указателей: {count} в {len(canon_files)} канон-файлах")


def git_ignored_top(root):
    """Верхнеуровневые записи, которые git игнорирует: артефакты и локальные клоны, дом им в каноне не нужен."""
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory"],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {line.strip().rstrip("/").split("/")[0] for line in out.splitlines() if line.strip() and "/" not in line.strip().rstrip("/")}


def check_coverage(root, cfg, canon_files, rep):
    corpus = "\n".join(read_text(f) for f in canon_files)
    ignored = git_ignored_top(root)
    if ignored:
        rep.i("структура", "по .gitignore пропущено: " + ", ".join(sorted(ignored)))
    for name in sorted(os.listdir(root)):
        if name.startswith("."):
            continue
        full = os.path.join(root, name)
        if os.path.isdir(full) and is_nested_repo(full):
            rep.i("структура", f"вложенный репозиторий `{name}/` — пропущен"); continue  # раньше gitignore: вложенный репо часто игнорируется родителем
        if name in ignored:
            continue
        if os.path.isdir(full):
            if name in cfg["ignore_dirs"]:
                continue
            token = name + "/"
        else:
            if any(fnmatch.fnmatch(name, g) for g in cfg["coverage_ignore"]):
                continue
            token = name
        if token not in corpus:
            rep.f("структура", f"без дома: `{token}` не упомянут ни в одном канон-файле")


def check_duplicates(root, cfg, canon_files, rep):
    seen = {}
    minlen = cfg["duplicate_min_len"]
    parents = [os.path.normpath(os.path.join(root, p)) for p in cfg.get("parent_canon", [])]
    parents = [p for p in parents if os.path.isfile(p)]
    if parents:
        rep.i("структура", "дубли сверяются и с родительским каноном: " + ", ".join(rel(root, p) for p in parents))
    for f in canon_files + parents:
        for ln, line in enumerate(read_text(f).splitlines(), 1):
            norm = re.sub(r"\s+", " ", line.strip())
            if len(norm) < minlen or norm.startswith(("|--", "```", "<!--")):
                continue
            seen.setdefault(norm, []).append((rel(root, f), ln))
    for norm, places in seen.items():
        files = {p[0] for p in places}
        if len(files) > 1:
            where = ", ".join(f"{p}:{l}" for p, l in places[:4])
            rep.f("структура", f"дубль: «{norm[:70]}…» в {where}")


# ---------- заполнение: планы, handoff, правила ----------

def pm_units(root, pm):
    """Единицы pm-каталога: `<slug>/` и `<container>/<deliverable>/`; прочие `_`-каталоги служебные."""
    base = os.path.join(root, pm["root"])
    if not os.path.isdir(base):
        return None
    units = []
    for name in sorted(os.listdir(base)):
        p = os.path.join(base, name)
        if not os.path.isdir(p) or name in DEFAULT_CONFIG["ignore_dirs"]:
            continue
        if name in pm["containers"]:
            units += [os.path.join(p, sub) for sub in sorted(os.listdir(p)) if os.path.isdir(os.path.join(p, sub))]
        elif not name.startswith("_"):
            units.append(p)
    return units


def check_pm(root, cfg, rep):
    """Заполнение — механизм вместо дисциплины: шапка плана держит next action, план не раздувается,
    handoff не копятся, направление без плана — сирота (deliverable в контейнере живёт и одним handoff).
    Правки планов линтер не делает."""
    pm = {**DEFAULT_CONFIG["pm"], **(cfg.get("pm") or {})}
    units = pm_units(root, pm)
    if units is None:
        rep.i("заполнение", f"каталога {pm['root']}/ нет — планов нет"); return
    for u in units:
        r = rel(root, u)
        plan = os.path.join(u, pm["plan"])
        handoffs = sorted(glob.glob(os.path.join(u, pm["handoff_glob"])))
        if os.path.exists(plan):
            lines = read_text(plan).splitlines()
            head = "\n".join(lines[:pm["header_lines"]]).lower()
            missing = [k for k in pm["header_keys"] if k.lower() not in head]
            if missing:
                rep.f("заполнение", f"{r}/{pm['plan']}: в шапке ({pm['header_lines']} строк) нет "
                      + ", ".join(f"`{k}`" for k in missing) + " — читающая сессия берёт шапку, не хвост лога")
            if len(lines) > pm["plan_lines"]:
                rep.f("заполнение", f"{r}/{pm['plan']}: {len(lines)} строк (лимит {pm['plan_lines']}) — "
                      "старый лог в archive/, шапка и текущий шаг остаются")
        elif os.path.basename(os.path.dirname(u)) in pm["containers"]:
            if handoffs:
                rep.i("заполнение", f"{r}/: без {pm['plan']}, только handoff ({len(handoffs)})")
            else:
                rep.i("заполнение", f"{r}/: ни {pm['plan']}, ни {pm['handoff_glob']} — закрытая работа или сирота, решает владелец")
        else:
            rep.f("заполнение", f"{r}/: нет {pm['plan']} — один план на направление, каталог-сирота")
        if len(handoffs) > pm["handoffs_max"]:
            rep.w("заполнение", f"{r}/: handoff-файлов {len(handoffs)} (лимит {pm['handoffs_max']}) — "
                  "handoff только для незавершённой передачи, старые сложить в план")
    rep.i("заполнение", f"pm-единиц проверено: {len(units)}")


def check_rules(root, cfg, rep):
    """Правило без `paths:` грузится в каждую сессию целиком — это L0-байты под видом L1."""
    d = os.path.join(root, cfg.get("rules_dir", ".claude/rules"))
    if not os.path.isdir(d):
        return
    for f in sorted(glob.glob(os.path.join(d, "*.md"))):
        text = read_text(f)
        parts = text.split("---", 2)
        has_paths = text.startswith("---") and len(parts) >= 3 and re.search(r"^paths:", parts[1], re.M) is not None
        if not has_paths:
            rep.w("структура", f"правило {rel(root, f)} без `paths:` во frontmatter — грузится в каждую сессию, это L0-байты, не L1")


# ---------- свежесть ----------

def source_hashes(root, sources, skip=None):
    files = expand(root, sources)
    return {rel(root, p): sha(p) for p in files if rel(root, p) != skip}  # сам документ входом не считается


def check_freshness(root, docs, rep, today):
    if not docs:
        rep.i("свежесть", "каталог свежести пуст — разметка не установлена"); return
    for doc, meta in docs.items():
        if not os.path.exists(os.path.join(root, doc)):
            rep.f("свежесть", f"{doc}: размеченный документ не существует"); continue
        reviewed = meta.get("reviewed")
        ttl = meta.get("review_after")
        srcs = meta.get("sources")
        problems = []
        if not reviewed:
            problems.append("никогда не ревьюился")
        else:
            try:
                rdate = dt.date.fromisoformat(reviewed)
            except ValueError:
                rdate = None; problems.append(f"reviewed `{reviewed}` не дата")
            if rdate and ttl and (today - rdate).days > int(ttl):
                problems.append(f"ttl: reviewed {reviewed}, срок {ttl} дн")
        if srcs == "external":
            if not ttl:
                problems.append("external без review_after — не протухнет никогда")
        elif isinstance(srcs, list):
            cur = source_hashes(root, srcs, doc)
            if not cur:
                rep.w("свежесть", f"{doc}: sources не находят ни одного файла")
            old = {p: v.rsplit(":", 1)[-1] for p, v in meta.get("hashes", {}).items()}  # старый формат без префикса
            changed = sorted([p for p in cur if old.get(p) != cur[p].rsplit(":", 1)[-1]] + [p for p in old if p not in cur])
            if changed and old:
                head = ", ".join(changed[:3]) + (f" (+{len(changed)-3} ещё)" if len(changed) > 3 else "")
                problems.append(f"hash: изменились {head}")
            elif changed and not old:
                problems.append("hash: входы ни разу не зафиксированы (--bless)")
        else:
            problems.append("sources: ни список путей, ни `external`")
        if problems:
            rep.f("свежесть", f"stale {doc} — " + "; ".join(problems))
        else:
            until = f", до {dt.date.fromisoformat(reviewed) + dt.timedelta(days=int(ttl))}" if ttl else ""
            rep.o("свежесть", f"{doc} — свежий (reviewed {reviewed}{until})")


def bless(root, fresh_dir, docs, today):
    table, _ = load_freshness(fresh_dir)  # перечитать перед записью: соседнее окно могло благословить другой документ
    for doc in docs:
        if doc not in table:
            print(f"bless: {doc} не размечен в {fresh_dir} — сначала добавь файл с sources", file=sys.stderr)
            return 2
        meta = table[doc]
        if isinstance(meta.get("sources"), list):
            meta["hashes"] = source_hashes(root, meta["sources"], doc)
        meta["reviewed"] = today.isoformat()
        path = fresh_file(fresh_dir, doc); tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2); fh.write("\n")
        os.replace(tmp, path)  # атомарно и по одному файлу на документ: читатель не увидит полфайла, ветки сливаются без конфликтов
        if meta.get("sources") == "external":
            print(f"bless: {doc} — reviewed {today}, внешняя природа, срок review_after {meta.get('review_after')} дн")
        else:
            print(f"bless: {doc} — reviewed {today}, входов зафиксировано: {len(meta.get('hashes', {}))}")
    return 0


# ---------- main ----------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--config")
    ap.add_argument("--freshness")
    ap.add_argument("--bless", nargs="+", metavar="DOC")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--exit-code", action="store_true")
    ap.add_argument("--today", help="YYYY-MM-DD, для тестов")
    ap.add_argument("--tree", action="store_true", help="после корня — summary каждого вложенного репозитория со своим config.json")
    a = ap.parse_args(argv)

    root = os.path.abspath(a.root)
    cfg_path = a.config or os.path.join(root, ".claude", "doc-model", "config.json")
    fresh_dir = a.freshness or os.path.join(root, ".claude", "doc-model", "freshness")
    today = dt.date.fromisoformat(a.today) if a.today else dt.date.today()

    user_cfg, cfg_found = load_json(cfg_path, {})
    cfg = merge_config(DEFAULT_CONFIG, user_cfg)
    fresh_docs, fresh_found = load_freshness(fresh_dir)

    if a.bless:
        return bless(root, fresh_dir, a.bless, today)

    rep = Report()
    rep.i("структура", f"конфиг: {cfg_path if cfg_found else 'встроенные значения (файла нет)'}")
    canon_files = expand(root, cfg["canon"])
    if not canon_files:
        rep.f("структура", f"канон-файлы по паттернам {cfg['canon']} не найдены")
    check_limits(root, cfg, rep)
    nested_repos = find_nested_repos(root, cfg["ignore_dirs"])
    check_links(root, cfg, canon_files, rep, nested_repos)
    check_coverage(root, cfg, canon_files, rep)
    check_duplicates(root, cfg, canon_files, rep)
    check_rules(root, cfg, rep)
    check_pm(root, cfg, rep)
    if fresh_found:
        check_freshness(root, fresh_docs, rep, today)
    else:
        shown = rel(root, fresh_dir) if fresh_dir.startswith(root + os.sep) else fresh_dir
        rep.i("свежесть", f"каталога {shown}/ нет — разметка свежести не установлена")

    n_struct = sum(1 for s, _ in rep.fail if s == "структура")
    n_fresh = sum(1 for s, _ in rep.fail if s == "свежесть")
    n_fill = sum(1 for s, _ in rep.fail if s == "заполнение")
    total = (f"doclint: {len(rep.fail)} красных (структура {n_struct}, свежесть {n_fresh}, заполнение {n_fill}), "
             f"предупреждений {len(rep.warn)}")

    if a.summary:
        print(total)
    else:
        print(f"doclint — {root} — {today}")
        print(LIMITS_NOTE)
        for section in ("структура", "свежесть", "заполнение"):
            print(f"\n[{section}]")
            for kind, items in (("FAIL", rep.fail), ("WARN", rep.warn), ("OK", rep.ok), ("INFO", rep.info)):
                for s, m in items:
                    if s == section:
                        print(f"  {kind:<4} {m}")
        print("\n" + total)
    rc = 1 if (a.exit_code and rep.fail) else 0
    if a.tree:
        for name in sorted(os.listdir(root)):
            sub = os.path.join(root, name)
            if not os.path.isdir(sub) or name.startswith(".") or name in cfg["ignore_dirs"] or not is_nested_repo(sub):
                continue
            if not os.path.exists(os.path.join(sub, ".claude", "doc-model", "config.json")):
                print(f"{name}/: doc-model не установлен"); continue
            sys.stdout.write(f"{name}/: ")
            sub_args = ["--root", sub, "--summary"] + (["--today", a.today] if a.today else []) + (["--exit-code"] if a.exit_code else [])
            rc = max(rc, main(sub_args))
    return rc


if __name__ == "__main__":
    sys.exit(main())
