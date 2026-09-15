#!/usr/bin/env python3
# PreCompact hook: extract a bounded, secret-scrubbed state snapshot from the
# transcript that is about to be compacted, and write it to
#   $HOME/.claude/compact-state/<session_id>.md
#
# WHY A SNAPSHOT FILE AND NOT PreCompact's own "Additional Instructions" channel:
# PreCompact stdout (raw, non-JSON) is spliced verbatim into the compaction
# SUMMARIZER's prompt (var name in the 2.1.258 binary: newCustomInstructions ->
# "Additional Instructions:" section, function THe, confirmed in
# scratchpad/audit/precompact/precompact_impl.txt + r1_hook_contract.md §2.2).
# That is a one-shot nudge to the model that WROTE the summary, not a durable
# artifact. Writing our own independently-computed snapshot file means the
# facts we care about (R3-proven loss categories: live env facts, edited
# files, todo state) survive regardless of what the LLM summarizer chose to
# keep, and can be re-injected on every subsequent SessionStart(compact) --
# see postcompact-inject.sh. Because of that we intentionally emit NOTHING on
# stdout/stderr on the success path: anything this script prints on stdout
# would silently become part of the summarization prompt, which is not this
# hook's job and is not something to do accidentally.
#
# WHY NOT ALSO WRITE THE OLD MARKER INTO .claude/pm/_active.md (dropped):
# The prior compact-mark.sh appended a marker line into <cwd>/.claude/pm/_active.md.
# The precompact audit (R-series) found _active.md is a git-TRACKED file in the
# private repos -- 1128 marker lines had accumulated across
# 140 files, pure git diff noise with zero information content (no state, just
# "a compaction happened at time T"). This hook writes only to $HOME/.claude/,
# never into any repo working tree, so there is nothing to keep or replace: the
# marker behaviour is dropped outright rather than reimplemented safely.
#
# CONTRACT (verified against the 2.1.258 binary, see r1_hook_contract.md §1.2):
#   stdin  = {"session_id":..., "transcript_path":..., "cwd":..., "trigger":"manual"|"auto", ...}
#   stdout = nothing (see above)
#   exit   = always 0. This hook must NEVER block compaction and must NEVER
#            throw -- any failure (missing jq -- irrelevant, we're python;
#            unreadable transcript; malformed JSON; permission error) is
#            swallowed and the hook exits 0 silently.
#
# PERFORMANCE: single forward streaming pass over the transcript (line-by-line,
# never loads the whole file into memory), O(1) extra memory via bounded
# deques/dicts. Benchmarked at ~0.3-0.6s on real 40-45MB / ~20k-line
# transcripts -- see sample_output.md for actual numbers.

import sys
import os
import re
import json
import time
import shlex
from collections import deque, OrderedDict

# ---- hard caps (spec: ~4000 tokens / ~16000 bytes) -------------------------
MAX_OUTPUT_BYTES = 16000
FILES_LIMIT = 20
ENV_FACT_LIMIT_PER_CATEGORY = 10
ACCEPT_CMDS_LIMIT = 8
PROMPTS_LIMIT = 3
PROMPT_MAX_CHARS = 600
TODO_ITEMS_MAX = 40


def eprint(*a):
    # diagnostics only -- stderr, never stdout (see header comment)
    try:
        print(*a, file=sys.stderr)
    except Exception:
        pass


