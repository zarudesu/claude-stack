"""Single source of truth for banned identity language and secret-shaped text.

IDENTITY is the union of the identity tokens found across the last run's
finder/judge/doc/fleet workflows, in the order they first appeared, followed
by the eleven style words in fixed order. IDENTITY_TEXT is the exact
paragraph every pipeline/workflows/*.js embeds; check_identity_sync.py
compares each workflow's `const IDENTITY = ...` literal against it, so a
change here must be pasted into every workflow file verbatim.

SENSITIVE holds generic secret-shaped patterns (ipv4, domain, a generic
infra hostname shape, ssh user) for scanning free text before it is
written into a file that ships with a repository. It carries no
repository-specific allow entries -- SENSITIVE_ALLOW is limited to
standard loopback/example placeholders that are never actually sensitive.
"""
from __future__ import annotations

import re

# Identity tokens, then the eleven style words, fixed order. Two named
# lists concatenated, rather than one list plus a slice off its tail:
# a slice would let an edit that keeps _check_identity_sync() happy --
# appending one word to both IDENTITY and the Avoid sentence -- silently
# shift which words STYLE_WORDS picks up.
IDENTITY_TOKENS: list[str] = [
    "Claude",
    "Anthropic",
    "ChatGPT",
    "GPT",
    "Copilot",
    "LLM",
    "AI",
    "AI-assisted",
    "AI-generated",
    "coding agent",
    "language model",
    "assistant",
    "Co-Authored-By",
]

STYLE_WORDS: list[str] = [
    "delve",
    "leverage",
    "comprehensive",
    "robust",
    "seamless",
    "streamline",
    "consolidate",
    "modernize",
    "enhanced",
    "utilize",
    "facilitate",
]

IDENTITY: list[str] = IDENTITY_TOKENS + STYLE_WORDS

IDENTITY_TEXT: str = (
    "Your output may end up committed to a pushed repository. Write plain, "
    "neutral engineering prose, as any developer on the team would. Never "
    "mention Claude, Anthropic, ChatGPT, GPT, Copilot, LLM, \"AI\", "
    "\"AI-assisted\", \"AI-generated\", \"coding agent\", \"language model\", "
    "\"assistant\", or add a Co-Authored-By line anywhere -- not in claim "
    "text, notes, code comments, commit messages, or filenames. Avoid: "
    "delve, leverage, comprehensive, robust, seamless, streamline, "
    "consolidate, modernize, enhanced, utilize, facilitate."
)

_MENTION_BLOCK_RE = re.compile(r"Never mention (.+?), or add a (.+?) line anywhere", re.S)
_AVOID_BLOCK_RE = re.compile(r"Avoid: (.+?)\.\s*$", re.S)


def _paragraph_tokens(text: str) -> set[str]:
    """Pull the enumerated identity tokens back out of IDENTITY_TEXT's own wording."""
    mention = _MENTION_BLOCK_RE.search(text)
    avoid = _AVOID_BLOCK_RE.search(text)
    if not mention or not avoid:
        raise RuntimeError("IDENTITY_TEXT no longer matches the expected sentence shape")
    items = [w.strip().strip('"') for w in mention.group(1).split(",")]
    items.append(mention.group(2).strip())
    items += [w.strip() for w in avoid.group(1).split(",")]
    return set(items)


def _check_identity_sync() -> None:
    """Keep IDENTITY and IDENTITY_TEXT from drifting apart, in both directions.

    Runs unconditionally (not via `assert`, which `python -O` strips) and
    checks both that every IDENTITY token appears in the paragraph and
    that the paragraph names no token IDENTITY does not track.
    """
    missing = [w for w in IDENTITY if w not in IDENTITY_TEXT]
    if missing:
        raise RuntimeError(f"IDENTITY tokens missing from IDENTITY_TEXT: {missing}")
    extra = _paragraph_tokens(IDENTITY_TEXT) - set(IDENTITY)
    if extra:
        raise RuntimeError(f"IDENTITY_TEXT names tokens absent from IDENTITY: {sorted(extra)}")


_check_identity_sync()

_IDENTITY_RE = [(w, re.compile(r"\b" + re.escape(w) + r"\b", re.IGNORECASE)) for w in IDENTITY]


def scan_identity(text: str):
    """Return [(word, matched_text)] for every IDENTITY token found in text."""
    hits = []
    if not text:
        return hits
    for word, pat in _IDENTITY_RE:
        m = pat.search(text)
        if m:
            hits.append((word, m.group(0)))
    return hits


CLAUDE_MD_RE = re.compile(r"CLAUDE\.md:\d+")

# Generic infra role prefixes for the hostname pattern -- deliberately not
# tied to any one repository's naming scheme, and deliberately limited to
# prefixes that read as infrastructure on their own, with no digit needed
# to tell them apart from prose. Ordinary tech words (web, api, test, dev,
# stage, node, cache, mail, ssh) are left out: they match constantly as
# plain English or generic tech vocabulary (web-based, api-gateway,
# test-fixture, node-18, api-v2, ssh-keygen, cache-control) and a
# following digit does not distinguish those from a real host label
# (node-18, test-case-3 are ordinary, not host names).
_HOSTNAME_PREFIXES = (
    "prod", "staging", "db", "host", "vpn", "proxy", "relay", "gateway",
    "lb", "smtp", "bastion",
)

SENSITIVE: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"), "ipv4"),
    (re.compile(
        r"\b[A-Za-z0-9-]+\.(?:com|net|org|ru|dev|io|sh|cc|co|de|nl|uk|cz|ch|dk|by|tr|"
        r"me|pro|site|online|app|xyz|link|top|su)\b", re.IGNORECASE,
    ), "domain"),
    (re.compile(
        r"\b(?:" + "|".join(_HOSTNAME_PREFIXES) + r")-[a-z0-9-]+\b", re.IGNORECASE,
    ), "hostname"),
    (re.compile(r"\broot@"), "ssh-user"),
]

# Exact values only -- never a substring test, which would let e.g. an
# allowed "0.0.0.0" swallow every real address that merely contains it
# (a plain "10.0.0.0" would count as allowed under a substring test).
# Limited to values a SENSITIVE pattern can actually produce: the domain
# rule needs a dot plus a listed TLD, so "localhost" and "test.local"
# (no TLD in the set) never reach this list in the first place.
SENSITIVE_ALLOW = (
    "127.0.0.1",
    "example.com", "example.org",
)


def scan(text: str):
    """Return [(name, matched_text)] for every SENSITIVE hit not in SENSITIVE_ALLOW."""
    hits = []
    if not text:
        return hits
    for pat, name in SENSITIVE:
        for mo in pat.finditer(text):
            val = mo.group(0)
            if val in SENSITIVE_ALLOW:
                continue
            hits.append((name, val))
    return hits
