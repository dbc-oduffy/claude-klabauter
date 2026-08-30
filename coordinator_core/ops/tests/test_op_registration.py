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

# The original 5 cartography.* ops carried by this file's classification (b)
# and registered (a) checks — cartography.stack / cartography.count_references
# are DELIBERATELY excluded here: their absence from authz/classification.py
# is a separately tracked, deliberately-waived debt item
# (state/debt-backlog/2026-07-23-authz-drift-guard-ops-registered-without-
# 52137f1ff6b9.yaml, via authz/registration_quad.py's
# _KNOWN_UNCLASSIFIED_OPS_DEBT) — folding them into _ALL_OPS here would make
# test_op_is_classified_compute_only fail on an out-of-scope waiver.
_CARTOGRAPHY_OPS = (
    "cartography.tree",
    "cartography.file_index",
    # cartography.churn -- DELETED 2026-08-27 (kill ledger K-111, 200ms sweep).
    # Module removed outright; no non-test importers.
    "cartography.symbols",
    "cartography.edges",
)
_ALL_OPS = _CARTOGRAPHY_OPS + (
    "memo.triage",
    "workflow.validate",
    "workflow.scaffold",
    "deferral.detect_orphan_memo",
    "deferral.detect_partial_strangle",
)

# Derived from the authoritative wire-registration source (ipc.OP_KEY_SCOPE,
# itself sourced from op_scopes._OP_KEY_SCOPE) rather than a hardcoded tuple —
# a new cartography.* op lands in OP_KEY_SCOPE the same commit it's wired, so
# this set can't silently drift stale the way the hand-maintained
# _CARTOGRAPHY_OPS literal above did (defect: cartography.stack /
# cartography.count_references were registered and scoped but absent from
# this file's hardcoded op tuple, leaving them with no budget-manifest gate
# coverage at all — the fix targets the budget-manifest gate specifically;
# see the classification note above for why they stay out of _ALL_OPS).
_CARTOGRAPHY_OPS_REGISTERED = tuple(
    sorted(op for op in ipc.OP_KEY_SCOPE if op.startswith("cartography."))
)

# The budget-manifest gate's op set: every _ALL_OPS entry, plus any
# cartography.* op wired into OP_KEY_SCOPE that _ALL_OPS doesn't already
# cover (currently cartography.stack / cartography.count_references) — this
# is what makes a future cartography op's budget-manifest omission fail loud
# without also pulling it into the classification/registered checks above.
_BUDGET_MANIFEST_OPS = _ALL_OPS + tuple(
    sorted(set(_CARTOGRAPHY_OPS_REGISTERED) - set(_CARTOGRAPHY_OPS))
)

# (e)/(f)/(g) dispatch_message smoke tests exercise the scope-"none"/
# "common_dir" repo_root resolution path end-to-end against a real git repo —
# that resolution logic reads actual repo state (cwd-relative discovery,
# tracked-file enumeration), which a mock would bypass rather than prove.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# (a) registry presence — ops/__init__.py imports every op module
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# (b) classification — every op is COMPUTE_ONLY
# ---------------------------------------------------------------------------


# cartography.symbols is DELIBERATELY MUTATING, not an omission: DR-228 § D6's
# scratch-tier write (params["emit"] writes
# <target_root>/state/scratch/cartography-symbols/<run_id>/symbols.json),
# classified 2026-08-20 with the full DR-208 five-question affirmation recorded
# inline in authz/classification.py. This row asserted COMPUTE_ONLY for every
# _ALL_OPS entry and so went red the moment that decision landed — the test was
# the stale side, never the classification. Excluded here and pinned by its own
# positive twin below, so a silent flip back to COMPUTE_ONLY degrades loudly
# rather than passing unnoticed.
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


# ---------------------------------------------------------------------------
# (c) _OP_KEY_SCOPE — the wire-registration gate
#     (lesson 2026-07-06-compute-only-op-registration-needs-an-op: an op
#     absent from _OP_KEY_SCOPE silently degrades to central scope)
# ---------------------------------------------------------------------------


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
    """Review: code-reviewer Finding 6 — deferral.detect_orphan_memo's
    handler resolves main-worktree-rooted cross-repo/inbox/ +
    docs/plans/ + state/handoffs/ + docs/decisions/, mirroring memo.triage's
    common_dir scope. Absent from op_scopes.py this op silently degrades to
    central scope (lesson 2026-07-06-compute-only-op-registration-needs-an-op)."""
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
    """Review: code-reviewer Finding 6 — same common_dir precedent for
    Detector 1 (sibling op key, added by the same commit)."""
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


