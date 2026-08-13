"""
coordinator_core.cartography.tests.test_edges

Unit tests for coordinator_core.cartography.edges — static import +
intra-module call graph.

Coverage:
  (a) plain "import x" edge recorded
  (b) "from x import y" edge recorded against the module, not the name
  (c) intra-module call edge: a top-level function calling another
      top-level function in the SAME file is recorded
  (d) a call to an imported (cross-file) name does NOT produce a call edge
  (e) a SyntaxError file is captured into the "error" field, not raised
  (f) build_edges aggregates multiple files under {"edges": [...]}
  (g) AC7 (hard requirement 1): a known register_op registration edge is
      ABSENT from the static graph — the hybrid dynamic-dispatch blind spot
      is proven, not merely asserted in prose.
  (h) AC7 (hard requirement 2): the in-band completeness marker
      ("excludes": ["register_op_dynamic_dispatch"], "static_only": true)
      is present on every build_edges payload, regardless of input.
  (i) path containment: an escaping file_path raises PathEscapeError
  (j) import-guard + registry — "cartography.edges" registered after import
  (k) op handler — missing target_root/files raises ValueError; happy path
      delegates to build_edges
  (l) op handler guards target_root via path_guard(target_root, ".") BEFORE
      build_edges runs any per-file work (Finding 2, 2026-07-12-codereview-
      slicecartography-substrate-b-wave)
  (m) a root-level __init__.py gets a sentinel module name (target_root's
      basename), not an empty string (Finding 3, 2026-07-12-codereview-
      slicecartography-substrate-b-wave)

Spec backlink: pln-claude-klabauter-cartography-substrate-a-26eb2e
§ chunk C4 (cartography.edges), AC7.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# Review: code-reviewer (P1, 2026-07-12-workflow-review-cartography.md) —
# this file never imported the op module, so register_op never fired and the
# @register_op-decorated handler body (param extraction, error behavior) was
# exercised by nothing.
# ---------------------------------------------------------------------------
import coordinator_core.ops.cartography_edges  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.cartography_edges import (
    _cartography_edges,
    _cartography_count_references,
)
from coordinator_core.cartography._guard import PathEscapeError
from coordinator_core.cartography.edges import build_edges, edges_for_file

_OP_NAME = "cartography.edges"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.cartography_edges @register_op did not fire"
)


def _write(root, rel_path, content):
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_plain_import_edge_recorded(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "mod.py", "import os\n")

    result = edges_for_file(root, "mod.py")

    kinds = [e for e in result["edges"] if e["kind"] == "import"]
    assert {"from": "mod", "to": "os", "kind": "import"} in kinds


def test_from_import_edge_recorded_against_module(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "mod.py", "from pathlib import Path\n")

    result = edges_for_file(root, "mod.py")

    assert {"from": "mod", "to": "pathlib", "kind": "import"} in result["edges"]


def test_intra_module_call_edge_recorded(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(
        root,
        "mod.py",
        "def helper():\n    return 1\n\n"
        "def caller():\n    return helper()\n",
    )

    result = edges_for_file(root, "mod.py")

    call_edges = [e for e in result["edges"] if e["kind"] == "call"]
    assert {"from": "mod.caller", "to": "mod.helper", "kind": "call"} in call_edges


def test_call_to_imported_name_is_not_a_call_edge(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(
        root,
        "mod.py",
        "from other import helper\n\n"
        "def caller():\n    return helper()\n",
    )

    result = edges_for_file(root, "mod.py")

    call_edges = [e for e in result["edges"] if e["kind"] == "call"]
    assert call_edges == []


def test_syntax_error_captured_not_raised(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "broken.py", "def f(:\n    pass\n")

    result = edges_for_file(root, "broken.py")

    assert result["edges"] == []
    assert "error" in result
    assert "SyntaxError" in result["error"]


def test_build_edges_aggregates_multiple_files(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "a.py", "import os\n")
    _write(root, "b.py", "import sys\n")

    result = build_edges(root, ["a.py", "b.py"])

    assert len(result["edges"]) == 2
    assert {e["path"] for e in result["edges"]} == {"a.py", "b.py"}


def test_root_level_init_py_gets_sentinel_module_name_not_empty(tmp_path):
    """Review: code-reviewer (nit, Finding 3, 2026-07-12-codereview-
    slicecartography-substrate-b-wave) — a root-level __init__.py must not
    produce an empty module name ("" for `from`); falls back to the
    containing directory's (target_root's) basename."""
    root = tmp_path / "my_pkg"
    root.mkdir()
    _write(root, "__init__.py", "import os\n")

    result = edges_for_file(root, "__init__.py")

    assert {"from": "my_pkg", "to": "os", "kind": "import"} in result["edges"]


def test_path_containment_escape_raises(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "secret.py"
    outside.write_text("import os\n", encoding="utf-8")

    with pytest.raises(PathEscapeError):
        edges_for_file(root, "../secret.py")


# ---------------------------------------------------------------------------
# AC7 — the hybrid dynamic-dispatch blind spot (hard requirement 1)
# ---------------------------------------------------------------------------


def test_register_op_dynamic_dispatch_edge_is_absent(tmp_path):
    """A register_op registration is a RUNTIME dispatch edge, never a static
    one — a static import+call walk over a module that calls register_op()
    at import time produces NO edge from the dispatcher to the registered
    handler, because that edge only exists via the string-keyed registry
    populated at runtime, not via any static `call` AST node naming the
    handler by its callee identity.

    This test proves the absence concretely: a synthetic op module mirroring
    ops/ping.py's shape (``@register_op("some.op")`` decorating a handler)
    is fed through edges_for_file, and we assert that no edge in the output
    connects anything to the STRING op name "some.op" or otherwise encodes
    the runtime dispatcher->handler relationship. The only edges the static
    walk can produce here are the import edge to coordinator_core.ipc and a
    (decorator) call edge to the two-argument form of register_op itself —
    never an edge keyed on the string "some.op" leading to the handler,
    because "some.op" is a string literal, not a named symbol a static call
    graph can connect to a callee.

    Re-read against Rule 1 (chunk C6, 2026-08-06): Rule 1 makes
    ``register_op`` itself a legitimate cross-file-resolved import in EVERY
    real op module — ``from coordinator_core.ipc import register_op``
    followed by the call resolves to ``coordinator_core.ipc.register_op``
    once that module is confirmed to define it. That resolved edge does NOT
    appear in THIS fixture, because this fixture's ``target_root`` contains
    only ``fake_op.py`` — no ``coordinator_core`` package on disk for Rule 1
    to find and confirm — so the import declines here (external/not-found),
    same as any module absent from the corpus. But the assertions below do
    not depend on that decline: a resolved Rule-1 edge targets
    ``"coordinator_core.ipc.register_op"``, never the string ``"some.op"``,
    so they hold regardless of whether the import resolves. See
    ``test_register_op_resolved_edge_is_not_the_dynamic_dispatch_blind_spot``
    below for the case where ``coordinator_core.ipc`` IS present and the
    Rule-1 edge is proven to legitimately appear alongside the still-absent
    blind-spot edge.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _write(
        root,
        "fake_op.py",
        "from coordinator_core.ipc import register_op\n\n"
        '@register_op("some.op")\n'
        "async def _handler(params, repo_root=None):\n"
        "    return {\"ok\": True}\n",
    )

    result = edges_for_file(root, "fake_op.py")

    edge_targets = {e["to"] for e in result["edges"]}
    edge_sources = {e["from"] for e in result["edges"]}

    # No edge is keyed on the dynamic-dispatch string op name.
    assert "some.op" not in edge_targets
    assert "some.op" not in edge_sources
    # No edge connects the dispatcher's registry lookup to the handler by
    # the op-name string — the only handler-shaped node present is the
    # handler's own function identity (fake_op._handler), never reached via
    # "some.op".
    assert not any(
        e["kind"] == "call" and e["to"] == "fake_op._handler" and "some.op" in e.get("from", "")
        for e in result["edges"]
    )


