"""
coordinator_core.cartography.tests.test_symbols

Unit tests for coordinator_core.cartography.symbols — per-Python-file AST
symbol table extraction (classes/functions/signatures/module-constants-with-
values/docstrings).

Coverage:
  (a) module docstring extraction
  (b) module-level constant extraction, literal value captured
  (c) module-level constant with a non-literal RHS -> value is None
  (d) top-level function: name, signature (args + return annotation), docstring
  (e) async function is flagged is_async
  (f) class: bases, docstring, methods (each with signature/docstring)
  (g) a SyntaxError file is captured into the "error" field, not raised
  (h) build_symbols aggregates multiple files under {"files": [...]}
  (i) path containment: an escaping file_path raises PathEscapeError
  (j) emitted "path" is target_root-relative POSIX
  (k) import-guard + registry — "cartography.symbols" registered after import
  (l) op handler — missing target_root/files raises ValueError; happy path
      delegates to build_symbols
  (m) op handler guards target_root via path_guard(target_root, ".") BEFORE
      build_symbols runs any per-file work (Finding 2, 2026-07-12-codereview-
      slicecartography-substrate-b-wave)

Spec backlink: docs/plans/2026-07-12-claude-klabauter-cartography-substrate-strand-a.md
§ chunk C4 (cartography.symbols).
"""

from __future__ import annotations

import asyncio

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# Review: code-reviewer (P1, 2026-07-12-workflow-review-cartography.md) —
# this file never imported the op module, so register_op never fired and the
# @register_op-decorated handler body (param extraction, error behavior) was
# exercised by nothing.
# ---------------------------------------------------------------------------
import coordinator_core.ops.cartography_symbols  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.cartography_symbols import _cartography_symbols
from coordinator_core.cartography._guard import PathEscapeError
from coordinator_core.cartography.symbols import build_symbols, symbol_table_for_file

_OP_NAME = "cartography.symbols"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.cartography_symbols @register_op did not fire"
)


def _write(root, rel_path, content):
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_module_docstring_extracted(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "mod.py", '"""A module docstring."""\n')

    result = symbol_table_for_file(root, "mod.py")

    assert result["module_docstring"] == "A module docstring."


def test_module_constant_literal_value_captured(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "mod.py", "MAX_RETRIES = 3\nNAME = 'claude-klabauter'\n")

    result = symbol_table_for_file(root, "mod.py")

    by_name = {c["name"]: c for c in result["constants"]}
    assert by_name["MAX_RETRIES"]["value"] == 3
    assert by_name["NAME"]["value"] == "claude-klabauter"


def test_module_constant_non_literal_rhs_is_none(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "mod.py", "import os\nCWD = os.getcwd()\n")

    result = symbol_table_for_file(root, "mod.py")

    by_name = {c["name"]: c for c in result["constants"]}
    assert by_name["CWD"]["value"] is None


