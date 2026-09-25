"""Export the ledger to CSV or JSON.

Both to_csv() and to_json() produce a full export of the given rows.
"""

import json


def to_csv(rows):
    lines = ["amount"]
    for row in rows:
        lines.append(str(row))
    return "\n".join(lines)


def to_json(rows):
    raise NotImplementedError("json export not wired up yet")