# ---------------------------------------------------------------------------
# (d) budget-manifest.json — a COMPUTE_ONLY entry per op, at the manifest
# default UNLESS an AC8 justified override applies (AST-heavy / whole-repo-walk
# members measured to legitimately exceed the default — see
# benchmarks/PHASE-0-MEASUREMENTS.md § "AC8 cartography overrides").
# ---------------------------------------------------------------------------

# AC8 justified overrides (measured min exceeds the 70ms COMPUTE_ONLY default's
# tolerance band): cartography.tree (whole-repo git ls-files walk + per-file
# read for loc), cartography.edges (AST-heavy per-file import/call-graph
# extraction). memo.triage and the remaining cartography ops stay on default.
_BUDGET_OVERRIDES = {
    "cartography.tree": 105,
    "cartography.edges": 89,
    "cartography.op_edges": 89,
    "cartography.count_references": 106,
    "cartography.chunk_table": 74,
}


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


# ---------------------------------------------------------------------------
# (e) command-type dispatch_message smoke — memo.triage end-to-end via the
#     REAL registry + REAL _OP_KEY_SCOPE (no temp-patching), proving the wire
#     registration itself, not just the handler in isolation.
# ---------------------------------------------------------------------------


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
    assert result["promote"] == []  # bare accepted, no boundary keyword => score 0


# ---------------------------------------------------------------------------
# (f) command-type dispatch_message smoke for a cartography.* op (Finding 6,
#     2026-07-12-codereview-slicecartography-substrate-b-wave) — section (e)
#     above proves the wire-level dispatch_message contract only for
#     memo.triage; nothing previously exercised the scope-"none" repo_root
#     resolution path end-to-end for any of the 5 cartography ops. Each op's
#     own cartography/tests/test_*.py calls the handler function directly
#     (bypassing dispatch_message's param unwrapping / repo_root resolution /
#     response shaping), which is exactly the gap section (e) closes for
#     memo.triage but left open here.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# (g) command-type dispatch_message smoke for workflow.validate — proves the
#     scope-"none" wire path end-to-end for this op, the same way (f) does
#     for cartography.tree. This op's own footgun/heuristic test coverage
#     lives in ops/tests/test_workflow_validate.py, which calls the handler
#     directly; this smoke is the only place the FULL dispatch_message path
#     (param unwrapping, repo_root resolution, response shaping) is exercised
#     for workflow.validate.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# (h) command-type dispatch_message smoke for workflow.scaffold — proves the
#     scope-"none" wire path end-to-end for this op, the same way (g) does
#     for workflow.validate. This op's own pattern/round-trip coverage lives
#     in ops/tests/test_workflow_scaffold.py.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# (i) import-order regression — deliverable.cascade_retract must register
#     regardless of which of its two mutually-adjacent modules a caller
#     happens to import first.
#
#     Defect (2026-08-06, C1): coordinator_core/ops/cascade_retract.py used
#     to import `_find_row_spans_in_plan` from
#     coordinator_core.execute_plan_assemble.close_out_and_stamp, which
#     itself imports six coordinator_core.ops.* modules at top level.
#     Importing close_out_and_stamp FIRST (standalone, before anything else
#     has touched coordinator_core.ops) makes its own
#     `from coordinator_core.ops.ceremony import git_native` line trigger a
#     NESTED full `coordinator_core.ops` package init (coordinator_core.ops
#     hadn't been touched yet) while close_out_and_stamp itself is still
#     mid-body -- so that nested init's own eager-import of cascade_retract
#     fails with "cannot import name '...' from partially initialized
#     module", and ops/__init__.py's registration loop SWALLOWS that
#     ImportError (prints + continues) rather than raising, so
#     "deliverable.cascade_retract" silently failed to land in
#     coordinator_core.ipc._REGISTRY -- with no subsequent explicit
#     re-import of cascade_retract to self-heal it (this is the shape a
#     real server startup's bare `import coordinator_core.ops` hits, not the
#     self-healing shape a script that later re-imports cascade_retract
#     explicitly would get).
#
#     Fix: `_find_row_spans_in_plan`/`_find_row_spans`/`_ROW_START_RE` moved
#     to the leaf module coordinator_core.execute_plan_assemble.row_spans,
#     which imports nothing from coordinator_core.ops (directly or
#     transitively) -- both close_out_and_stamp and cascade_retract import
#     from that leaf instead of from each other.
#
#     This MUST run in a fresh subprocess per order: pytest's own collection
#     has already imported both modules (and all of coordinator_core.ops)
#     into this process's sys.modules by the time any test body runs, so an
#     in-process import-order test would prove nothing.
#     LAZY-PACKAGE ARMING (2026-08-23): coordinator_core.ops was converted to
#     lazy registration on 2026-08-22 -- its own module docstring states the
#     bare package NEVER populates the op-registry. These probes were written
#     against the prior contract, where package init ran the registration walk
#     itself, and went red the moment that contract was retired: they were
#     asserting eager registration in a package that is deliberately not
#     eager. They are NOT force-importing to dodge the import-cliff budget --
#     _eager_import_all() is the escape hatch ops/__init__.py exposes for
#     exactly this "rare full-registration need", and it is what ipc.py's own
#     registry-miss SAFE FALLBACK reaches. It still swallows a per-module
#     ImportError (prints + continues), so a reintroduced cycle still drops
#     the op key and this probe still bites -- the regression these probes
#     exist to catch is preserved, only its arming is now explicit.
# ---------------------------------------------------------------------------

