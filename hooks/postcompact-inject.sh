#!/usr/bin/env python3
# Re-inject the compact-state.sh snapshot (plus .claude/pm/_active.md, if
# present) into the model's context after a compaction.
#
# WHY THIS IS WIRED UNDER SessionStart (matcher "compact"), NOT PostCompact,
# EVEN THOUGH THE FILENAME SAYS "postcompact":
#
# R1 (byte-level reading of the CC 2.1.258 binary, see
# scratchpad/audit/precompact/r1_hook_contract.md) proved PostCompact cannot
# deliver anything to the model in this build. Its executor (hMe, offset
# 165795409) returns exactly one field:
#     return { userDisplayMessage: v.length > 0 ? v.join("\n") : void 0 }
# `userDisplayMessage` only ever reaches the transcript/UI. `PreCompact` and
# `PostCompact` are the two hook events that are absent from the
# hookSpecificOutput discriminated union entirely (joe.hookSpecificOutput =
# We([voe, Roe, ..., Ooe, ...]) has 22 members and neither PreCompact's nor
# PostCompact's own hookEventName is one of them) -- so there is no amount of
# correctly-shaped JSON a PostCompact hook can print that the model will
# ever see. Wiring the "make the model remember what just got compacted"
# script under PostCompact would ship a hook that runs, costs a subprocess,
# and does *nothing useful* -- worse than not having it, because it would
# look like this problem was solved when it was not.
#
# What DOES reach the model post-compaction: SessionStart with
# source==="compact" (schema $re allows "compact" as a source value; its
# output schema Ooe is the one member of that union carrying
# additionalContext for this event). That is a real, currently-documented
# SessionStart behaviour, not a reverse-engineering trick -- R1 cross-checked
# it against code.claude.com/docs/en/hooks independently of the binary read.
#
# So: this script is dual-mode, driven by hook_event_name on stdin, and
# INSTALL.md wires it into BOTH events for different reasons:
#   - SessionStart (matcher "compact")  -> real injection, JSON on stdout:
#       {"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"..."}}
#   - PostCompact (optional, cosmetic)  -> plain text on stdout, becomes
#     userDisplayMessage: a one-line "snapshot saved, N bytes" confirmation
#     visible to the user in the transcript. Never JSON here -- PostCompact
#     stdout is used as literal display text, not parsed.
# Any other event/source (plain SessionStart on "startup"/"resume"/"clear"/
# "fork") produces no output at all: this hook's job is specifically
# post-compaction continuity, not a general context injector.
#
# CONTRACT (verified in r1_hook_contract.md §1.3/§1.4):
#   stdin  = {"session_id":..., "cwd":..., "hook_event_name": "SessionStart"|"PostCompact",
#             "source": "compact"|"startup"|"resume"|"clear"|"fork",   # SessionStart only
#             "compact_summary": "...", ...}                           # PostCompact only
#   stdout = JSON (SessionStart/compact) | plain text (PostCompact) | nothing
#   exit   = always 0, fail-open, same discipline as compact-state.sh.

import sys
import os
import re
import json

MAX_INJECT_BYTES = 16500  # snapshot is already capped at ~16000B by compact-state.sh; small margin for _active.md


def eprint(*a):
    try:
        print(*a, file=sys.stderr)
    except Exception:
        pass


_SAFE_SESSION_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,128}$')


def read_snapshot(session_id):
    if not _SAFE_SESSION_ID_RE.match(session_id or ''):
        session_id = 'unknown-session'
    path = os.path.join(os.path.expanduser('~'), '.claude', 'compact-state', '%s.md' % session_id)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read(), path
    except FileNotFoundError:
        return None, path
    except Exception as e:
        eprint('postcompact-inject.sh: snapshot read error (ignored):', repr(e))
        return None, path


def read_active_md(cwd):
    if not cwd:
        return None
    path = os.path.join(cwd, '.claude', 'pm', '_active.md')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        return None
    except Exception as e:
        eprint('postcompact-inject.sh: _active.md read error (ignored):', repr(e))
        return None


def build_context(session_id, cwd):
    snapshot, snap_path = read_snapshot(session_id)
    # v_usefulness #2: _active.md уже безусловно отдаётся matcher-less
    # SessionStart-хуком на КАЖДОМ старте, включая source="compact".
    # Повторная вставка здесь — чистое дублирование токенов.
    active = None
    if not snapshot and not active:
        return None, snap_path
    parts = []
    if snapshot:
        parts.append(snapshot.strip())
    if active:
        parts.append('## Active work (.claude/pm/_active.md)\n\n' + active.strip())
    text = '\n\n---\n\n'.join(parts)
    if len(text.encode('utf-8')) > MAX_INJECT_BYTES:
        text = text.encode('utf-8')[:MAX_INJECT_BYTES].decode('utf-8', 'ignore') + \
            '\n\n... [truncated to fit injection budget]'
    return text, snap_path


def main():
    raw = sys.stdin.read()
    hook_in = json.loads(raw)

    event = hook_in.get('hook_event_name')
    session_id = hook_in.get('session_id') or ''
    cwd = hook_in.get('cwd') or ''

    if event == 'SessionStart':
        if hook_in.get('source') != 'compact':
            return  # not a post-compaction start -- nothing to inject
        text, _ = build_context(session_id, cwd)
        if not text:
            return
        print(json.dumps({
            'hookSpecificOutput': {
                'hookEventName': 'SessionStart',
                'additionalContext': text,
            }
        }))
    elif event == 'PostCompact':
        # Cosmetic only (see header comment): PostCompact stdout becomes
        # userDisplayMessage, plain text, never parsed as JSON, never seen
        # by the model. Kept purely so the user sees confirmation in the
        # transcript that a snapshot was written and where.
        snapshot, snap_path = read_snapshot(session_id)
        if snapshot:
            print('state snapshot saved (%d bytes): %s' % (len(snapshot.encode('utf-8')), snap_path))
        # else: silent -- compact-state.sh (PreCompact) presumably failed
        # or produced nothing; nothing useful to tell the user here.
    # else: some other event this script was not meant to be wired to -- silent.


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        eprint('postcompact-inject.sh: fatal (ignored):', repr(e))
    sys.exit(0)