# ---------------------------------------------------------------------------
# AC7 — in-band completeness marker (hard requirement 2)
# ---------------------------------------------------------------------------


def test_completeness_marker_present_on_every_payload(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "a.py", "import os\n")

    result = build_edges(root, ["a.py"])

    assert result["static_only"] is True
    assert "register_op_dynamic_dispatch" in result["excludes"]


def test_completeness_marker_present_even_with_no_files(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    result = build_edges(root, [])

    assert result["edges"] == []
    assert result["static_only"] is True
    assert result["excludes"] == ["register_op_dynamic_dispatch"]


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------


def test_op_missing_target_root_raises_value_error():
    with pytest.raises(ValueError):
        _cartography_edges({"files": ["mod.py"]})


def test_op_missing_files_raises_value_error(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(ValueError):
        _cartography_edges({"target_root": str(root)})


def test_op_happy_path(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "mod.py", "import os\n")

    result = _cartography_edges({"target_root": str(root), "files": ["mod.py"]})

    assert result["static_only"] is True
    assert len(result["edges"]) == 1
    per_file = result["edges"][0]
    assert per_file["path"] == "mod.py"
    assert {"from": "mod", "to": "os", "kind": "import"} in per_file["edges"]


def test_op_guards_target_root_before_build_edges_is_called(tmp_path, monkeypatch):
    """Review: code-reviewer (P2, Finding 2, 2026-07-12-codereview-slicecartography-
    substrate-b-wave) — cartography.edges must validate target_root via
    path_guard(target_root, ".") at the handler boundary, mirroring
    cartography.tree/file_index, BEFORE build_edges runs any per-file work.
    Proven by making path_guard raise and asserting build_edges is never
    reached (a plain `target_root` string can't be made to fail path_guard's
    own containment check on its own — "." relative to itself never escapes
    — so the up-front-vs-per-file call-stack positioning is the property
    under test, not a new failure mode)."""
    import coordinator_core.ops.cartography_edges as op_mod

    root = tmp_path / "repo"
    root.mkdir()

    calls: list[str] = []

    def _raising_guard(target_root, path):
        calls.append("guard")
        raise PathEscapeError("simulated up-front rejection")

    def _should_not_be_called(target_root, files):
        calls.append("build_edges")
        raise AssertionError("build_edges must not run when path_guard rejects target_root")

    monkeypatch.setattr(op_mod, "path_guard", _raising_guard)
    monkeypatch.setattr(op_mod, "build_edges", _should_not_be_called)

    with pytest.raises(PathEscapeError):
        _cartography_edges({"target_root": str(root), "files": ["mod.py"]})

    assert calls == ["guard"]


# ---------------------------------------------------------------------------
# Rule 1 — cross-file direct-call resolution (chunk C6, 2026-08-06,
# respecified against docs/research/spike-verdicts/
# 2026-08-06-cartography-cross-file-call-resolution.md)
# ---------------------------------------------------------------------------


def test_rule1_direct_call_resolved_to_confirmed_definition(tmp_path):
    """The proven case: mod.py imports run from other.py (absolute
    ImportFrom), other.py DEFINES run at module level — a direct call
    resolves to a cross-file call edge naming the defining module."""
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "other.py", "def run():\n    return 1\n")
    _write(
        root,
        "mod.py",
        "from other import run\n\n"
        "def caller():\n    return run()\n",
    )

    result = edges_for_file(root, "mod.py")

    call_edges = [e for e in result["edges"] if e["kind"] == "call"]
    assert {"from": "mod.caller", "to": "other.run", "kind": "call"} in call_edges


def test_rule1_declines_when_target_module_does_not_define_symbol(tmp_path):
    """The __init__ re-export gap (0.5% of the measured corpus, spike
    verdict 2026-08-06): pkg/__init__.py re-exports `run` from pkg._impl
    rather than defining it. A caller importing `run` FROM THE PACKAGE
    binds to `pkg`, but `pkg` itself does not define `run` at module level
    — decline-by-default means no edge, not a guessed one."""
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "pkg/_impl.py", "def run():\n    return 1\n")
    _write(root, "pkg/__init__.py", "from pkg._impl import run\n")
    _write(
        root,
        "mod.py",
        "from pkg import run\n\n"
        "def caller():\n    return run()\n",
    )

    result = edges_for_file(root, "mod.py")

    call_edges = [e for e in result["edges"] if e["kind"] == "call"]
    assert call_edges == []


