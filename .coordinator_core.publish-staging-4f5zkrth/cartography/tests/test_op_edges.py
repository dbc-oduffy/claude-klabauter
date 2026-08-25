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
  (k) module-scope Name -> string Constant resolves ONE hop for
      register_op/get_op_handler/dispatch_message alike
  (l) a `for` target iterating a module-scope constant tuple/list/set of
      string literals yields one edge per member
  (m) negative-spec fixtures — call-bound name, f-string, concatenation,
      imported name, function parameter, function-local assignment — each
      yield zero edges
  (n) oracle — six live dispatch seams resolved by name, not by count,
      straight off this repo's own tree (post_commit_tail.py, tail_ops.py,
      guard_roster_ops.py)

Spec backlink: cross-repo memo, 2026-08-06 architecture survey; AC13,
docs/plans/2026-08-22-the-composition-gate-counts-processes-across-the-op-graph.md.
"""

from __future__ import annotations

from pathlib import Path

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
    with pytest.raises(ValueError, match="target_root"):
        _cartography_op_edges({"files": ["a.py"]})
    with pytest.raises(ValueError, match="files"):
        _cartography_op_edges({"target_root": str(tmp_path)})


def test_op_handler_happy_path_delegates(tmp_path):
    _write(
        tmp_path,
        "producer.py",
        "from coordinator_core.ipc import register_op\n\n"
        "@register_op('demo.op')\n"
        "async def _h(params, repo_root=None):\n    return {}\n",
    )
    result = _cartography_op_edges(
        {"target_root": str(tmp_path), "files": ["producer.py"]}
    )
    assert result["op_names"] == ["demo.op"]
    assert result["static_only"] is True


# ---------------------------------------------------------------------------
# Module-scope Name resolution (ONE hop) + for-target-over-collection
# ---------------------------------------------------------------------------


def test_module_scope_name_resolved_for_register_op_get_op_handler_dispatch(tmp_path):
    _write(
        tmp_path,
        "named.py",
        "from coordinator_core.ipc import register_op, get_op_handler\n"
        "\n"
        "OP_A = 'demo.name_a'\n"
        "OP_B: str = 'demo.name_b'\n"
        "\n"
        "@register_op(OP_A)\n"
        "async def _h(params, repo_root=None):\n"
        "    return {}\n"
        "\n"
        "def f():\n"
        "    get_op_handler(OP_B)\n"
        "\n"
        "async def g(msg):\n"
        "    await dispatch_message({'method': OP_A, 'params': {}})\n",
    )
    entry = op_edges_for_file(tmp_path, "named.py")
    assert entry["registrations"] == ["demo.name_a"]
    assert entry["lookups"] == ["demo.name_b"]
    assert entry["dispatches"] == ["demo.name_a"]


def test_for_target_over_module_collection_yields_one_edge_per_member(tmp_path):
    _write(
        tmp_path,
        "roster.py",
        "from coordinator_core.ipc import get_op_handler\n"
        "from typing import Tuple\n"
        "\n"
        "_NAMES: Tuple[str, ...] = ('demo.roster_a', 'demo.roster_b', 'demo.roster_c')\n"
        "\n"
        "def resolve_all():\n"
        "    for name in _NAMES:\n"
        "        get_op_handler(name)\n",
    )
    entry = op_edges_for_file(tmp_path, "roster.py")
    assert sorted(entry["lookups"]) == ["demo.roster_a", "demo.roster_b", "demo.roster_c"]


# ---------------------------------------------------------------------------
# Negative-spec fixtures — one per defeating shape, each zero edges
# ---------------------------------------------------------------------------


def test_negative_call_bound_name_yields_no_edge(tmp_path):
    _write(
        tmp_path,
        "neg_call_bound.py",
        "from coordinator_core.ipc import get_op_handler\n"
        "\n"
        "def make_name():\n"
        "    return 'demo.made'\n"
        "\n"
        "def f():\n"
        "    op_name = make_name()\n"
        "    get_op_handler(op_name)\n",
    )
    entry = op_edges_for_file(tmp_path, "neg_call_bound.py")
    assert entry["lookups"] == []


def test_negative_fstring_yields_no_edge(tmp_path):
    _write(
        tmp_path,
        "neg_fstring.py",
        "from coordinator_core.ipc import get_op_handler\n"
        "\n"
        "def f(suffix):\n"
        "    get_op_handler(f'demo.{suffix}')\n",
    )
    entry = op_edges_for_file(tmp_path, "neg_fstring.py")
    assert entry["lookups"] == []


def test_negative_concatenation_yields_no_edge(tmp_path):
    _write(
        tmp_path,
        "neg_concat.py",
        "from coordinator_core.ipc import get_op_handler\n"
        "\n"
        "def f():\n"
        "    get_op_handler('demo.' + 'concat')\n",
    )
    entry = op_edges_for_file(tmp_path, "neg_concat.py")
    assert entry["lookups"] == []


def test_negative_imported_name_yields_no_edge(tmp_path):
    _write(
        tmp_path,
        "neg_import.py",
        "from coordinator_core.ipc import get_op_handler\n"
        "from somewhere_else import OP_IMPORTED\n"
        "\n"
        "def f():\n"
        "    get_op_handler(OP_IMPORTED)\n",
    )
    entry = op_edges_for_file(tmp_path, "neg_import.py")
    assert entry["lookups"] == []


def test_negative_function_parameter_yields_no_edge(tmp_path):
    _write(
        tmp_path,
        "neg_param.py",
        "from coordinator_core.ipc import get_op_handler\n"
        "\n"
        "def f(op_name):\n"
        "    get_op_handler(op_name)\n",
    )
    entry = op_edges_for_file(tmp_path, "neg_param.py")
    assert entry["lookups"] == []


def test_negative_function_local_assignment_yields_no_edge(tmp_path):
    _write(
        tmp_path,
        "neg_local.py",
        "from coordinator_core.ipc import get_op_handler\n"
        "\n"
        "def f():\n"
        "    op_name = 'demo.local'\n"
        "    get_op_handler(op_name)\n",
    )
    entry = op_edges_for_file(tmp_path, "neg_local.py")
    assert entry["lookups"] == []


# ---------------------------------------------------------------------------
# Oracle — six live dispatch seams, asserted by op name, never by count
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_oracle_post_commit_tail_resolves_named_ops():
    entry = op_edges_for_file(_REPO_ROOT, "coordinator_core/ops/ceremony/post_commit_tail.py")
    assert "deliverable.cascade_terminal" in entry["lookups"]
    assert "handoff.transition" in entry["lookups"]
    assert "handoff.close_origin_stub" in entry["lookups"]


def test_oracle_tail_ops_resolves_review_trail_write():
    entry = op_edges_for_file(_REPO_ROOT, "coordinator_core/ops/ceremony/tail_ops.py")
    assert "review_trail.write" in entry["lookups"]


def test_oracle_guard_roster_ops_resolves_ported_advisory_hook_names():
    entry = op_edges_for_file(_REPO_ROOT, "coordinator_core/ops/session/guard_roster_ops.py")
    expected = {
        "hooks.nudge_foreground_agent_dispatch",
        "hooks.suggest_sonnet_research",
        "hooks.nudge_em_code_dispatch",
        "hooks.nudge_unauthorized_handoff",
        "hooks.postuse_advisory_dispatch",
        "hooks.nudge_named_agent_report_delivery",
    }
    assert expected.issubset(set(entry["lookups"]))