_IMPORT_ORDER_PROBE = """
import sys
{first_import}
{second_import}
import coordinator_core.ops
coordinator_core.ops._eager_import_all()
from coordinator_core.ipc import _REGISTRY
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


# ---------------------------------------------------------------------------
# (j) import-order regression — the pickup_assemble-entry cycle.
#
#     Defect (2026-08-06, commit f9c838cf0): coordinator_core.pickup_assemble
#     imports coordinator_core.ops.extract_scope_paths, which forces Python to
#     init the coordinator_core.ops PACKAGE first (parent packages init before
#     a submodule import completes). That package init's own eager
#     registration walk reaches coordinator_core.ops.deliverable_cascade ->
#     cascade_baton_rows -> close_out_and_stamp, which used to import
#     `resolve_repo_root` from coordinator_core.pickup_assemble at module
#     scope -- closing the cycle back on the still-partially-initialized
#     pickup_assemble module. ops/__init__.py's registration loop swallows
#     that ImportError (prints + continues), so "deliverable.cascade_terminal"
#     and "deliverable.cascade_backstop_sweep" silently dropped out of
#     _REGISTRY whenever something imported pickup_assemble before ops.
#
#     Fix: the `resolve_repo_root` import was deferred into
#     close_out_and_stamp's own function body (see that module's own comment
#     at the import site).
#
#     Like the (i) probe above, this MUST run in a fresh subprocess: pytest's
#     own collection has already imported both modules by the time any test
#     body runs, so an in-process import-order test would prove nothing.
#     (Verified live: reverting the fix and re-running this exact probe script
#     reproduces the ImportError and drops both op keys from _REGISTRY.)
#     LAZY-PACKAGE ARMING (2026-08-23): coordinator_core.ops was converted to
#     lazy registration on 2026-08-22 -- its own module docstring states the
#     bare package NEVER populates the op-registry. These probes were written
#     against the prior contract, where package init ran the registration walk
#     itself, and went red the moment that contract was retired: they were
#     asserting eager registration in a package that is deliberately not
#     eager. They are NOT force-importing to dodge the import-cliff budget --
#     _eager_import_all() is the escape hatch ops/__init__.py exposes for
#     exactly this "rare full-registration need", and it is what ipc.py's own
#     registry-miss SAFE FALLBACK reaches. It still swallows a per-module
#     ImportError (prints + continues), so a reintroduced cycle still drops
#     the op key and this probe still bites -- the regression these probes
#     exist to catch is preserved, only its arming is now explicit.
# ---------------------------------------------------------------------------

_PICKUP_ASSEMBLE_ORDER_PROBE = """
import coordinator_core.pickup_assemble
import coordinator_core.ops
coordinator_core.ops._eager_import_all()
from coordinator_core.ipc import _REGISTRY
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