def test_rule1_declines_external_module(tmp_path):
    """A name imported from a module not found under target_root (stdlib/
    third-party, or simply outside this corpus) declines — no edge."""
    root = tmp_path / "repo"
    root.mkdir()
    _write(
        root,
        "mod.py",
        "from os.path import join\n\n"
        "def caller():\n    return join('a', 'b')\n",
    )

    result = edges_for_file(root, "mod.py")

    call_edges = [e for e in result["edges"] if e["kind"] == "call"]
    assert call_edges == []


def test_rule1_declines_unbound_name(tmp_path):
    """A builtin call has no import binding at all — declines, no edge."""
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "mod.py", "def caller():\n    return len([1, 2, 3])\n")

    result = edges_for_file(root, "mod.py")

    call_edges = [e for e in result["edges"] if e["kind"] == "call"]
    assert call_edges == []


def test_rule1_negative_local_shadow_is_not_import_bound(tmp_path):
    """Negative fixture (per dispatch brief): module A (other.py) defines
    run(); module B (mod.py) never imports run — it assigns a LOCAL name
    `run` and calls that. The binding table only tracks names bound via
    ImportFrom, so this must NOT produce a false edge to other.run."""
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "other.py", "def run():\n    return 1\n")
    _write(
        root,
        "mod.py",
        "def caller():\n    run = lambda: 2\n    return run()\n",
    )

    result = edges_for_file(root, "mod.py")

    call_edges = [e for e in result["edges"] if e["kind"] == "call"]
    assert call_edges == []


def test_register_op_resolved_edge_is_not_the_dynamic_dispatch_blind_spot(tmp_path):
    """Complements test_register_op_dynamic_dispatch_edge_is_absent: here
    coordinator_core/ipc.py IS present under target_root and DEFINES
    register_op, so Rule 1 legitimately resolves the static call
    `register_op(...)` inside fake_op.py's decorator to
    coordinator_core.ipc.register_op. This is a real, correctly-resolved
    edge — and a DIFFERENT edge from the dispatcher->handler blind spot:
    no edge is keyed on the "some.op" string, and none connects anything to
    fake_op._handler via that string. Both facts hold simultaneously: the
    static call to register_op resolves; the runtime string-keyed dispatch
    to _handler remains categorically unresolvable by any static walk."""
    root = tmp_path / "repo"
    root.mkdir()
    _write(
        root,
        "coordinator_core/ipc.py",
        "def register_op(name, handler=None):\n    return handler\n",
    )
    _write(
        root,
        "fake_op.py",
        "from coordinator_core.ipc import register_op\n\n"
        '@register_op("some.op")\n'
        "async def _handler(params, repo_root=None):\n"
        "    return {\"ok\": True}\n",
    )

    result = edges_for_file(root, "fake_op.py")

    call_edges = [e for e in result["edges"] if e["kind"] == "call"]
    assert {
        "from": "fake_op._handler",
        "to": "coordinator_core.ipc.register_op",
        "kind": "call",
    } in call_edges

    edge_targets = {e["to"] for e in result["edges"]}
    edge_sources = {e["from"] for e in result["edges"]}
    assert "some.op" not in edge_targets
    assert "some.op" not in edge_sources


