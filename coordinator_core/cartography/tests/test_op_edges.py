"""
coordinator_core.cartography.tests.test_op_edges

Unit tests for coordinator_core.cartography.op_edges (pure functions) and the
thin cartography.op_edges op wrapper (coordinator_core/ops/
cartography_op_edges.py).

Coverage:
  (a) register_op decorator-form and direct-call-form sites both recorded
  (b) get_op_handler literal-arg site recorded, non-literal arg ignored
  (c) dispatch_message inline-dict-literal site recorded; a variable arg
      (the dominant real-repo shape) contributes nothing
  (d) de-duplication: N register_op sites for the SAME op name across
      multiple files collapse to ONE entry in op_names, while
      registration_site_count still counts every site (866-sites/211-names
      shape this module is built against)
  (e) producer -> consumer edge join by literal op-name string equality,
      across files and within one file
  (f) an op name with no registration site anywhere in `files` produces no
      edge
  (g) a SyntaxError file is captured into the "error" field, not raised
  (h) path containment: an escaping file_path raises PathEscapeError
  (i) op handler — missing target_root/files raises ValueError; happy path
      delegates to build_op_edges
  (j) import-guard + registry — "cartography.op_edges" registered after
      import

Spec backlink: cross-repo memo, 2026-08-06 architecture survey.
"""

from __future__ import annotations

import asyncio

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.cartography_op_edges  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.cartography_op_edges import _cartography_op_edges
from coordinator_core.cartography._guard import PathEscapeError
from coordinator_core.cartography.op_edges import build_op_edges, op_edges_for_file

_OP_NAME = "cartography.op_edges"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.cartography_op_edges @register_op did not fire"
)


def _write(tmp_path, rel, content):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_register_op_decorator_and_direct_call_forms(tmp_path):
    _write(
        tmp_path,
        "producer.py",
        "from coordinator_core.ipc import register_op\n"
        "\n"
        "@register_op('demo.decorator')\n"
        "async def _h(params, repo_root=None):\n"
        "    return {}\n"
        "\n"
        "def _direct(params, repo_root=None):\n"
        "    return {}\n"
        "\n"
        "register_op('demo.direct', _direct)\n",
    )
    entry = op_edges_for_file(tmp_path, "producer.py")
    assert sorted(entry["registrations"]) == ["demo.decorator", "demo.direct"]


def test_get_op_handler_literal_recorded_nonliteral_ignored(tmp_path):
    _write(
        tmp_path,
        "consumer.py",
        "from coordinator_core.ipc import get_op_handler\n"
        "\n"
        "def f(op_name):\n"
        "    get_op_handler('demo.decorator')\n"
        "    get_op_handler(op_name)\n",
    )
    entry = op_edges_for_file(tmp_path, "consumer.py")
    assert entry["lookups"] == ["demo.decorator"]


def test_dispatch_message_inline_literal_recorded_variable_ignored(tmp_path):
    _write(
        tmp_path,
        "dispatcher.py",
        "async def f(msg):\n"
        "    await dispatch_message({'method': 'demo.decorator', 'params': {}})\n"
        "    await dispatch_message(msg)\n",
    )
    entry = op_edges_for_file(tmp_path, "dispatcher.py")
    assert entry["dispatches"] == ["demo.decorator"]


def test_dedup_op_names_vs_raw_registration_site_count(tmp_path):
    _write(
        tmp_path,
        "a.py",
        "from coordinator_core.ipc import register_op\n\n"
        "@register_op('demo.shared')\n"
        "async def _a(params, repo_root=None):\n    return {}\n",
    )
    _write(
        tmp_path,
        "b.py",
        "from coordinator_core.ipc import register_op\n\n"
        "@register_op('demo.shared')\n"
        "async def _b(params, repo_root=None):\n    return {}\n",
    )
    _write(
        tmp_path,
        "c.py",
        "from coordinator_core.ipc import register_op\n\n"
        "@register_op('demo.other')\n"
        "async def _c(params, repo_root=None):\n    return {}\n",
    )
    result = build_op_edges(tmp_path, ["a.py", "b.py", "c.py"])
    assert result["op_names"] == ["demo.other", "demo.shared"]
    assert result["registration_site_count"] == 3
    assert result["static_only"] is True


def test_producer_consumer_edge_join_across_and_within_files(tmp_path):
    _write(
        tmp_path,
        "producer.py",
        "from coordinator_core.ipc import register_op\n\n"
        "@register_op('demo.op')\n"
        "async def _h(params, repo_root=None):\n    return {}\n",
    )
    _write(
        tmp_path,
        "consumer.py",
        "from coordinator_core.ipc import get_op_handler\n\n"
        "def f():\n    get_op_handler('demo.op')\n",
    )
    _write(
        tmp_path,
        "self_referential.py",
        "from coordinator_core.ipc import register_op, get_op_handler\n\n"
        "@register_op('demo.self')\n"
        "async def _h(params, repo_root=None):\n    return {}\n\n"
        "def f():\n    get_op_handler('demo.self')\n",
    )
    result = build_op_edges(
        tmp_path, ["producer.py", "consumer.py", "self_referential.py"]
    )
    edges = {(e["op"], e["from"], e["to"], e["kind"]) for e in result["edges"]}
    assert ("demo.op", "producer.py", "consumer.py", "get_op_handler") in edges
    assert (
        "demo.self",
        "self_referential.py",
        "self_referential.py",
        "get_op_handler",
    ) in edges
    assert len(edges) == 2


def test_op_with_no_registration_site_produces_no_edge(tmp_path):
    _write(
        tmp_path,
        "consumer.py",
        "from coordinator_core.ipc import get_op_handler\n\n"
        "def f():\n    get_op_handler('demo.unregistered')\n",
    )
    result = build_op_edges(tmp_path, ["consumer.py"])
    assert result["edges"] == []
    assert result["op_names"] == []


def test_syntax_error_captured_not_raised(tmp_path):
    _write(tmp_path, "bad.py", "def f(:\n    pass\n")
    entry = op_edges_for_file(tmp_path, "bad.py")
    assert entry["registrations"] == []
    assert "SyntaxError" in entry["error"]


def test_path_escape_raises(tmp_path):
    outside = tmp_path.parent / "outside_op_edges_test.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    try:
        with pytest.raises(PathEscapeError):
            op_edges_for_file(tmp_path, outside)
    finally:
        outside.unlink()


def test_op_handler_missing_params_raise_value_error(tmp_path):
    async def _run():
        with pytest.raises(ValueError, match="target_root"):
            await _cartography_op_edges({"files": ["a.py"]})
        with pytest.raises(ValueError, match="files"):
            await _cartography_op_edges({"target_root": str(tmp_path)})

    asyncio.run(_run())


def test_op_handler_happy_path_delegates(tmp_path):
    _write(
        tmp_path,
        "producer.py",
        "from coordinator_core.ipc import register_op\n\n"
        "@register_op('demo.op')\n"
        "async def _h(params, repo_root=None):\n    return {}\n",
    )
    async def _run():
        return await _cartography_op_edges(
            {"target_root": str(tmp_path), "files": ["producer.py"]}
        )

    result = asyncio.run(_run())
    assert result["op_names"] == ["demo.op"]
    assert result["static_only"] is True
