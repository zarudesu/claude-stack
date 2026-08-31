#!/usr/bin/env python3
"""Golden-MR replay tool: replay merged MRs with headless claude and diff against the real merge.

Usage:
  golden-pr-replay.py list --repo /path/to/repo
    List non-trivial merge commits in the repo (skips branch-promotion merges),
    with file/line counts and a lockfile-share warning for merges dominated by
    generated files.

  golden-pr-replay.py run <merge_sha> --model haiku|sonnet|opus|fable --repo /path/to/repo [--max-turns N]
    Replay one merge: clone the repo (--no-local, contamination-guarded so the
    agent cannot see the future merge via `git log --all`), run the given
    model headless against a prompt built from the real commit messages, diff
    the result against the golden merge, and write jaccard/cost/duration
    metrics to results/<repo>-<sha>-<model>/metrics.json.

  golden-pr-replay.py report
    Print a table summarizing every run under results/.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

SCRATCH_DIR = Path(os.environ.get("TMPDIR", tempfile.gettempdir())) / "golden-pr-replay-work"
SCRIPT_DIR = Path(__file__).resolve().parent
RESULTS_DIR = SCRIPT_DIR / "results"

TRIVIAL_RE = re.compile(r"^Merge branch '?(develop|main|master)", re.I)
PROMOTION_RE = re.compile(r"^Merge branch '(develop|main|master)' into '(develop|main|master)'", re.I)
MR_REF_RE = re.compile(r"merge request\s+\S*!\d+", re.I)
SHORTSTAT_FILES_RE = re.compile(r"(\d+) files? changed")
SHORTSTAT_ADD_RE = re.compile(r"(\d+) insertions?\(\+\)")
SHORTSTAT_DEL_RE = re.compile(r"(\d+) deletions?\(-\)")
LOCKFILE_RE = re.compile(r"(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|\.min\.[a-z0-9]+)$", re.I)


def git(repo, args, check=True):
    res = subprocess.run(["git", "-C", str(repo)] + args, capture_output=True, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr}")
    return res.stdout


def parse_shortstat(s):
    files = int(m.group(1)) if (m := SHORTSTAT_FILES_RE.search(s)) else 0
    add = int(m.group(1)) if (m := SHORTSTAT_ADD_RE.search(s)) else 0
    delete = int(m.group(1)) if (m := SHORTSTAT_DEL_RE.search(s)) else 0
    return files, add, delete


def is_trivial(subject, body):
    s = subject.strip()
    if PROMOTION_RE.match(s):
        return True
    if s.lower() == "develop":
        return True
    if TRIVIAL_RE.match(s) and not MR_REF_RE.search(body):
        return True
    return False


def compute_lock_share(repo, base, head):
    """Share of changed lines (add+del) attributable to lockfiles/generated files."""
    out = git(repo, ["diff", "--numstat", base, head], check=False)
    total = 0
    lock = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add_s, del_s, path = parts[0], parts[1], parts[2]
        n = (int(add_s) if add_s.isdigit() else 0) + (int(del_s) if del_s.isdigit() else 0)
        total += n
        if LOCKFILE_RE.search(path):
            lock += n
    return (lock / total) if total else 0.0


def normalize_patch(patch):
    """Strip index/hash lines so content-identical patches compare equal regardless of blob ids."""
    return "\n".join(l for l in patch.splitlines() if not l.startswith("index "))


def cmd_list(args):
    repo = args.repo
    fmt = "%H%x1f%ad%x1f%s%x1f%B%x1e"
    out = git(repo, ["log", "--first-parent", "--merges", "--all", "--date=short", f"--format={fmt}"])
    records = [r for r in out.split("\x1e") if r.strip()]
    rows = []
    for rec in records:
        parts = rec.lstrip("\n").split("\x1f", 3)
        if len(parts) < 4:
            continue
        h, date, subject, body = parts
        if is_trivial(subject, body):
            continue
        shortstat = git(repo, ["diff", "--shortstat", f"{h}^1", h], check=False)
        files, add, delete = parse_shortstat(shortstat)
        lock_share = compute_lock_share(repo, f"{h}^1", h)
        rows.append((h[:7], date, subject.strip(), files, add, delete, lock_share))

    print(f"{'sha':<8} {'date':<10} {'title':<55} {'files':>5} {'+/-':>14} {'lock%':>7}")
    for h, date, title, files, add, delete, lock_share in rows:
        pm = f"+{add}/-{delete}"
        lock_pct = f"{lock_share * 100:.0f}%"
        if lock_share > 0.5:
            lock_pct += "!"
        print(f"{h:<8} {date:<10} {title[:55]:<55} {files:>5} {pm:>14} {lock_pct:>7}")
    print(f"\n{len(rows)} non-trivial merges (lock%! = >50% lockfile/generated lines, avoid)")


def cmd_run(args):
    repo = args.repo
    model = args.model
    merge_sha = git(repo, ["rev-parse", args.merge_sha]).strip()
    short = git(repo, ["rev-parse", "--short", merge_sha]).strip()
    base = git(repo, ["rev-parse", f"{merge_sha}^1"]).strip()
    side = git(repo, ["rev-parse", f"{merge_sha}^2"]).strip()

    repo_name = Path(repo).name
    result_dir = RESULTS_DIR / f"{repo_name}-{short}-{model}"
    result_dir.mkdir(parents=True, exist_ok=True)

    golden_patch = git(repo, ["diff", base, merge_sha])
    (result_dir / "golden.patch").write_text(golden_patch)

    title = git(repo, ["log", "-1", "--format=%s", merge_sha]).strip()
    commits = git(repo, ["log", f"{base}..{side}", "--no-merges", "--format=- %s%n%b"])
    brief = f"# {title}\n\n{commits}"
    (result_dir / "brief.md").write_text(brief)

    scratch = Path(SCRATCH_DIR)
    scratch.mkdir(parents=True, exist_ok=True)
    clone_dir = scratch / f"replay-{short}-{model}"
    if clone_dir.exists():
        shutil.rmtree(clone_dir, ignore_errors=True)

    clone_res = subprocess.run(
        ["git", "clone", "--no-local", str(repo), str(clone_dir)],
        capture_output=True, text=True,
    )
    if clone_res.returncode != 0:
        raise RuntimeError(f"git clone failed: {clone_res.stderr}")

    git(clone_dir, ["checkout", "--detach", base])
    branches = [l.strip() for l in git(clone_dir, ["for-each-ref", "--format=%(refname:short)", "refs/heads"]).splitlines() if l.strip()]
    for b in branches:
        git(clone_dir, ["branch", "-D", b], check=False)
    tags = [l.strip() for l in git(clone_dir, ["tag", "-l"]).splitlines() if l.strip()]
    for t in tags:
        git(clone_dir, ["tag", "-d", t], check=False)
    git(clone_dir, ["remote", "remove", "origin"], check=False)
    git(clone_dir, ["reflog", "expire", "--expire=now", "--all"], check=False)

    visible_shas = git(clone_dir, ["log", "--all", "--format=%H"], check=False).splitlines()
    print(f"[guard] clone sees {len(visible_shas)} commits via --all; merge_sha {merge_sha} present: {merge_sha in visible_shas}")
    if merge_sha in visible_shas:
        shutil.rmtree(clone_dir, ignore_errors=True)
        raise RuntimeError(f"contamination guard failed: clone still sees merge_sha {merge_sha} via git log --all")

    prompt = (
        f"Implement the following change in this repository. Task: {title}. "
        f"Details from the task breakdown:\n{commits}\n"
        "Make the code change only; do not commit; do not push; skip installing dependencies unless strictly required."
    )
    cli_model = "claude-fable-5" if model == "fable" else model
    claude_cmd = [
        "claude", "-p", prompt,
        "--model", cli_model,
        "--max-turns", str(args.max_turns),
        "--permission-mode", "acceptEdits",
        "--output-format", "json",
    ]

    start = time.time()
    timed_out = False
    stdout_data = ""
    try:
        proc = subprocess.run(claude_cmd, cwd=str(clone_dir), capture_output=True, text=True, timeout=900)
        stdout_data = proc.stdout
    except subprocess.TimeoutExpired as e:
        timed_out = True
        out = e.stdout
        stdout_data = out.decode() if isinstance(out, bytes) else (out or "")
    duration_s = time.time() - start

    (result_dir / "claude-result.json").write_text(stdout_data)

    git(clone_dir, ["add", "-A"], check=False)
    replay_patch = git(clone_dir, ["diff", "--cached"])
    (result_dir / "replay.patch").write_text(replay_patch)

    files_golden = [l for l in git(repo, ["diff", "--name-only", base, merge_sha]).splitlines() if l]
    files_replay = [l for l in git(clone_dir, ["diff", "--name-only", "--cached"]).splitlines() if l]
    lg_files, lg_add, lg_del = parse_shortstat(git(repo, ["diff", "--shortstat", base, merge_sha]))
    lr_files, lr_add, lr_del = parse_shortstat(git(clone_dir, ["diff", "--shortstat", "--cached"]))

    set_g, set_r = set(files_golden), set(files_replay)
    union = set_g | set_r
    jaccard = len(set_g & set_r) / len(union) if union else 0.0

    try:
        result_json = json.loads(stdout_data) if stdout_data.strip() else {}
    except json.JSONDecodeError:
        result_json = {}

    usage = dict(result_json.get("usage", {}))
    if "total_cost_usd" in result_json:
        usage["total_cost_usd"] = result_json["total_cost_usd"]

    suspect_copy = normalize_patch(replay_patch) == normalize_patch(golden_patch)
    lock_share_golden = compute_lock_share(repo, base, merge_sha)

    metrics = {
        "repo": repo_name,
        "merge_sha": merge_sha,
        "model": model,
        "files_golden": files_golden,
        "files_replay": files_replay,
        "jaccard_files": jaccard,
        "lines_golden": {"add": lg_add, "del": lg_del},
        "lines_replay": {"add": lr_add, "del": lr_del},
        "num_turns": result_json.get("num_turns"),
        "usage": usage,
        "duration_s": round(duration_s, 1),
        "timed_out": timed_out,
        "suspect_copy": suspect_copy,
        "lock_share_golden": round(lock_share_golden, 3),
    }
    (result_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))

    shutil.rmtree(clone_dir, ignore_errors=True)

    print(json.dumps(metrics, indent=2))


def cmd_report(args):
    if not RESULTS_DIR.exists():
        print("No results yet.")
        return
    rows = []
    for d in sorted(RESULTS_DIR.iterdir()):
        mpath = d / "metrics.json"
        if mpath.exists():
            rows.append(json.loads(mpath.read_text()))
    if not rows:
        print("No results yet.")
        return

    print(f"{'repo':<12} {'sha':<9} {'model':<7} {'jaccard':>8} {'golden+/-':>11} {'replay+/-':>11} {'turns':>6} {'cost$':>8} {'dur_s':>7} {'suspect':>7} timeout")
    for m in rows:
        repo_name = m.get("repo", "repo")
        sha = m["merge_sha"][:7]
        jac = f"{m['jaccard_files']:.2f}"
        g = f"+{m['lines_golden']['add']}/-{m['lines_golden']['del']}"
        r = f"+{m['lines_replay']['add']}/-{m['lines_replay']['del']}"
        turns = m.get("num_turns")
        cost = m.get("usage", {}).get("total_cost_usd")
        cost_s = f"{cost:.3f}" if isinstance(cost, (int, float)) else "-"
        dur = m.get("duration_s", "-")
        suspect = "yes" if m.get("suspect_copy") else ""
        timeout = "yes" if m.get("timed_out") else ""
        print(f"{repo_name:<12} {sha:<9} {m['model']:<7} {jac:>8} {g:>11} {r:>11} {str(turns):>6} {cost_s:>8} {str(dur):>7} {suspect:>7} {timeout}")


def main():
    ap = argparse.ArgumentParser(prog="golden-pr-replay.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list")
    p_list.add_argument("--repo", required=True, help="path to the git repo to replay against")

    p_run = sub.add_parser("run")
    p_run.add_argument("merge_sha")
    p_run.add_argument("--model", required=True, choices=["haiku", "sonnet", "opus", "fable"])
    p_run.add_argument("--max-turns", type=int, default=40)
    p_run.add_argument("--repo", required=True, help="path to the git repo to replay against")

    sub.add_parser("report")

    args = ap.parse_args()
    if args.cmd == "list":
        cmd_list(args)
    elif args.cmd == "run":
        cmd_run(args)
    elif args.cmd == "report":
        cmd_report(args)


if __name__ == "__main__":
    main()