# ---- secret redaction --------------------------------------------------
# Applied per-value while harvesting AND once more as a full-text safety net
# right before the file is written (belt and suspenders: a miss in one of the
# per-category extractors must not become a leak).
_PEM_RE = re.compile(r'-----BEGIN [A-Z0-9 ]*(PRIVATE KEY|CERTIFICATE)-----.*?-----END [A-Z0-9 ]*(PRIVATE KEY|CERTIFICATE)-----', re.DOTALL)
_PEM_LINE_RE = re.compile(r'^-----BEGIN [A-Z0-9 ]*-----\s*$', re.MULTILINE)
_KV_SECRET_RE = re.compile(
    # Deliberately case-insensitive only *inside* this scoped group, on a
    # fixed whole-word alternation, and deliberately NOT including a bare
    # "AUTH" trigger: an un-scoped (?i) with a free "AUTH" alternative
    # previously matched *inside* the English word "Authorization" (whose
    # own header form is handled separately by _AUTH_HEADER_RE below) and
    # silently ate surrounding prose words when both regexes then fired on
    # the same span. \b...\b on a closed word set avoids that class of bug.
    r'\b((?i:[A-Za-z0-9_]*(?:API[_-]?KEY|ACCESS[_-]?KEY|SECRET|PASSWORD|PASSWD|PASS|PWD|TOKEN|SESSION(?![_-]?ID\b)|COOKIE|PRIVATE[_-]?KEY)[A-Za-z0-9_]*)'
    r'\s*[:=]\s*)(["\']?)([^\s"\']{3,})\2'
)
_AUTH_HEADER_RE = re.compile(r'(?i)(Authorization:\s*)((?:Bearer|Basic|Token|Digest)\s+\S+|\S+)')
_SSHPASS_RE = re.compile(r'(?i)(sshpass\s+-p\s*)(\S+)')
# v_leak #1: curl/wget HTTP Basic via -u user:pass — короткий пароль не ловится
# ни shape-сетями, ни keyword-правилом, поэтому отдельное правило по флагу.
_BASICAUTH_FLAG_RE = re.compile(r'(?i)(\s-u\s+)([^\s:@]+):(\S+)')
# v_leak #2: креды внутри URL (https://user:pass@host/...) — userinfo-сегмент.
_URL_USERINFO_RE = re.compile(r'(://)([^/@\s:]+):([^/@\s]+)(@)')
# Deliberately NOT including "/" in the base64-ish charset: this text is a
# safety-net pass over the WHOLE assembled document, which is full of file
# paths ("site/public/data/breaklevel/trades") that are >=32 contiguous
# chars of [A-Za-z0-9/] and would otherwise be false-positive-redacted into
# uselessness (caught empirically -- see r-precompact test run notes).
# Real base64 secrets are still caught: they almost always contain a digit
# within 32 random symbols, enforced via the digit check in _redact_b64.
_BASE64ISH_RE = re.compile(r'\b[A-Za-z0-9+]{32,}={0,2}\b')
_HEX_RE = re.compile(r'\b[0-9a-fA-F]{32,}\b')
# v_break-it #2: session_id уходит в путь файла — только безопасный алфавит.
_SAFE_SESSION_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,128}$')


def _redact_b64(m):
    s = m.group(0)
    return '[REDACTED-B64]' if any(ch.isdigit() for ch in s) else s


def redact(text):
    if not text:
        return text
    t = text
    t = _PEM_RE.sub('[REDACTED PEM BLOCK]', t)
    t = _PEM_LINE_RE.sub('[REDACTED PEM BLOCK]', t)
    t = _KV_SECRET_RE.sub(lambda m: m.group(1) + '[REDACTED]', t)
    t = _AUTH_HEADER_RE.sub(lambda m: m.group(1) + '[REDACTED]', t)
    t = _SSHPASS_RE.sub(lambda m: m.group(1) + '[REDACTED]', t)
    t = _BASICAUTH_FLAG_RE.sub(lambda m: m.group(1) + m.group(2) + ':[REDACTED]', t)
    t = _URL_USERINFO_RE.sub(lambda m: m.group(1) + '[REDACTED]' + m.group(4), t)
    t = _HEX_RE.sub('[REDACTED-HEX]', t)
    t = _BASE64ISH_RE.sub(_redact_b64, t)
    return t


# ---- env-fact extraction from successful Bash command lines ---------------
# A naive "word after the flags" regex over the raw command string mismatches
# badly on multi-token flag/value pairs: "ssh -o BatchMode=yes -o
# ConnectTimeout=5 host" would capture "BatchMode=yes" as the host, and
# "docker logs --since 12h mycontainer" would capture "12h" as the
# container. Fixed by: (1) splitting the command into clauses on
# ;/&&/||/|/newline first, so a flag from one pipeline stage never leaks
# into the next command's parse, (2) tokenizing each clause with shlex, and
# (3) walking tokens with an explicit "does this flag consume the next
# token" table per tool, so a flag's value is skipped as one unit rather
# than mistaken for the positional target.

