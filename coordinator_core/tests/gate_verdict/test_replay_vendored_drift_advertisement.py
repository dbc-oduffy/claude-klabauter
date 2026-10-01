"""Replay guard: `claude-klabauter-revendor-schema --list` claims a gate only where a drift oracle backs it.

BACKED is derived by AST over test_schema_validate.py: a schema is backed when a test function
calls `check_schema_drift` with an argument naming `<name>.schema.json`, as a string constant or
through a module-level constant assigned such a path. A call forwarding `check_schema_drift` as an
argument (the pin-tracked `_skip_if_probe_unavailable` form) counts. Fixture schemas in tmp dirs
are not vendored names and drop out.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import re
import sys
from pathlib import Path

GATE_VERDICT_CASE = "vendored-schema-drift"

_REPO = Path(__file__).resolve().parents[3]
_ORACLES = _REPO / "coordinator_core/frontmatter/tests/test_schema_validate.py"
_SCRIPT = _REPO / "bin/claude-klabauter-revendor-schema.py"
_SCHEMA_NAME_RE = re.compile(r"([A-Za-z0-9_-]+)\.schema\.json$")

_spec = importlib.util.spec_from_file_location("_revendor_schema_drift_adv", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]


def _names_in(node: ast.AST) -> set[str]:
    return {
        m.group(1)
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        for m in [_SCHEMA_NAME_RE.search(n.value)]
        if m
    }


def _backed_schemas() -> set[str]:
    tree = ast.parse(_ORACLES.read_text(encoding="utf-8"))
    constants = {
        t.id: _names_in(stmt.value)
        for stmt in tree.body
        if isinstance(stmt, ast.Assign)
        for t in stmt.targets
        if isinstance(t, ast.Name)
    }
    backed: set[str] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for call in ast.walk(fn):
            if not isinstance(call, ast.Call):
                continue
            callee = call.func
            callee_name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", "")
            forwarded = any(
                isinstance(a, ast.Name) and a.id == "check_schema_drift" for a in call.args
            )
            if callee_name != "check_schema_drift" and not forwarded:
                continue
            for arg in [*call.args, *(k.value for k in call.keywords)]:
                backed |= _names_in(arg)
                for n in ast.walk(arg):
                    if isinstance(n, ast.Name):
                        backed |= constants.get(n.id, set())
    return backed & set(_mod._vendored_names())


def test_gated_equals_backed_set():
    backed = _backed_schemas()
    assert backed, "AST derivation found no check_schema_drift oracle; the guard is blind"
    gated = set(_mod._GATED)
    assert gated - backed == set(), f"_GATED names schemas with no drift oracle: {sorted(gated - backed)}"
    assert backed - gated == set(), f"backed schemas missing from _GATED: {sorted(backed - gated)}"


def test_list_names_a_gate_only_for_gated_schemas():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        _mod._print_list()
    gated = set(_mod._GATED)
    seen = set()
    for line in buf.getvalue().splitlines():
        parts = line.split()
        if not parts or parts[0] not in set(_mod._vendored_names()):
            continue
        seen.add(parts[0])
        if parts[0] not in gated:
            assert "gate" not in line.lower(), f"--list advertises a gate without an oracle: {line!r}"
    assert seen == set(_mod._vendored_names())