# ---------------------------------------------------------------------------
# Rule 2 — cross-file attribute-call resolution through a confirmed module
# alias (chunk C14, 2026-08-06, split from C6 per
# docs/research/spike-verdicts/2026-08-06-cartography-cross-file-call-resolution.md
# § Disposition)
# ---------------------------------------------------------------------------


def test_rule2_attribute_call_resolved_through_module_alias(tmp_path):
    """The proven case: mod.py imports other (plain ``import other``),
    other.py DEFINES helper at module level — an attribute call
    ``other.helper()`` resolves to a cross-file call edge naming the
    defining module."""
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "other.py", "def helper():\n    return 1\n")
    _write(
        root,
        "mod.py",
        "import other\n\n"
        "def caller():\n    return other.helper()\n",
    )

    result = edges_for_file(root, "mod.py")

    call_edges = [e for e in result["edges"] if e["kind"] == "call"]
    assert {"from": "mod.caller", "to": "other.helper", "kind": "call"} in call_edges


def test_rule2_declines_when_module_alias_does_not_define_attribute(tmp_path):
    """Negative fixture (per dispatch brief): module A (mod.py) imports
    module B (other.py); B does NOT define `helper`; A calls
    `B.helper()`. Decline-by-default — no edge, not a guessed one."""
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "other.py", "def unrelated():\n    return 1\n")
    _write(
        root,
        "mod.py",
        "import other\n\n"
        "def caller():\n    return other.helper()\n",
    )

    result = edges_for_file(root, "mod.py")

    call_edges = [e for e in result["edges"] if e["kind"] == "call"]
    assert call_edges == []


def test_rule2_declines_external_module_alias(tmp_path):
    """A receiver bound to an external (stdlib/third-party) module alias
    declines — no edge, even though the attribute name exists on the real
    stdlib module, because that module is not found under target_root."""
    root = tmp_path / "repo"
    root.mkdir()
    _write(
        root,
        "mod.py",
        "import os\n\n"
        "def caller():\n    return os.getcwd()\n",
    )

    result = edges_for_file(root, "mod.py")

    call_edges = [e for e in result["edges"] if e["kind"] == "call"]
    assert call_edges == []


def test_rule2_declines_self_attribute_call(tmp_path):
    """``self.foo()`` is never resolved by Rule 2 — ``self`` is a method
    parameter, never bound via an ``ast.Import``, so it never appears in
    module_bindings at all; declines by construction."""
    root = tmp_path / "repo"
    root.mkdir()
    _write(
        root,
        "mod.py",
        "class Thing:\n"
        "    def helper(self):\n"
        "        return 1\n\n"
        "    def caller(self):\n"
        "        return self.helper()\n",
    )

    result = edges_for_file(root, "mod.py")

    call_edges = [e for e in result["edges"] if e["kind"] == "call"]
    # `helper` is a CLASS METHOD, not a top-level def (_collect_top_level_defs
    # excludes methods), so same-file resolution does not match either; `self`
    # is a method parameter, never bound via an ast.Import, so it never
    # appears in module_bindings — Rule 2 declines by construction.
    assert call_edges == []


def test_rule2_declines_non_determinable_receiver(tmp_path):
    """A receiver that is not a plain ``ast.Name`` at all (here, the result
    of a call expression) is never statically determinable — declines, no
    edge, per the 72.1% measured decline rate."""
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "other.py", "def helper():\n    return 1\n")
    _write(
        root,
        "mod.py",
        "import other\n\n"
        "def get_module():\n    return other\n\n"
        "def caller():\n    return get_module().helper()\n",
    )

    result = edges_for_file(root, "mod.py")

    call_edges = [e for e in result["edges"] if e["kind"] == "call"]
    assert {"from": "mod.caller", "to": "other.helper", "kind": "call"} not in call_edges


def test_rule2_negative_module_alias_target_does_not_define_helper(tmp_path):
    """Negative fixture (per dispatch brief, explicit): module A imports
    module B; B does not define `helper`; A calls `B.helper()`. Assert NO
    edge is produced — not a guessed one, even though `helper` IS the
    attribute name being called."""
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "b.py", "def other_thing():\n    return 1\n")
    _write(
        root,
        "a.py",
        "import b\n\n"
        "def caller():\n    return b.helper()\n",
    )

    result = edges_for_file(root, "a.py")

    call_edges = [e for e in result["edges"] if e["kind"] == "call"]
    assert call_edges == []