_CLAUSE_SPLIT_RE = re.compile(r'(?:;|\n|&&|\|\|?)')
_BAD_HOST_WORDS = {'then', 'do', 'done', 'fi', 'true', 'false'}
# Wrapper commands that can legitimately precede ssh/docker at clause-start
# without themselves being the command ("sudo ssh host", "timeout 10 ssh host").
_WRAPPER_CMDS_NOARG = {'sudo', 'nohup', 'time'}
_WRAPPER_CMDS_ARG = {'timeout', 'nice'}
# Strict hostname/alias shape: letters/digits/underscore/dot/hyphen, optional
# single "user@" prefix. Rejects shell noise that survives naive tokenizing
# ("2>/dev/null", "./", "$(...)" fragments) which are not valid hostnames.
_HOSTLIKE_RE = re.compile(r'^(?:[A-Za-z0-9_][A-Za-z0-9_.\-]*@)?[A-Za-z0-9_][A-Za-z0-9_.\-]*$')

# ssh(1): short options that consume a following value (per manpage).
_SSH_ARG_SHORT = set('BbcDEeFIiJLlmOoQpRSWw')
# docker exec/logs/inspect/restart/stop/start/cp: options that consume a value.
_DOCKER_ARG_SHORT = set('uwe')
_DOCKER_ARG_LONG = {'--user', '--workdir', '--env', '--since', '--until',
                     '--tail', '--format', '--filter', '--platform'}
_DOCKER_SUBCMDS = {'exec', 'logs', 'inspect', 'restart', 'stop', 'start', 'cp'}

_KUBECTL_CTX_RE = re.compile(r'\bkubectl\b[^;&|\n]*?--context[= ]([A-Za-z0-9_.\-]+)')
_KUBECTL_NS_RE = re.compile(r'\bkubectl\b[^;&|\n]*?(?:-n\s+|--namespace[= ])([A-Za-z0-9_.\-]+)')
_URL_RE = re.compile(r'https?://[^\s"\'`)]+')

_HOSTPORT_RE = re.compile(
    r'\b(?:localhost|\d{1,3}(?:\.\d{1,3}){3}|[A-Za-z0-9_.\-]+\.(?:com|net|org|io|dev|ru|su|local|internal)):(\d{2,5})\b')
_PORT_FLAG_RE = re.compile(r'--port[= ](\d{2,5})\b')
_PORT_ENV_RE = re.compile(r'\bPORT=(\d{2,5})\b')
_DOCKER_PUBLISH_RE = re.compile(r'-p\s+(\d{2,5}):\d{1,5}\b')


def _split_clauses(cmd):
    return [c.strip() for c in _CLAUSE_SPLIT_RE.split(cmd) if c.strip()]


def _tokenize(clause):
    try:
        return shlex.split(clause, posix=True)
    except ValueError:
        return clause.split()


def _clean_target(tok):
    if not tok:
        return None
    if tok in _BAD_HOST_WORDS:
        return None
    if not _HOSTLIKE_RE.match(tok):
        return None
    if tok.isdigit():
        return None
    if len(tok) < 2 or len(tok) > 100:
        return None
    return tok


def _skip_wrappers(toks):
    i = 0
    while i < len(toks):
        if toks[i] in _WRAPPER_CMDS_NOARG:
            i += 1
        elif toks[i] in _WRAPPER_CMDS_ARG:
            i += 2
        else:
            break
    return i


def _first_positional(tokens, start, arg_short, arg_long=()):
    i = start
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith('--'):
            i += 2 if tok in arg_long else 1
            continue
        if len(tok) >= 2 and tok[0] == '-' and not tok.startswith('--'):
            i += 2 if tok[1] in arg_short else 1
            continue
        return tok
    return None


def _harvest_ssh_docker(cmd, facts):
    # ssh/docker detection requires the (wrapper-stripped) clause to actually
    # START with the tool -- not just contain the word "ssh"/"docker"
    # somewhere -- so "command -v ssh 2>/dev/null" or "which docker" or a
    # heredoc line that merely mentions "ssh" in passing does not misfire.
    # This is still a regex/tokenizer heuristic, not a real shell parser: a
    # heredoc body (<<'EOF' ... EOF) is not recognized as a single unit and
    # its lines are split like any other clause, so a multi-line script
    # being *written* (not executed) that itself begins a line with "ssh "
    # or "docker " can still produce a false hit. Accepted trade-off --
    # full POSIX shell parsing is out of scope for a best-effort snapshot.
    for clause in _split_clauses(cmd):
        toks = _tokenize(clause)
        if not toks:
            continue
        start = _skip_wrappers(toks)
        if start >= len(toks):
            continue
        if toks[start] == 'ssh':
            host = _clean_target(_first_positional(toks, start + 1, _SSH_ARG_SHORT))
            if host:
                facts['ssh'].add(host)
        elif toks[start] in ('docker', 'docker-compose'):
            i = start + 1
            if i < len(toks) and toks[i] == 'compose':
                i += 1
            if i < len(toks) and toks[i] in _DOCKER_SUBCMDS:
                target = _clean_target(_first_positional(toks, i + 1, _DOCKER_ARG_SHORT, _DOCKER_ARG_LONG))
                if target:
                    facts['docker'].add(target)