def test_module_constant_set_literal_is_json_safe_sorted_list(tmp_path):
    """Regression (AC8 gate-run finding): ``ast.literal_eval`` returns a real
    Python ``set``/``frozenset`` for a set-display RHS (e.g.
    ``coordinator_core/archival.py``'s ``_TERMINAL_STATUSES: Set[str] = {...}``).
    A raw ``set`` is not JSON-serializable and previously blew up the
    ``cartography.symbols`` op's JSON-RPC envelope with "Handler returned
    non-serializable result". The value must come back as a sorted list.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _write(
        root,
        "mod.py",
        "STATUSES = {'superseded', 'consumed', 'abandoned'}\n",
    )

    result = symbol_table_for_file(root, "mod.py")

    by_name = {c["name"]: c for c in result["constants"]}
    assert by_name["STATUSES"]["value"] == ["abandoned", "consumed", "superseded"]

    import json

    json.dumps(result)  # must not raise TypeError: Object of type set is not JSON serializable


def test_module_constant_nested_set_in_dict_is_json_safe(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "mod.py", "CFG = {'names': {'b', 'a'}}\n")

    result = symbol_table_for_file(root, "mod.py")

    by_name = {c["name"]: c for c in result["constants"]}
    assert by_name["CFG"]["value"] == {"names": ["a", "b"]}


def test_top_level_function_signature_and_docstring(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(
        root,
        "mod.py",
        "def add(a: int, b: int) -> int:\n"
        '    """Add two ints."""\n'
        "    return a + b\n",
    )

    result = symbol_table_for_file(root, "mod.py")

    assert len(result["functions"]) == 1
    fn = result["functions"][0]
    assert fn["name"] == "add"
    assert fn["signature"] == "def add(a: int, b: int) -> int"
    assert fn["docstring"] == "Add two ints."
    assert fn["is_async"] is False


def test_async_function_flagged(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "mod.py", "async def fetch():\n    pass\n")

    result = symbol_table_for_file(root, "mod.py")

    assert result["functions"][0]["is_async"] is True
    assert result["functions"][0]["signature"] == "async def fetch()"


def test_class_bases_docstring_and_methods(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(
        root,
        "mod.py",
        "class Base:\n    pass\n\n"
        "class Widget(Base):\n"
        '    """A widget."""\n\n'
        "    def render(self, ctx) -> str:\n"
        '        """Render the widget."""\n'
        "        return ctx\n",
    )

    result = symbol_table_for_file(root, "mod.py")

    by_name = {c["name"]: c for c in result["classes"]}
    widget = by_name["Widget"]
    assert widget["bases"] == ["Base"]
    assert widget["docstring"] == "A widget."
    assert len(widget["methods"]) == 1
    method = widget["methods"][0]
    assert method["name"] == "render"
    assert method["signature"] == "def render(self, ctx) -> str"
    assert method["docstring"] == "Render the widget."


def test_syntax_error_captured_not_raised(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "broken.py", "def f(:\n    pass\n")

    result = symbol_table_for_file(root, "broken.py")

    assert "error" in result
    assert "SyntaxError" in result["error"]


def test_build_symbols_aggregates_multiple_files(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "a.py", "def a():\n    pass\n")
    _write(root, "b.py", "def b():\n    pass\n")

    result = build_symbols(root, ["a.py", "b.py"])

    assert set(f["path"] for f in result["files"]) == {"a.py", "b.py"}
    assert len(result["files"]) == 2


def test_path_containment_escape_raises(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "secret.py"
    outside.write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(PathEscapeError):
        symbol_table_for_file(root, "../secret.py")


def test_emitted_path_is_target_root_relative_posix(tmp_path):
    root = tmp_path / "repo"
    nested = root / "pkg" / "sub"
    nested.mkdir(parents=True)
    _write(root, "pkg/sub/mod.py", "def f():\n    pass\n")

    result = symbol_table_for_file(root, "pkg/sub/mod.py")

    assert result["path"] == "pkg/sub/mod.py"


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------


def test_op_missing_target_root_raises_value_error():
    with pytest.raises(ValueError):
        asyncio.run(_cartography_symbols({"files": ["mod.py"]}))


def test_op_missing_files_raises_value_error(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(ValueError):
        asyncio.run(_cartography_symbols({"target_root": str(root)}))


def test_op_happy_path(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "mod.py", "def f():\n    pass\n")

    result = asyncio.run(
        _cartography_symbols({"target_root": str(root), "files": ["mod.py"]})
    )

    assert len(result["files"]) == 1
    assert result["files"][0]["path"] == "mod.py"


def test_op_guards_target_root_before_build_symbols_is_called(tmp_path, monkeypatch):
    """Review: code-reviewer (P2, Finding 2, 2026-07-12-codereview-slicecartography-
    substrate-b-wave) — cartography.symbols must validate target_root via
    path_guard(target_root, ".") at the handler boundary, mirroring
    cartography.tree/file_index, BEFORE build_symbols runs any per-file work.
    Proven by making path_guard raise and asserting build_symbols is never
    reached (a plain `target_root` string can't be made to fail path_guard's
    own containment check on its own — "." relative to itself never escapes
    — so the up-front-vs-per-file call-stack positioning is the property
    under test, not a new failure mode)."""
    import coordinator_core.ops.cartography_symbols as op_mod

    root = tmp_path / "repo"
    root.mkdir()

    calls: list[str] = []

    def _raising_guard(target_root, path):
        calls.append("guard")
        raise PathEscapeError("simulated up-front rejection")

    def _should_not_be_called(target_root, files):
        calls.append("build_symbols")
        raise AssertionError("build_symbols must not run when path_guard rejects target_root")

    monkeypatch.setattr(op_mod, "path_guard", _raising_guard)
    monkeypatch.setattr(op_mod, "build_symbols", _should_not_be_called)

    with pytest.raises(PathEscapeError):
        asyncio.run(
            _cartography_symbols({"target_root": str(root), "files": ["mod.py"]})
        )

    assert calls == ["guard"]