# ---------------------------------------------------------------------------
# C15 — additive relative-import normalization (`to_normalized`)
#
# Spec backlink: pln-claude-klabauter-ize-the-survey-census-c-2a0dfd § C15,
# docs/research/spike-verdicts/2026-08-06-cartography-cross-file-call-resolution.md
# § "The count_references hazard". `to` MUST stay byte-identical on every
# edge — normalization lands ONLY in the new `to_normalized` field.
# ---------------------------------------------------------------------------


def test_c15_relative_import_to_is_byte_identical_golden(tmp_path):
    """Golden-fixture byte-identity assertion: every relative-import shape
    this module already emits (`from . import x`, `from .sibling import y`,
    `from .. import x`, `from ..pkg.sub import z`) produces the EXACT same
    `to` string as before this row landed — leading dots (or bare name for
    the module-less form) intact, never normalized in place."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pkg").mkdir()
    (root / "pkg" / "sub").mkdir()
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/sub/__init__.py", "")
    _write(root, "pkg/sibling.py", "")
    _write(root, "pkg/other.py", "")
    _write(
        root,
        "pkg/sub/mod.py",
        "from . import x\n"
        "from .sibling import y\n"
        "from .. import x\n"
        "from ..other import z\n",
    )

    result = edges_for_file(root, "pkg/sub/mod.py")
    import_edges = [e for e in result["edges"] if e["kind"] == "import"]

    golden_to = {
        # `from . import x` — node.module is None, existing behavior records
        # the bare imported name with NO dots at all.
        "x",
        # `from .sibling import y` — level=1, dots preserved on the target.
        ".sibling",
        # `from .. import x` (second occurrence, module_name None branch,
        # bare imported name again)
        "x",
        # `from ..other import z` — level=2, dots preserved.
        "..other",
    }
    assert {e["to"] for e in import_edges} == golden_to


def test_c15_to_normalized_field_correctness(tmp_path):
    """The new `to_normalized` field carries the PEP-328-resolved absolute
    dotted module name for a known relative-import fixture — anchored at the
    importing file's own module name, per PEP 328's level semantics."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pkg").mkdir()
    (root / "pkg" / "sub").mkdir()
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/sub/__init__.py", "")
    _write(root, "pkg/sibling.py", "")
    _write(root, "pkg/other.py", "")
    _write(
        root,
        "pkg/sub/mod.py",
        "from .sibling import y\n"
        "from ..other import z\n",
    )

    result = edges_for_file(root, "pkg/sub/mod.py")
    import_edges = {e["to"]: e for e in result["edges"] if e["kind"] == "import"}

    # module_name for pkg/sub/mod.py is "pkg.sub.mod"; level=1 resolves
    # relative to "pkg.sub" (its containing package); level=2 resolves
    # relative to "pkg" (one ancestor further up).
    assert import_edges[".sibling"]["to_normalized"] == "pkg.sub.sibling"
    assert import_edges["..other"]["to_normalized"] == "pkg.other"


def test_c15_normalized_dotless_import_has_no_normalized_field(tmp_path):
    """A plain (non-relative) `import x` or `from x import y` edge — the
    overwhelming majority of edges — carries no `to_normalized` key at all;
    the field is added ONLY for `level > 0` relative imports."""
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "mod.py", "import os\nfrom sys import argv\n")

    result = edges_for_file(root, "mod.py")
    import_edges = [e for e in result["edges"] if e["kind"] == "import"]

    assert import_edges
    assert all("to_normalized" not in e for e in import_edges)