CMD_ANALYSIS_MAX_CHARS = 8000
RESULT_ANALYSIS_MAX_CHARS = 4000
# v_usefulness #1: потерянный факт в trading-bots/c49b0d3e — соответствие
# хостового пути и пути внутри контейнера (/docker/trading-bots/arb-state ->
# /app/output). Ни один из прежних сборщиков не смотрел ни на -v/--volume,
# ни на volumes: в compose, ни на вывод команд.
_DOCKER_VOL_RE = re.compile(r'(?:-v|--volume)[= ]([./~][^\s:]*|/[^\s:]*):(/[^\s:,]*)')
_COMPOSE_VOL_RE = re.compile(r'^\s*-\s+["\']?([./][^\s:"\']*|/[^\s:"\']*):(/[^\s:"\']*)', re.MULTILINE)


def harvest_path_maps(text, facts, limit_chars):
    for m in _DOCKER_VOL_RE.finditer(text[:limit_chars]):
        facts['pathmap'].add('%s -> %s' % (m.group(1), m.group(2)))
    for m in _COMPOSE_VOL_RE.finditer(text[:limit_chars]):
        facts['pathmap'].add('%s -> %s' % (m.group(1), m.group(2)))


def harvest_env_facts(cmd, facts):
    # v_break-it #1: бюджет сканирования проверяется только между строками,
    # поэтому одна гигантская команда могла крутить regex/shlex неограниченно.
    cmd = cmd[:CMD_ANALYSIS_MAX_CHARS]
    # shlex-tokenizing every clause of every successful Bash command was the
    # dominant cost (profiled: ~75% of total wall time on a 45MB/19k-line
    # real transcript, dwarfing JSON parsing). Most Bash commands mention
    # neither "ssh" nor "docker" at all, so a cheap substring pre-check
    # skips the clause-split+shlex work entirely for them; the remaining
    # (regex-only, no tokenizing) extractors below always run.
    if 'ssh' in cmd or 'docker' in cmd:
        _harvest_ssh_docker(cmd, facts)
    if '-v ' in cmd or '--volume' in cmd:
        harvest_path_maps(cmd, facts, CMD_ANALYSIS_MAX_CHARS)
    for m in _KUBECTL_CTX_RE.finditer(cmd):
        facts['kubectl_context'].add(m.group(1))
    for m in _KUBECTL_NS_RE.finditer(cmd):
        facts['kubectl_ns'].add(m.group(1))
    for m in _URL_RE.finditer(cmd):
        u = _URL_USERINFO_RE.sub(lambda x: x.group(1) + '[REDACTED]' + x.group(4), m.group(0))
        facts['url'].add(u.rstrip('.,;)'))
    for rx in (_HOSTPORT_RE, _PORT_FLAG_RE, _PORT_ENV_RE, _DOCKER_PUBLISH_RE):
        for m in rx.finditer(cmd):
            facts['port'].add(m.group(1))


_ACCEPT_CMD_RE = re.compile(
    r'(?i)(^|[;&|]\s*)('
    r'make(\s+\S+)?|'
    r'npm\s+(run\s+)?(test|lint|build)|'
    r'yarn\s+(test|lint|build)|'
    r'pnpm\s+(test|lint|build)|'
    r'pytest\S*|'
    r'python3?\s+-m\s+pytest|'
    r'go\s+(test|vet|build)|'
    r'cargo\s+(test|build|clippy)|'
    r'mvn\s+test|'
    r'gradle\w*\s+test|'
    r'tox\b|'
    r'rspec\b|'
    r'phpunit\b|'
    r'ruff\s+check|'
    r'eslint\b|'
    r'golangci-lint\b'
    r')'
)


def is_acceptance_command(cmd):
    cmd = cmd[:CMD_ANALYSIS_MAX_CHARS]
    return bool(_ACCEPT_CMD_RE.search(cmd))


# ---- PM slug / plan.md detection -------------------------------------------
_PLAN_RE = re.compile(r'\.claude/pm/([A-Za-z0-9_.\-]+)/plan\.md')


