"""AST scan for pytest node ids, shared by every stage that checks whether a
judged claim's pytest node ids already exist in the worktree (no pytest run,
no venv needed).
"""
from __future__ import annotations

import ast
import functools
from pathlib import Path


@functools.lru_cache(maxsize=None)
def _is_testcase(node: ast.ClassDef) -> bool:
    """pytest collects unittest.TestCase subclasses whatever their name
    (``ParserTests``), not only ``Test*`` classes."""
    for base in node.bases:
        name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
        if name.endswith("TestCase"):
            return True
    return False


def pytest_nodes_in_file(path: Path) -> frozenset[str]:
    """Return every Test*/TestCase-subclass ::test_* and top-level test_* node id a python file defines.

    Cached per path: a caller checking several node ids against the same
    file (a claim can list more than one) only pays the parse cost once.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError, UnicodeDecodeError):
        return frozenset()
    nodes = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            nodes.add(node.name)
        elif isinstance(node, ast.ClassDef) and (node.name.startswith("Test") or _is_testcase(node)):
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name.startswith("test_"):
                    nodes.add(f"{node.name}::{sub.name}")
    return frozenset(nodes)