def test_c15_count_references_unchanged_on_collision_fixture(tmp_path):
    """Regression fixture reproducing one of the spike verdict's 31
    collision cases: a relative-import edge whose NORMALIZED target
    collides with an existing absolute edge's `to`. Because `to` stays
    byte-identical, `count_references` (exact string equality on `to`) must
    NOT count the relative-import edge toward the absolute module's
    reference count — proving the additive field changes nothing about the
    existing contract."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pkg").mkdir()
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/target.py", "")
    _write(
        root,
        "pkg/relative_importer.py",
        # Relative import whose normalized form is "pkg.target" — the same
        # dotted name the absolute importer below references directly.
        "from .target import thing\n",
    )
    _write(
        root,
        "absolute_importer.py",
        "import pkg.target\n",
    )

    result = _cartography_count_references(
        {
            "target_root": str(root),
            "module_name": "pkg.target",
            "files": [
                "pkg/__init__.py",
                "pkg/target.py",
                "pkg/relative_importer.py",
                "absolute_importer.py",
            ],
        }
    )

    # Only the absolute importer's edge matches "pkg.target" by exact
    # string equality on `to` — the relative importer's edge has
    # to="".join([".", "target"]) == ".target" (byte-identical, unchanged),
    # so it does NOT contribute to the count even though its
    # to_normalized == "pkg.target" collides with the absolute target.
    assert result["reference_count"] == 1


# ---------------------------------------------------------------------------
# C7 — boundary-marker join via a module-name -> path table, additive,
# gated on the optional `path_system_map` param.
# ---------------------------------------------------------------------------


def test_c7_boundary_omitted_when_path_system_map_not_supplied(tmp_path):
    """Default (no `path_system_map`) is byte-identical to pre-C7 output —
    no import edge gains a "boundary" field at all."""
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "mod.py", "import os\n")

    result = edges_for_file(root, "mod.py")
    import_edges = [e for e in result["edges"] if e["kind"] == "import"]

    assert import_edges
    assert all("boundary" not in e for e in import_edges)


def test_c7_internal_when_same_system(tmp_path):
    """Target inverts to a path in the SAME system as the caller ->
    "internal"."""
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "sysa/__init__.py", "")
    _write(root, "sysa/caller.py", "import sysa.callee\n")
    _write(root, "sysa/callee.py", "")

    result = build_edges(
        root,
        ["sysa/__init__.py", "sysa/caller.py", "sysa/callee.py"],
        path_system_map={
            "sysa/__init__.py": "sysa",
            "sysa/caller.py": "sysa",
            "sysa/callee.py": "sysa",
        },
    )
    caller_entry = next(e for e in result["edges"] if e["path"] == "sysa/caller.py")
    import_edge = next(
        e for e in caller_entry["edges"] if e["kind"] == "import" and e["to"] == "sysa.callee"
    )
    assert import_edge["boundary"] == "internal"


def test_c7_cross_system_when_caller_and_target_in_different_systems(tmp_path):
    """Target inverts to a first-party path, and the CALLER inverts to a
    first-party path in a DIFFERENT system -> "cross-system" (coordinator-claude's
    `[BOUNDARY -> system-name]`: this call reaches INTO a different
    system)."""
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "sysa/__init__.py", "")
    _write(root, "sysa/caller.py", "import sysb.internal\n")
    _write(root, "sysb/__init__.py", "")
    _write(root, "sysb/internal.py", "")

    result = build_edges(
        root,
        ["sysa/caller.py", "sysb/__init__.py", "sysb/internal.py"],
        path_system_map={
            "sysa/caller.py": "sysa",
            "sysb/__init__.py": "sysb",
            "sysb/internal.py": "sysb",
        },
    )
    caller_entry = next(e for e in result["edges"] if e["path"] == "sysa/caller.py")
    import_edge = next(
        e
        for e in caller_entry["edges"]
        if e["kind"] == "import" and e["to"] == "sysb.internal"
    )
    assert import_edge["boundary"] == "cross-system"


def test_c7_entry_when_caller_outside_mapped_system_set(tmp_path):
    """Target inverts to a first-party path, but the CALLER does not
    resolve into the mapped system set at all -> "entry" (coordinator-claude's `[ENTRY]`:
    this target is reached from OUTSIDE its own system entirely). The
    caller's own file is deliberately OMITTED from `path_system_map` here —
    that omission is what makes it "outside the mapped system set", not
    anything about the target path's shape."""
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "sysa/__init__.py", "")
    _write(root, "sysa/caller.py", "import sysb.internal\n")
    _write(root, "sysb/__init__.py", "")
    _write(root, "sysb/internal.py", "")

    result = build_edges(
        root,
        ["sysa/caller.py", "sysb/__init__.py", "sysb/internal.py"],
        path_system_map={
            # sysa/caller.py intentionally absent — outside the mapped set.
            "sysb/__init__.py": "sysb",
            "sysb/internal.py": "sysb",
        },
    )
    caller_entry = next(e for e in result["edges"] if e["path"] == "sysa/caller.py")
    import_edge = next(
        e
        for e in caller_entry["edges"]
        if e["kind"] == "import" and e["to"] == "sysb.internal"
    )
    assert import_edge["boundary"] == "entry"


def test_c7_external_is_a_real_string_never_null(tmp_path):
    """Target does NOT invert to any path under target_root at all
    (stdlib/third-party) -> the literal string "external", never None."""
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "sysa/__init__.py", "")
    _write(root, "sysa/caller.py", "import os\n")

    result = build_edges(
        root,
        ["sysa/__init__.py", "sysa/caller.py"],
        path_system_map={"sysa/__init__.py": "sysa", "sysa/caller.py": "sysa"},
    )
    caller_entry = next(e for e in result["edges"] if e["path"] == "sysa/caller.py")
    import_edge = next(
        e for e in caller_entry["edges"] if e["kind"] == "import" and e["to"] == "os"
    )
    assert import_edge["boundary"] == "external"
    assert import_edge["boundary"] is not None
    assert isinstance(import_edge["boundary"], str)