def main():
    t0 = time.time()
    raw = sys.stdin.read()
    hook_in = json.loads(raw)

    _sid_raw = hook_in.get('session_id') or ''
    session_id = _sid_raw if _SAFE_SESSION_ID_RE.match(_sid_raw) else 'unknown-session'
    transcript_path = hook_in.get('transcript_path') or ''
    hook_cwd = hook_in.get('cwd') or ''
    trigger = hook_in.get('trigger') or 'auto'

    out_dir = os.path.join(os.path.expanduser('~'), '.claude', 'compact-state')
    os.makedirs(out_dir, exist_ok=True)
    try:
        os.chmod(out_dir, 0o700)
    except OSError:
        pass
    out_path = os.path.join(out_dir, '%s.md' % session_id)

    # ---- accumulators (all bounded -> O(1) memory regardless of file size) --
    last_todo = None            # list[dict] or None
    last_todo_ts = None
    files_seen = OrderedDict()  # file_path -> (cwd_at_edit, tool_name)  (move-to-end = most recent)
    env_facts = {
        'ssh': set(), 'docker': set(), 'kubectl_context': set(),
        'kubectl_ns': set(), 'url': set(), 'port': set(),
        'pathmap': set(),
    }
    accept_cmds = OrderedDict()  # command text -> True (dedup, insertion order = chronological)
    pending_bash = {}           # tool_use_id -> command text (only Bash)
    last_git_branch = None
    last_cwd = hook_cwd or None
    plan_path_seen = None       # last seen ".../.claude/pm/<slug>/plan.md" match
    user_prompts = deque(maxlen=PROMPTS_LIMIT)  # (timestamp, text)

    n_lines = 0
    n_parse_errors = 0
    truncated_scan = False

    # Defensive wall-clock budget: this hook's process time directly adds to
    # every compaction's latency (PreCompact hooks run before the summarizer
    # call), so a pathological transcript (e.g. one absurdly long line, or
    # thousands of ssh/docker Bash calls) must not be allowed to stall
    # compaction for real. Measured cost is ~0.4-0.9s on real 40-45MB/~20k-
    # line transcripts; this budget is a wide multiple of that, checked
    # cheaply (every 3000 lines, one time.time() call) so it never fires in
    # the normal case.
    SCAN_TIME_BUDGET_SEC = 3.0
    TIME_CHECK_EVERY = 3000

    def note_plan_ref(text):
        nonlocal plan_path_seen
        if not text:
            return
        m = _PLAN_RE.search(text)
        if m:
            plan_path_seen = m.group(0)

    try:
        with open(transcript_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                n_lines += 1
                if n_lines % TIME_CHECK_EVERY == 0 and (time.time() - t0) > SCAN_TIME_BUDGET_SEC:
                    truncated_scan = True
                    break
                line = line.strip()
                if not line:
                    continue
                # cheap pre-filter before the json.loads cost
                if '"type":"assistant"' not in line and '"type":"user"' not in line \
                        and '"type": "assistant"' not in line and '"type": "user"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    n_parse_errors += 1
                    continue

                t = obj.get('type')
                if t not in ('assistant', 'user'):
                    continue

                gb = obj.get('gitBranch')
                if gb:
                    last_git_branch = gb
                cw = obj.get('cwd')
                if cw:
                    last_cwd = cw

                msg = obj.get('message') or {}
                content = msg.get('content')

                if t == 'assistant':
                    if isinstance(content, list):
                        for c in content:
                            if not isinstance(c, dict) or c.get('type') != 'tool_use':
                                continue
                            name = c.get('name')
                            inp = c.get('input') or {}
                            if name == 'TodoWrite':
                                todos = inp.get('todos')
                                if isinstance(todos, list):
                                    last_todo = todos
                                    last_todo_ts = obj.get('timestamp')
                            elif name in ('Edit', 'Write', 'MultiEdit', 'NotebookEdit'):
                                fp = inp.get('file_path') or inp.get('notebook_path')
                                if fp:
                                    if fp in files_seen:
                                        files_seen.move_to_end(fp)
                                    files_seen[fp] = (cw or last_cwd, name)
                            elif name == 'Bash':
                                cmd = inp.get('command')
                                tid = c.get('id')
                                if cmd and tid:
                                    pending_bash[tid] = cmd
                                    note_plan_ref(cmd)

                elif t == 'user':
                    if isinstance(content, list):
                        has_tool_result = False
                        for c in content:
                            if not isinstance(c, dict):
                                continue
                            if c.get('type') == 'tool_result':
                                has_tool_result = True
                                tid = c.get('tool_use_id')
                                cmd = pending_bash.pop(tid, None) if tid else None
                                if cmd is not None:
                                    ok = not c.get('is_error')
                                    tur = obj.get('toolUseResult')
                                    if isinstance(tur, dict) and tur.get('interrupted'):
                                        ok = False
                                    if ok:
                                        harvest_env_facts(cmd, env_facts)
                                        if ('docker' in cmd or 'compose' in cmd
                                                or 'mount' in cmd):
                                            rtext = c.get('content')
                                            if isinstance(rtext, list):
                                                rtext = ' '.join(
                                                    x.get('text', '') for x in rtext
                                                    if isinstance(x, dict))
                                            if isinstance(rtext, str) and rtext:
                                                harvest_path_maps(
                                                    rtext, env_facts,
                                                    RESULT_ANALYSIS_MAX_CHARS)
                                        if is_acceptance_command(cmd):
                                            if cmd in accept_cmds:
                                                del accept_cmds[cmd]
                                            accept_cmds[cmd] = True
                                            if len(accept_cmds) > ACCEPT_CMDS_LIMIT:
                                                accept_cmds.popitem(last=False)
                        if not has_tool_result and not obj.get('isSidechain'):
                            # candidate "real" user prompt: role user, no tool_result
                            # in its content list, not a subagent-internal turn.
                            text = None
                            for c in content:
                                if isinstance(c, dict) and c.get('type') == 'text':
                                    text = c.get('text')
                                    break
                            if text:
                                m = re.search(r'<command-args>(.*?)</command-args>', text, re.DOTALL)
                                if m:
                                    text = m.group(1).strip()
                                if text and not text.lstrip().startswith('<'):
                                    note_plan_ref(text)
                                    user_prompts.append((obj.get('timestamp'), text[:PROMPT_MAX_CHARS]))
                    elif isinstance(content, str):
                        text = content
                        m = re.search(r'<command-args>(.*?)</command-args>', text, re.DOTALL)
                        if m:
                            text = m.group(1).strip()
                        if text and not text.lstrip().startswith('<') and not obj.get('isSidechain'):
                            note_plan_ref(text)
                            user_prompts.append((obj.get('timestamp'), text[:PROMPT_MAX_CHARS]))
    except FileNotFoundError:
        eprint('compact-state.sh: transcript not found:', transcript_path)
        return
    except Exception as e:
        eprint('compact-state.sh: scan error:', repr(e))
        # fall through -- write whatever partial state we collected, still useful

    elapsed = time.time() - t0

    # ---- render -------------------------------------------------------------
    lines = []
    lines.append('# Compact state snapshot')
    lines.append('')
    lines.append('- session_id: `%s`' % session_id)
    lines.append('- trigger: %s' % trigger)
    lines.append('- generated: %s (PreCompact, scan %.3fs, %d transcript lines, %d parse errors)'
                  % (time.strftime('%Y-%m-%d %H:%M:%S'), elapsed, n_lines, n_parse_errors))
    if truncated_scan:
        lines.append('- ⚠ scan stopped early at the %.1fs time budget (transcript only partially read, '
                      'oldest/earliest lines not reached -- everything below reflects only the scanned prefix)'
                      % SCAN_TIME_BUDGET_SEC)
    if last_cwd:
        lines.append('- worktree/cwd: `%s`' % last_cwd)
    if last_git_branch:
        lines.append('- git branch: `%s`' % last_git_branch)
    if plan_path_seen:
        base = last_cwd or ''
        lines.append('- active PM plan: `%s` (referenced under %s)' % (plan_path_seen, base or '?'))
    lines.append('')

    sections = []  # (priority, title, body_lines)  -- lower priority number = kept first when truncating

    # 1. todo state (R2: partial signal, but explicitly requested)
    body = []
    if last_todo:
        body.append('_as of %s_' % (last_todo_ts or '?'))
        for it in last_todo[:TODO_ITEMS_MAX]:
            if isinstance(it, dict):
                status = it.get('status', '?')
                content_ = it.get('content') or it.get('activeForm') or ''
                body.append('- [%s] %s' % (status, redact(content_)))
        if len(last_todo) > TODO_ITEMS_MAX:
            body.append('- ... (%d more items truncated)' % (len(last_todo) - TODO_ITEMS_MAX))
    else:
        body.append('_no TodoWrite call found in this transcript_')
    sections.append((1, 'Latest todo state', body))

    # 2. environment facts from successful Bash commands (R3's #1 proven loss category)
    body = []
    labels = [
        ('ssh', 'SSH hosts/aliases'), ('docker', 'Docker containers/services'),
        ('kubectl_context', 'kubectl contexts'), ('kubectl_ns', 'kubectl namespaces'),
        ('url', 'Base URLs'), ('port', 'Ports'),
        ('pathmap', 'Host -> container path mappings'),
    ]
    any_facts = False
    for key, label in labels:
        vals = sorted(env_facts[key])[:ENV_FACT_LIMIT_PER_CATEGORY]
        if vals:
            any_facts = True
            body.append('- **%s**: %s' % (label, ', '.join('`%s`' % redact(v) for v in vals)))
    if not any_facts:
        body.append('_no ssh/docker/kubectl/url/port facts harvested from successful Bash commands_')
    sections.append((2, 'Environment facts (from successful Bash commands)', body))

    # 3. acceptance-criterion commands that exited 0
    body = []
    if accept_cmds:
        for cmd in accept_cmds.keys():
            body.append('- `%s`' % redact(cmd.strip())[:300])
    else:
        body.append('_no make/test/lint/build invocation observed exiting 0_')
    sections.append((3, 'Acceptance commands that passed (exit 0)', body))

    # 4. last N distinct files edited/written
    body = []
    recent_files = list(files_seen.items())[-FILES_LIMIT:]
    if recent_files:
        for fp, (cw, name) in recent_files:
            body.append('- [%s] `%s`%s' % (name, fp, (' (cwd: `%s`)' % cw) if cw else ''))
    else:
        body.append('_no Edit/Write/MultiEdit/NotebookEdit tool_use in this transcript (subagent edits via Task/Agent are not visible here)_')
    sections.append((4, 'Files edited/written (last %d distinct)' % FILES_LIMIT, body))

    # 5. last real user prompts, verbatim
    body = []
    if user_prompts:
        for ts, text in user_prompts:
            body.append('- _%s_: %s' % (ts or '?', redact(text)))
    else:
        body.append('_no user prompt text captured_')
    sections.append((5, 'Last user prompts (verbatim, up to %d)' % PROMPTS_LIMIT, body))

    # ---- assemble with hard cap, dropping lowest-priority sections' oldest
    #      items first, then whole sections, until the byte budget is met ----
    header_text = '\n'.join(lines) + '\n'
    dropped_notes = []
    budget = MAX_OUTPUT_BYTES - len(header_text.encode('utf-8')) - 200  # reserve for footer

    sections.sort(key=lambda s: s[0])
    body_chunks = []
    for prio, title, body_lines in sections:
        block = '## %s\n\n%s\n\n' % (title, '\n'.join(body_lines))
        block_bytes = len(block.encode('utf-8'))
        if block_bytes <= budget:
            body_chunks.append(block)
            budget -= block_bytes
        else:
            # try truncating this section's body from the front (oldest first)
            kept = list(body_lines)
            while kept:
                kept = kept[1:]  # drop oldest line first
                block = '## %s _(truncated, oldest entries dropped)_\n\n%s\n\n' % (title, '\n'.join(kept))
                block_bytes = len(block.encode('utf-8'))
                if block_bytes <= budget:
                    body_chunks.append(block)
                    budget -= block_bytes
                    dropped_notes.append(title)
                    break
            else:
                dropped_notes.append(title + ' (dropped entirely)')

    out_text = header_text + '\n' + ''.join(body_chunks)
    if dropped_notes:
        out_text += '\n---\n⚠ truncated to fit the %d-byte cap; oldest data dropped first in: %s\n' % (
            MAX_OUTPUT_BYTES, ', '.join(dropped_notes))

    # final full-text safety-net redaction pass (defense in depth on top of
    # the per-value redact() calls already applied above)
    out_text = redact(out_text)

    tmp_path = out_path + '.tmp%d' % os.getpid()
    with open(tmp_path, 'w', encoding='utf-8') as f:
        f.write(out_text)
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, out_path)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        # fail-open: never block compaction, never raise
        eprint('compact-state.sh: fatal (ignored):', repr(e))
    sys.exit(0)
