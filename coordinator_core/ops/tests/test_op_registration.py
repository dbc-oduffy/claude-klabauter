"""
coordinator_core.ops.tests.test_op_registration

Wire-registration tests for the 7 ops landed by the cartography (Strand A),
distill-ceremony (memo.triage), and workflow-skeleton-stamper (workflow.validate)
build waves: cartography.tree, cartography.file_index, cartography.churn,
cartography.symbols, cartography.edges, memo.triage, workflow.validate.

This is the ONE registration-pass test file — it asserts the 4-file shared seam
(ops/__init__.py import, authz/classification.py COMPUTE_ONLY entry, ipc.py
_OP_KEY_SCOPE entry, benchmarks/budget-manifest.json entry) is wired correctly
for every op above, not the op's own business logic (each op's own test module
already covers that: cartography/tests/test_*.py, ops/tests/test_memo_triage.py,
ops/tests/test_workflow_validate.py).

Coverage:
  (a) every op key RESOLVES through coordinator_core.ipc's real dispatch path
      (`_lazy_import_and_lookup`: OP_MODULE_MAP targeted import, then the
      `_eager_import_all` safe fallback) — i.e. coordinator-invoke can reach
      it. This used to read `op_key in ipc._REGISTRY` on the premise that
      importing coordinator_core.ops registers every op; that premise was
      retired 2026-08-22 when the ops package went lazy (its docstring: the
      bare package NEVER populates the registry), leaving the raw membership
      read a test of pytest collection order rather than of wire registration.
      See test_op_is_registered's own docstring.
  (b) authz.classification.classify() returns COMPUTE_ONLY for every op key
      except the deliberately-MUTATING cartography.symbols (DR-228 § D6),
      which carries its own positive pin instead.
  (c) ipc.OP_KEY_SCOPE carries an entry for all 7 op keys — the exact wire-
      registration gate lesson 2026-07-06-compute-only-op-registration-needs-
      an-op guards (an op absent from _OP_KEY_SCOPE silently degrades to
      central scope). The 5 cartography ops + workflow.validate are scope
      "none" (explicit target_root/script_path param, no repo-specific state);
      memo.triage is scope "common_dir" (handler resolves
      main_worktree_root(repo_root) to read main-worktree-rooted
      cross-repo/archive/ + docs/decisions/ + CLAUDE.md — see
      ops/memo_triage.py and its own dispatch_message smoke in
      test_memo_triage.py, which pins this op to "common_dir" directly).
  (d) benchmarks.budget.resolve_budget() resolves a budget for all 7 op keys
      at the manifest-sourced COMPUTE_ONLY default (target_ms 70, +0.2
      relative tolerance) via explicit budget-manifest.json overrides entries.
  (e) command-type dispatch_message smoke for memo.triage with _origin_worktree
      set — validates the wire registration end-to-end (real _REGISTRY +
      real _OP_KEY_SCOPE, no temp-patching), not just the handler in isolation.
  (f) command-type dispatch_message smoke for cartography.tree (Finding 6,
      2026-07-12-codereview-slicecartography-substrate-b-wave) — proves the
      scope-"none" repo_root resolution path end-to-end for the cartography
      op family, the same way (e) proves it for memo.triage's "common_dir"
      scope.
  (g) command-type dispatch_message smoke for workflow.validate — proves the
      scope-"none" wire path end-to-end for this op the same way (f) does
      for cartography.tree.

Spec backlink: pln-claude-klabauter-cartography-substrate-a-26eb2e
                docs/plans/2026-07-12-distill-ceremony-mechanical-substrate-joint-design.md § C5
                docs/plans/2026-07-12-workflow-skeleton-stamper-claude-klabauter-engine.md § C2
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import coordinator_core.ops  # noqa: F401 — triggers every op module's register_op(...) side-effect
from coordinator_core.win_portability import no_console_passthrough_kwargs
import coordinator_core.ipc as ipc
from coordinator_core.authz.classification import OpClass, classify
from coordinator_core.benchmarks.budget import resolve_budget

# are DELIBERATELY excluded here: their absence from authz/classification.py
# _KNOWN_UNCLASSIFIED_OPS_DEBT) — folding them into _ALL_OPS here would make
_CARTOGRAPHY_OPS = (
    "cartography.tree",
    "cartography.file_index",
    "cartography.symbols",
    "cartography.edges",
)
_CARTOGRAPHY_OPS__SUBJECT_CLASS = "op-name"
_ALL_OPS = _CARTOGRAPHY_OPS + (
    "memo.triage",
    "workflow.validate",
    "workflow.scaffold",
    "deferral.detect_orphan_memo",
    "deferral.detect_partial_strangle",
    # surfaces. Folded into _ALL_OPS so (a) registered, (b) COMPUTE_ONLY, and
    "freshness.commit_delta",
)

# Derived from the authoritative wire-registration source (ipc.OP_KEY_SCOPE,
# itself sourced from op_scopes._OP_KEY_SCOPE) rather than a hardcoded tuple —
# a new cartography.* op lands in OP_KEY_SCOPE the same commit it's wired, so
# _CARTOGRAPHY_OPS literal above did (defect: cartography.stack /
_CARTOGRAPHY_OPS_REGISTERED = tuple(
    sorted(op for op in ipc.OP_KEY_SCOPE if op.startswith("cartography."))
)

# cartography.* op wired into OP_KEY_SCOPE that _ALL_OPS doesn't already
_BUDGET_MANIFEST_OPS = _ALL_OPS + tuple(
    sorted(set(_CARTOGRAPHY_OPS_REGISTERED) - set(_CARTOGRAPHY_OPS))
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.mark.parametrize("op_key", _ALL_OPS)
def test_op_is_registered(op_key):
    """Resolve each op the way dispatch resolves it, never by raw _REGISTRY
    membership.

    RETIRED CONTRACT (2026-08-23). This assertion used to read
    `op_key in ipc._REGISTRY` directly, on the premise stated in this module's
    own header — "importing coordinator_core.ops populates
    coordinator_core.ipc._REGISTRY". That premise was retired on 2026-08-22
    when the ops package went lazy: its module docstring now states the bare
    package NEVER populates the registry. Against a lazy package the raw
    membership read does not test wire registration at all, it tests whether
    some OTHER module pytest happened to collect first imported this op --
    so it FAILED OPEN for the ops that had such a neighbour and went red for
    the ops that did not, which is a collection-order coin flip either way.

    `_lazy_import_and_lookup` is the actual resolution path `coordinator-invoke`
    takes on a registry miss (OP_MODULE_MAP targeted import, then the
    _eager_import_all SAFE FALLBACK). Asserting against it tests the property
    this file exists to guard -- the op is REACHABLE at dispatch -- under the
    contract that actually holds, and it still fails loud for an op whose
    module is missing, unmapped, or broken at import."""
    handler = ipc._REGISTRY.get(op_key) or ipc._lazy_import_and_lookup(op_key)
    assert handler is not None, (
        f"{op_key!r} does not resolve through coordinator_core.ipc's real "
        f"dispatch path (_lazy_import_and_lookup: OP_MODULE_MAP targeted "
        f"import, then the _eager_import_all safe fallback). The op ships "
        f"present-but-dead — coordinator-invoke cannot resolve it."
    )
    assert callable(handler)


# (b) classification — every op is COMPUTE_ONLY


# cartography.symbols is DELIBERATELY MUTATING, not an omission: DR-228 § D6's
# inline in authz/classification.py. This row asserted COMPUTE_ONLY for every
# positive twin below, so a silent flip back to COMPUTE_ONLY degrades loudly
_MUTATING_OPS = ("cartography.symbols",)
_COMPUTE_ONLY_OPS = tuple(op for op in _ALL_OPS if op not in _MUTATING_OPS)


@pytest.mark.parametrize("op_key", _MUTATING_OPS)
def test_deliberately_mutating_op_stays_mutating(op_key):
    assert classify(op_key) is OpClass.MUTATING, (
        f"{op_key!r} is classified MUTATING by deliberate decision (DR-228 "
        f"§ D6 scratch-tier emit, DR-208 five-question affirmation recorded "
        f"in authz/classification.py). A flip to COMPUTE_ONLY would silently "
        f"drop that write out of the mutating-op guardrails."
    )


@pytest.mark.parametrize("op_key", _COMPUTE_ONLY_OPS)
def test_op_is_classified_compute_only(op_key):
    assert classify(op_key) is OpClass.COMPUTE_ONLY, (
        f"{op_key!r} must be COMPUTE_ONLY in authz/classification.py's "
        f"OP_CLASSIFICATION registry."
    )


# (c) _OP_KEY_SCOPE — the wire-registration gate
#     absent from _OP_KEY_SCOPE silently degrades to central scope)


@pytest.mark.parametrize("op_key", _CARTOGRAPHY_OPS_REGISTERED)
def test_cartography_op_has_none_scope(op_key):
    assert op_key in ipc.OP_KEY_SCOPE, (
        f"{op_key!r} is missing from ipc._OP_KEY_SCOPE — an op absent from "
        f"_OP_KEY_SCOPE silently degrades to central scope "
        f"(lesson 2026-07-06-compute-only-op-registration-needs-an-op)."
    )
    assert ipc.OP_KEY_SCOPE[op_key] == "none", (
        f"{op_key!r} takes an explicit target_root wire param and accesses no "
        f"repo-specific state via repo_root — expected scope 'none', got "
        f"{ipc.OP_KEY_SCOPE[op_key]!r}."
    )


def test_workflow_validate_has_none_scope():
    assert "workflow.validate" in ipc.OP_KEY_SCOPE, (
        "'workflow.validate' is missing from ipc._OP_KEY_SCOPE — an op absent "
        "from _OP_KEY_SCOPE silently degrades to central scope "
        "(lesson 2026-07-06-compute-only-op-registration-needs-an-op)."
    )
    assert ipc.OP_KEY_SCOPE["workflow.validate"] == "none", (
        "workflow.validate takes an explicit script_path (+ optional "
        "target_root) wire param and accesses no repo-specific state via "
        f"repo_root — expected scope 'none', got "
        f"{ipc.OP_KEY_SCOPE['workflow.validate']!r}."
    )


def test_workflow_scaffold_has_none_scope():
    assert "workflow.scaffold" in ipc.OP_KEY_SCOPE, (
        "'workflow.scaffold' is missing from ipc._OP_KEY_SCOPE — an op absent "
        "from _OP_KEY_SCOPE silently degrades to central scope "
        "(lesson 2026-07-06-compute-only-op-registration-needs-an-op)."
    )
    assert ipc.OP_KEY_SCOPE["workflow.scaffold"] == "none", (
        "workflow.scaffold is pure generation from caller-supplied params — "
        f"no repo state accessed at all — expected scope 'none', got "
        f"{ipc.OP_KEY_SCOPE['workflow.scaffold']!r}."
    )


def test_memo_triage_has_common_dir_scope():
    assert "memo.triage" in ipc.OP_KEY_SCOPE, (
        "'memo.triage' is missing from ipc._OP_KEY_SCOPE — an op absent from "
        "_OP_KEY_SCOPE silently degrades to central scope "
        "(lesson 2026-07-06-compute-only-op-registration-needs-an-op)."
    )
    assert ipc.OP_KEY_SCOPE["memo.triage"] == "common_dir", (
        "memo.triage's handler resolves main_worktree_root(repo_root) to read "
        "main-worktree-rooted cross-repo/archive/ + docs/decisions/ + CLAUDE.md — "
        f"expected scope 'common_dir', got {ipc.OP_KEY_SCOPE['memo.triage']!r}."
    )


def test_deferral_detect_orphan_memo_has_common_dir_scope():
    assert "deferral.detect_orphan_memo" in ipc.OP_KEY_SCOPE, (
        "'deferral.detect_orphan_memo' is missing from ipc._OP_KEY_SCOPE — an "
        "op absent from _OP_KEY_SCOPE silently degrades to central scope "
        "(lesson 2026-07-06-compute-only-op-registration-needs-an-op)."
    )
    assert ipc.OP_KEY_SCOPE["deferral.detect_orphan_memo"] == "common_dir", (
        "deferral.detect_orphan_memo's handler resolves main-worktree-rooted "
        "cross-repo/inbox/ + docs/plans/ + state/handoffs/ + docs/decisions/ — "
        f"expected scope 'common_dir', got "
        f"{ipc.OP_KEY_SCOPE['deferral.detect_orphan_memo']!r}."
    )


def test_deferral_detect_partial_strangle_has_common_dir_scope():
    assert "deferral.detect_partial_strangle" in ipc.OP_KEY_SCOPE, (
        "'deferral.detect_partial_strangle' is missing from ipc._OP_KEY_SCOPE "
        "— an op absent from _OP_KEY_SCOPE silently degrades to central scope "
        "(lesson 2026-07-06-compute-only-op-registration-needs-an-op)."
    )
    assert ipc.OP_KEY_SCOPE["deferral.detect_partial_strangle"] == "common_dir", (
        "deferral.detect_partial_strangle's handler resolves main-worktree-"
        "rooted repo state via repo_root — expected scope 'common_dir', got "
        f"{ipc.OP_KEY_SCOPE['deferral.detect_partial_strangle']!r}."
    )


def test_freshness_commit_delta_has_show_top_scope():
    """freshness.commit_delta's handler resolves HEAD's own ancestry
    (`commit_delta()` -> `_derive_deltas`'s single `git log HEAD` read) —
    the same repo-relative-but-not-cwd-dependent shape `op_scopes.py`'s own
    "show_top" comment names for this op. Absent from ipc.OP_KEY_SCOPE this
    op silently degrades to central scope
    (lesson 2026-07-06-compute-only-op-registration-needs-an-op)."""
    assert "freshness.commit_delta" in ipc.OP_KEY_SCOPE, (
        "'freshness.commit_delta' is missing from ipc._OP_KEY_SCOPE — an op "
        "absent from _OP_KEY_SCOPE silently degrades to central scope "
        "(lesson 2026-07-06-compute-only-op-registration-needs-an-op)."
    )
    assert ipc.OP_KEY_SCOPE["freshness.commit_delta"] == "show_top", (
        "freshness.commit_delta resolves HEAD's own ancestry via a single "
        f"git log read — expected scope 'show_top', got "
        f"{ipc.OP_KEY_SCOPE['freshness.commit_delta']!r}."
    )


# (d) budget-manifest.json — a COMPUTE_ONLY entry per op, at the manifest
# benchmarks/PHASE-0-MEASUREMENTS.md § "AC8 cartography overrides").

# AC8 justified overrides (measured min exceeds the 70ms COMPUTE_ONLY default's
_BUDGET_OVERRIDES = {
    "cartography.tree": 105,
    "cartography.edges": 89,
    "cartography.op_edges": 89,
    "cartography.count_references": 106,
    "cartography.chunk_table": 74,
}
_BUDGET_OVERRIDES__SUBJECT_CLASS = "op-name"


@pytest.mark.parametrize("op_key", _BUDGET_MANIFEST_OPS)
def test_op_has_budget_manifest_entry(op_key):
    budget = resolve_budget(op_key, OpClass.COMPUTE_ONLY)
    expected_target_ms = _BUDGET_OVERRIDES.get(op_key, 70)
    assert budget["target_ms"] == expected_target_ms, (
        f"{op_key!r} budget target_ms should be "
        f"{'its AC8 justified override' if op_key in _BUDGET_OVERRIDES else 'the manifest-sourced COMPUTE_ONLY default'} "
        f"({expected_target_ms}ms; phase-0 cold-start floor measured at 57.11ms), got "
        f"{budget['target_ms']!r}."
    )
    assert budget["tolerance"] == {"kind": "relative", "value": 0.2}


#     REAL registry + REAL _OP_KEY_SCOPE (no temp-patching), proving the wire


def test_memo_triage_dispatch_message_smoke(tmp_path, monkeypatch):
    archive_dir = tmp_path / "cross-repo" / "archive"
    archive_dir.mkdir(parents=True)

    memo = archive_dir / "2026-01-01-solo-memo.md"
    memo.write_text(
        textwrap.dedent(
            """\
            ---
            title: "Solo memo — no boundary keyword"
            decision: accepted
            decision_note: "routine ack, nothing distinctive here"
            ---

            Body.
            """
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "dot-claude"))

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())

    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "memo.triage",
        "params": {
            "archive_dir": str(archive_dir),
            "project_slug": "test-slug",
        },
        "_origin_worktree": str(tmp_path),
    }
    d = _run(ipc.dispatch_message(msg))

    assert "result" in d, f"dispatch_message must succeed; got error: {d.get('error')}"
    result = d["result"]
    assert result["counts"]["total"] == 1
    assert result["promote"] == []


def test_cartography_tree_dispatch_message_smoke(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "cartography-test@claude-klabauter.test"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "Cartography Test"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    (repo / "mod.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "add mod.py"], cwd=repo, check=True, **no_console_passthrough_kwargs())

    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "cartography.tree",
        "params": {"target_root": str(repo)},
    }
    d = _run(ipc.dispatch_message(msg))

    assert "result" in d, f"dispatch_message must succeed; got error: {d.get('error')}"
    result = d["result"]
    assert result["file_count"] == 1
    assert "mod.py" in result["files"]


def test_workflow_validate_dispatch_message_smoke(tmp_path):
    script = tmp_path / "conformant.mjs"
    script.write_text(
        textwrap.dedent(
            """\
            export const meta = {
              name: 'demo-workflow',
              description: 'a conformant demo workflow',
              phases: ['collect'],
            };

            async function run(ctx) {
              phase('collect');
              return await agent({ prompt: 'do work', model: 'sonnet' });
            }
            """
        ),
        encoding="utf-8",
    )

    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "workflow.validate",
        "params": {"script_path": str(script)},
    }
    d = _run(ipc.dispatch_message(msg))

    assert "result" in d, f"dispatch_message must succeed; got error: {d.get('error')}"
    result = d["result"]
    assert result["ok"] is True
    assert result["error_count"] == 0


#     module", and ops/__init__.py's registration loop SWALLOWS that
#     coordinator_core.ipc._REGISTRY -- with no subsequent explicit
#     Fix: `_find_row_spans_in_plan`/`_find_row_spans`/`_ROW_START_RE` moved
#     LAZY-PACKAGE ARMING (2026-08-23): coordinator_core.ops was converted to
#     registry-miss SAFE FALLBACK reaches. It still swallows a per-module

_IMPORT_ORDER_PROBE = """
import sys
{first_import}
{second_import}
import coordinator_core.ops
coordinator_core.ops._eager_import_all()
from coordinator_core.ipc import _REGISTRY
poisoned = coordinator_core.ops.get_poisoned_modules()
assert not poisoned, (
    "modules poisoned during _eager_import_all() after "
    "{order_label} import order: " + repr(poisoned)
)
assert "deliverable.cascade_retract" in _REGISTRY, (
    "deliverable.cascade_retract missing from _REGISTRY after "
    "{order_label} import order"
)
print("REGISTERED")
"""


def _run_import_order_probe(first_import: str, second_import: str, order_label: str) -> None:
    script = _IMPORT_ORDER_PROBE.format(
        first_import=first_import, second_import=second_import, order_label=order_label
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, (
        f"{order_label} import order failed in a fresh interpreter:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "REGISTERED" in result.stdout, (
        f"{order_label} import order did not confirm registration:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_cascade_retract_registers_close_out_and_stamp_imported_first():
    _run_import_order_probe(
        first_import="import coordinator_core.execute_plan_assemble.close_out_and_stamp",
        second_import="import coordinator_core.ops.cascade_retract",
        order_label="close_out_and_stamp-then-cascade_retract",
    )


def test_cascade_retract_registers_cascade_retract_imported_first():
    _run_import_order_probe(
        first_import="import coordinator_core.ops.cascade_retract",
        second_import="import coordinator_core.execute_plan_assemble.close_out_and_stamp",
        order_label="cascade_retract-then-close_out_and_stamp",
    )


#     _REGISTRY whenever something imported pickup_assemble before ops.
#     reproduces the ImportError and drops both op keys from _REGISTRY.)
#     LAZY-PACKAGE ARMING (2026-08-23): coordinator_core.ops was converted to
#     registry-miss SAFE FALLBACK reaches. It still swallows a per-module

_PICKUP_ASSEMBLE_ORDER_PROBE = """
import coordinator_core.pickup_assemble
import coordinator_core.ops
coordinator_core.ops._eager_import_all()
from coordinator_core.ipc import _REGISTRY
poisoned = coordinator_core.ops.get_poisoned_modules()
assert not poisoned, (
    "modules poisoned during _eager_import_all() after "
    "pickup_assemble-then-ops import order: " + repr(poisoned)
)
# deliverable.cascade_terminal was DELETED 2026-08-27 (kill ledger K-104,
# 200ms sweep). The import-order hazard this probe guards is unchanged and
# still worth pinning -- cascade_backstop_sweep exercises the same
# pickup_assemble-then-ops path through the same package.
assert "deliverable.cascade_terminal" not in _REGISTRY, (
    "deliverable.cascade_terminal is killed and must not re-register"
)
assert "deliverable.cascade_backstop_sweep" in _REGISTRY, (
    "deliverable.cascade_backstop_sweep missing from _REGISTRY after "
    "pickup_assemble-then-ops import order"
)
print("REGISTERED")
"""


def test_cascade_ops_register_when_pickup_assemble_imported_first():
    result = subprocess.run(
        [sys.executable, "-c", _PICKUP_ASSEMBLE_ORDER_PROBE],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, (
        "pickup_assemble-then-ops import order failed in a fresh interpreter:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "REGISTERED" in result.stdout, (
        "pickup_assemble-then-ops import order did not confirm registration:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_workflow_scaffold_dispatch_message_smoke():
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "workflow.scaffold",
        "params": {"name": "demo-workflow", "description": "a demo workflow"},
    }
    d = _run(ipc.dispatch_message(msg))

    assert "result" in d, f"dispatch_message must succeed; got error: {d.get('error')}"
    result = d["result"]
    assert "script" in result
    assert "export const meta" in result["script"]