def test_c7_op_wires_path_system_map_through_to_boundary_labelling(tmp_path):
    """The op handler (not just build_edges directly) passes `path_system_map`
    through, and count_references' own call path (which never supplies the
    param) stays byte-identical — see the sibling test below."""
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "sysa/__init__.py", "")
    _write(root, "sysa/caller.py", "import os\n")

    result = _cartography_edges(
        {
            "target_root": str(root),
            "files": ["sysa/__init__.py", "sysa/caller.py"],
            "path_system_map": {
                "sysa/__init__.py": "sysa",
                "sysa/caller.py": "sysa",
            },
        }
    )
    caller_entry = next(e for e in result["edges"] if e["path"] == "sysa/caller.py")
    import_edge = next(
        e for e in caller_entry["edges"] if e["kind"] == "import" and e["to"] == "os"
    )
    assert import_edge["boundary"] == "external"


def test_c7_count_references_unaffected_by_boundary_param(tmp_path):
    """`cartography.count_references` never supplies `path_system_map` on its
    own internal `build_edges` call, so its reply shape and counts are
    completely untouched by C7 — same fixture/assertion shape as the C15
    count_references regression test above, proving the two rows compose."""
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "sysa/__init__.py", "")
    _write(root, "sysa/caller.py", "import sysb\n")
    _write(root, "sysb/__init__.py", "")

    result = _cartography_count_references(
        {
            "target_root": str(root),
            "module_name": "sysb",
            "files": ["sysa/__init__.py", "sysa/caller.py", "sysb/__init__.py"],
        }
    )
    assert result == {
        "reference_count": 1,
        "referencing_files": ["sysa/caller.py"],
    }


def test_c7_root_mismatch_is_surfaced_not_silently_near_zero(tmp_path):
    """AC16 — reproduces the spike verdict's exact near-miss shape: module
    names derived under a `target_root` one level too deep for how the
    corpus refers to itself (files import each other as
    "coordinator_core.<mod>" but target_root is rooted AT the
    "coordinator_core" directory itself, stripping that leading component
    from every derived module name). This must raise loudly, not silently
    report near-zero resolution — the spike's own first probe run reported
    0.1%/1.6% and nearly produced a wrong "not viable" verdict."""
    repo = tmp_path / "repo"
    pkg = repo / "coordinator_core"
    pkg.mkdir(parents=True)
    _write(repo, "coordinator_core/__init__.py", "")
    _write(
        repo,
        "coordinator_core/a.py",
        "from coordinator_core.b import helper\n\n\ndef caller():\n    helper()\n",
    )
    _write(repo, "coordinator_core/b.py", "def helper():\n    pass\n")

    with pytest.raises(ValueError, match="import root"):
        build_edges(
            pkg,  # WRONG: one level too deep vs. the corpus's own "coordinator_core.*" imports
            ["a.py", "b.py"],
            path_system_map={"a.py": "coordinator_core", "b.py": "coordinator_core"},
        )


def test_c7_root_mismatch_absent_when_root_matches_import_root(tmp_path):
    """Sibling to the mismatch test above: the SAME fixture, rooted
    correctly (at `repo`, matching the corpus's own "coordinator_core.*"
    self-imports), raises nothing and resolves normally — proving the
    detection is precise (root-disagreement-specific), not a blanket
    low-resolution heuristic that would also fire here."""
    repo = tmp_path / "repo"
    (repo / "coordinator_core").mkdir(parents=True)
    _write(repo, "coordinator_core/__init__.py", "")
    _write(
        repo,
        "coordinator_core/a.py",
        "from coordinator_core.b import helper\n\n\ndef caller():\n    helper()\n",
    )
    _write(repo, "coordinator_core/b.py", "def helper():\n    pass\n")

    result = build_edges(
        repo,
        ["coordinator_core/__init__.py", "coordinator_core/a.py", "coordinator_core/b.py"],
        path_system_map={
            "coordinator_core/__init__.py": "coordinator_core",
            "coordinator_core/a.py": "coordinator_core",
            "coordinator_core/b.py": "coordinator_core",
        },
    )
    a_entry = next(e for e in result["edges"] if e["path"] == "coordinator_core/a.py")
    import_edge = next(
        e
        for e in a_entry["edges"]
        if e["kind"] == "import" and e["to"] == "coordinator_core.b"
    )
    assert import_edge["boundary"] == "internal"


# ---------------------------------------------------------------------------
# Review follow-up (coordinator:code-reviewer, 2026-08-06) — `to_normalized`
# for the module-less relative-import shape (`from . import x`) is EITHER a
# submodule of the anchor package OR a name re-exported from the anchor
# package's own `__init__.py`; disambiguated by checking both candidates
# against target_root, declining (no `to_normalized` key) when neither is
# confirmed. `to` itself is unaffected — these tests assert `to_normalized`
# only.
# ---------------------------------------------------------------------------


