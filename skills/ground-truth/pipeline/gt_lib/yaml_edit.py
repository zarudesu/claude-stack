"""Entry-scoped text edits for STATUS.yaml.

STATUS.yaml's claims and debt entries share one block shape (`- id: <id>`
followed by indented fields), so a handful of regex-based helpers can add,
read and rewrite single fields without a full YAML round-trip that would
reflow comments, quoting and key order across the whole file.

blocks() indexes every entry by its id (not by position) -- later edits
look an entry up by id and never rely on block order matching write order,
which mattered once a prior stage started reordering or removing entries
(see the run journal's line-drift lessons, p.18). yaml is imported lazily
inside get_field() because most callers never touch a multi-line field and
should not pay for the import.

An entry's `- id:` line can sit at any indentation (column 0, as
yaml.safe_dump writes it, or nested under a mapping key the way the
shipped STATUS.yaml.template shows it for readability) as long as that
entry's own fields are indented two spaces deeper and any wrapped
continuation line two spaces deeper still -- set_field/get_field/
remove_block all key off the indentation of the specific block they are
given, not a hardcoded column.
"""
import json
import re

ENTRY = re.compile(r'^( *)- id: (\S+)$\n((?:\1  .*\n)+)', re.M)


def blocks(t):
    """Return {id: full_block_text} for every `- id: ...` entry in t."""
    return {m.group(2): m.group(0) for m in ENTRY.finditer(t)}


def _indent(b):
    """Return the leading whitespace of block b's own `- id:` line."""
    return re.match(r' *', b).group(0)


def scalar(s):
    """Render s as a YAML scalar, quoting it whenever the bare (plain) form
    would not parse back to exactly this string.

    Hand-picking which characters make a plain scalar unsafe (a leading '-',
    a trailing ':', a bare 'true') misses cases; asking the same parser
    set_field's output is read back with does not.
    """
    import yaml
    try:
        parsed = yaml.safe_load('x: ' + s)
    except yaml.YAMLError:
        parsed = None
    if isinstance(parsed, dict) and parsed.get('x') == s and 'x' in parsed:
        return s
    return json.dumps(s, ensure_ascii=False)


def set_field(t, i, field, new):
    """Replace field's value inside entry i, keeping wrapped continuation lines out of the match."""
    b = blocks(t)[i]
    ind = _indent(b)
    m = re.search(r'^' + ind + '  ' + field + r': .*$(?:\n' + ind + '    (?!- ).*$)*', b, re.M)
    assert m, (i, field)
    nb = b[:m.start()] + ind + '  ' + field + ': ' + scalar(new) + b[m.end():]
    assert t.count(b) == 1
    return t.replace(b, nb)


def remove_block(t, i):
    """Remove entry i's whole block."""
    b = blocks(t)[i]
    assert t.count(b) == 1
    return t.replace(b, '')


def get_field(t, i, field):
    """Return field's value for entry i, or None if the field is absent.

    Handles a value wrapped across continuation lines whether it is a
    plain scalar (folded to a single space-joined line, per YAML's own
    line-folding rule) or an explicit block scalar (`>` or `|`) -- both
    are re-anchored to column 0 before parsing so a nested entry's deeper
    indentation never confuses the YAML scanner. A single-line value goes
    through the same yaml.safe_load path so quoting and an inline
    `# comment` are stripped the same way regardless of whether the value
    wraps.
    """
    b = blocks(t)[i]
    ind = _indent(b)
    m = re.search(r'^' + ind + '  ' + field + r': (.*)$(?:\n' + ind + '    (?!- ).*$)*', b, re.M)
    if not m:
        return None
    import yaml
    strip_n = len(ind) + 2
    snippet = '\n'.join(
        line[strip_n:] if line[:strip_n].strip() == '' else line
        for line in m.group(0).split('\n')
    )
    return yaml.safe_load(snippet)[field]