def test_module_less_relative_import_normalizes_to_submodule_when_real_file(tmp_path):
    """`from . import x` where `x` really is a submodule (`pkg/x.py` exists)
    normalizes to `pkg.x` — the pre-existing (correct) behavior for this
    shape, preserved by the disambiguation fix."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pkg").mkdir()
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/x.py", "")
    _write(root, "pkg/mod.py", "from . import x\n")

    result = edges_for_file(root, "pkg/mod.py")
    import_edges = [e for e in result["edges"] if e["kind"] == "import"]

    assert import_edges[0]["to"] == "x"
    assert import_edges[0]["to_normalized"] == "pkg.x"


def test_module_less_relative_import_normalizes_to_anchor_when_reexported_name(tmp_path):
    """`from . import x` where `x` is NOT a submodule but IS a name defined
    at module level in the anchor package's `__init__.py` (the re-export
    shape the module docstring calls out) normalizes to the ANCHOR package
    itself (`pkg`), not the wrong `pkg.x` the pre-fix code produced."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pkg").mkdir()
    _write(root, "pkg/__init__.py", "def x():\n    pass\n")
    _write(root, "pkg/mod.py", "from . import x\n")

    result = edges_for_file(root, "pkg/mod.py")
    import_edges = [e for e in result["edges"] if e["kind"] == "import"]

    assert import_edges[0]["to"] == "x"
    assert import_edges[0]["to_normalized"] == "pkg"


def test_module_less_relative_import_declines_when_neither_candidate_confirmed(tmp_path):
    """`from . import x` where `x` is neither a real submodule NOR a name
    defined in the anchor package's `__init__.py` (e.g. a dynamically
    injected attribute, or simply absent) declines `to_normalized` entirely
    — decline-by-default, never a guessed value. `to` stays present and
    byte-identical."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pkg").mkdir()
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/mod.py", "from . import x\n")

    result = edges_for_file(root, "pkg/mod.py")
    import_edges = [e for e in result["edges"] if e["kind"] == "import"]

    assert import_edges[0]["to"] == "x"
    assert "to_normalized" not in import_edges[0]


def test_module_less_relative_import_does_not_move_to_field(tmp_path):
    """Both the submodule and re-export disambiguation paths leave `to`
    untouched — `cartography.count_references` keeps filtering on `to`
    exactly as before this fix (module docstring RELATIVE-IMPORT
    NORMALIZATION invariant)."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pkg").mkdir()
    _write(root, "pkg/__init__.py", "def reexported():\n    pass\n")
    _write(root, "pkg/submod.py", "")
    _write(root, "pkg/mod.py", "from . import reexported, submod\n")

    result = edges_for_file(root, "pkg/mod.py")
    import_edges = {e["to"]: e for e in result["edges"] if e["kind"] == "import"}

    assert set(import_edges) == {"reexported", "submod"}
    assert import_edges["reexported"]["to_normalized"] == "pkg"
    assert import_edges["submod"]["to_normalized"] == "pkg.submod"


def test_module_less_relative_import_normalization_declined_without_target_root(tmp_path):
    """`_import_edges` called directly without `target_root` (a bare unit
    test / non-`edges_for_file` caller) declines `to_normalized` for the
    module-less relative shape rather than guessing — proving the decline
    path doesn't depend on `edges_for_file` plumbing specifically."""
    from coordinator_core.cartography.edges import _import_edges

    root = tmp_path / "repo"
    root.mkdir()
    (root / "pkg").mkdir()
    _write(root, "pkg/__init__.py", "def x():\n    pass\n")
    _write(root, "pkg/mod.py", "from . import x\n")

    tree = ast.parse((root / "pkg" / "mod.py").read_text(encoding="utf-8"))
    edges = _import_edges(tree, "pkg.mod")

    assert edges[0]["to"] == "x"
    assert "to_normalized" not in edges[0]


def test_definition_cache_shared_across_files_in_build_edges(tmp_path, monkeypatch):
    """Perf follow-up (P2): `build_edges` shares ONE `definition_cache`
    across every file in `files`, so a hot cross-file target imported+called
    from multiple calling files has its DEFINITION-CHECK parse
    (`_defines_symbol_at_module`) done once per `build_edges` invocation,
    not once per calling file. Proven directly against the shared cache
    object build_edges constructs, via `edges_for_file`'s optional
    `definition_cache` param."""
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "target.py", "def helper():\n    pass\n")
    _write(
        root,
        "caller_one.py",
        "from target import helper\n\n\ndef f():\n    helper()\n",
    )
    _write(
        root,
        "caller_two.py",
        "from target import helper\n\n\ndef g():\n    helper()\n",
    )

    parse_calls: list[str] = []
    original_parse = ast.parse

    def counting_parse(source, filename="<unknown>", *args, **kwargs):
        parse_calls.append(str(filename))
        return original_parse(source, filename, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", counting_parse)

    result = build_edges(root, ["target.py", "caller_one.py", "caller_two.py"])

    target_parses = [c for c in parse_calls if c.endswith("target.py")]
    # Once for target.py's OWN edges_for_file pass, plus at most once more
    # for the shared definition_cache's confirmation parse (triggered by the
    # first caller resolving `helper`) — never twice for the cache lookup,
    # which the pre-fix per-call-scoped cache would have done (one re-parse
    # per calling file).
    assert len(target_parses) <= 2
    assert result["edges"]
