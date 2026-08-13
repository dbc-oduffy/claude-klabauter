"""
coordinator_core.tests.test_op_scope_parity — Parity surface contract tests for
OP_KEY_SCOPE, WORKTREE_SCOPED_OPS, and auth-seam read tokens (cross-repo importable
exports, DR § AC-1b and coordinator-claude-shim auth-seam symmetry).

Tests:
  - WORKTREE_SCOPED_OPS derivation integrity: equals the set of keys in OP_KEY_SCOPE
    whose scope ∈ {common_dir, show_top}.
  - OP_KEY_SCOPE immutability: assignment raises TypeError (MappingProxyType).
  - Top-level package importability: both op-scope names and token readers importable
    from coordinator_core.
  - Spot-asserts on known members of WORKTREE_SCOPED_OPS (scoped ops IN) and known
    non-members (central/none ops NOT IN).
  - __all__ membership: read_token and read_token_ro declared in package __all__.

Spec backlink: docs/decisions/2026-07-04-coordinator-core-global-multiplex-topology.md § AC-1b
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from coordinator_core.ipc import OP_KEY_SCOPE, WORKTREE_SCOPED_OPS, _OP_KEY_SCOPE


# ---------------------------------------------------------------------------
# Derivation integrity
# ---------------------------------------------------------------------------

def test_worktree_scoped_ops_derivation():
    """WORKTREE_SCOPED_OPS must equal the set derived from _OP_KEY_SCOPE at import time."""
    expected = frozenset(
        op for op, scope in _OP_KEY_SCOPE.items() if scope in ("common_dir", "show_top")
    )
    assert WORKTREE_SCOPED_OPS == expected


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------

def test_deliverable_rollup_is_common_dir_scoped():
    """deliverable.rollup MUST be common_dir-keyed — it resolves DR-207 spine edges over
    main-worktree-rooted state/, so dispatch must supply the worktree common_dir. A missing
    keying entry silently degrades it to central-scope (repo_root=None → empty for every
    deliverable); the command-type smoke caught exactly that regression at execute time.
    Spec: docs/plans/2026-07-06-claude-klabauter-deliverable-spine-factsupply-op.md § C2/C3."""
    assert _OP_KEY_SCOPE.get("deliverable.rollup") == "common_dir"
    assert "deliverable.rollup" in WORKTREE_SCOPED_OPS


def test_records_query_is_common_dir_scoped():
    """records.query MUST be common_dir-keyed — it reads main-worktree-rooted project records,
    so the real transport (coordinator_core.invoke) must inject repo_root. A missing keying entry
    falls the op through to scope "none" → repo_root=None → the handler's empty-payload branch,
    which turns every downstream gate (coordinator-claude assert-no-terminal-plans / audit-roadmap stub-coverage)
    into a silent dead false-passer. The differential parity suite drives the handler with an
    explicit fixture root on both sides and never exercises invoke scope-registration, so this
    membership assert is the ONLY regression that guards the real-transport path.
    Cross-repo memo: cross-repo/inbox/2026-07-06-strang-11-records-query-repo-root-not-injected.md."""
    assert _OP_KEY_SCOPE.get("records.query") == "common_dir"
    assert "records.query" in WORKTREE_SCOPED_OPS


def test_op_key_scope_is_immutable():
    """OP_KEY_SCOPE is a MappingProxyType — assignment must raise TypeError."""
    with pytest.raises(TypeError):
        OP_KEY_SCOPE["ping"] = "x"  # type: ignore[index]


# ---------------------------------------------------------------------------
# Top-level package importability
# ---------------------------------------------------------------------------

def test_top_level_package_importable():
    """Both op-scope names must be importable directly from the coordinator_core package."""
    from coordinator_core import OP_KEY_SCOPE as pkg_scope, WORKTREE_SCOPED_OPS as pkg_scoped
    # Verify identity — same objects, not copies.
    assert pkg_scope is OP_KEY_SCOPE
    assert pkg_scoped is WORKTREE_SCOPED_OPS


# ---------------------------------------------------------------------------
# Auth-seam token reader parity (coordinator-claude-shim auth-seam consumer symmetry)
# ---------------------------------------------------------------------------

def test_token_readers_top_level_importable():
    """read_token and read_token_ro must be importable directly from coordinator_core."""
    from coordinator_core import read_token as pkg_read_token, read_token_ro as pkg_read_token_ro
    from coordinator_core.authz.token import read_token, read_token_ro
    # Verify identity — same callables, not copies.
    assert pkg_read_token is read_token
    assert pkg_read_token_ro is read_token_ro


def test_token_readers_in_dunder_all():
    """read_token and read_token_ro must be declared in coordinator_core.__all__."""
    import coordinator_core
    assert "read_token" in coordinator_core.__all__
    assert "read_token_ro" in coordinator_core.__all__


# ---------------------------------------------------------------------------
# Spot-asserts — known members (scoped) and non-members (central / none)
# ---------------------------------------------------------------------------

def test_known_scoped_ops_in_worktree_scoped_ops():
    """Known worktree-scoped ops must be members of WORKTREE_SCOPED_OPS."""
    assert "coverage.gate" in WORKTREE_SCOPED_OPS
    assert "handoff.has_live_children" in WORKTREE_SCOPED_OPS
    assert "hooks.nudge_foreground_agent_dispatch" in WORKTREE_SCOPED_OPS
    assert "hooks.nudge_em_code_dispatch" in WORKTREE_SCOPED_OPS


def test_known_non_scoped_ops_not_in_worktree_scoped_ops():
    """Known none-scoped ops must NOT be members of WORKTREE_SCOPED_OPS."""
    assert "ping" not in WORKTREE_SCOPED_OPS
    assert "hooks.suggest_sonnet_research" not in WORKTREE_SCOPED_OPS


def test_emit_ops_reclassified_to_common_dir():
    """Emit ops must be common_dir-scoped (per-repo emission, 2026-07-07 cutover).

    Prior to 2026-07-07, artifact.emit / backlog.record / goal.append were classified
    "central" — bypassing the per-request repo key entirely. The per-repo-emission-cutover
    plan (chunk C3) reclassified them to "common_dir" so each emitting repo's
    _origin_worktree is used to derive the per-repo snapshot root. Emit ops now REQUIRE
    _origin_worktree and appear in WORKTREE_SCOPED_OPS. A regression here would silently
    route all emit ops to repo_root=None and re-introduce the hardlocked-to-~/.claude bug.
    Spec: docs/plans/2026-07-07-per-repo-emission-cutover.md § C3 / AC1
    """
    assert _OP_KEY_SCOPE.get("artifact.emit") == "common_dir"
    assert _OP_KEY_SCOPE.get("backlog.record") == "common_dir"
    assert _OP_KEY_SCOPE.get("goal.append") == "common_dir"
    assert "artifact.emit" in WORKTREE_SCOPED_OPS
    assert "backlog.record" in WORKTREE_SCOPED_OPS
    assert "goal.append" in WORKTREE_SCOPED_OPS


def test_emit_cadence_is_common_dir_scoped():
    """emit.cadence MUST be common_dir-scoped — it sequences backlog.record then artifact.emit
    over the same common_dir key, deriving main_worktree_root(common_dir) exactly like its two
    sub-ops. A missing/wrong scope entry would silently route it to repo_root=None.
    Spec: cross-repo/inbox/2026-07-11-claude-central-em-backlog-record-cadence-relocation-reconcile.md
    """
    assert _OP_KEY_SCOPE.get("emit.cadence") == "common_dir"
    assert "emit.cadence" in WORKTREE_SCOPED_OPS


# ---------------------------------------------------------------------------
# DR-279 blast-radius table pinning
# ---------------------------------------------------------------------------

_DR_279_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs" / "decisions" / "DR-279-repo-on-a-none-scoped-op-fails-loud.md"
)


def _dr_279_enumerated_ops() -> frozenset[str]:
    """Parse the whitespace-delimited op table out of DR-279's fenced code block.

    The table lives inside a single ``` ... ``` block under the "## Blast radius"
    heading, whitespace-column-aligned prose (not one-per-line) — split on any
    run of whitespace rather than assuming a delimiter.
    """
    text = _DR_279_PATH.read_text(encoding="utf-8")
    match = re.search(r"## Blast radius.*?```\n(.*?)```", text, re.DOTALL)
    assert match, "DR-279's blast-radius fenced code block not found — doc structure changed"
    return frozenset(match.group(1).split())


def test_dr_279_blast_radius_table_matches_live_registry():
    """DR-279's hand-copied 73-op enumeration must equal the live registry's none-scoped ops.

    Review: code-reviewer (P3) — the table was presented as "enumerated from the live
    registry... not guessed" but had no test pinning it to that registry, so it could
    drift silently exactly like the op docstrings C10 swept. This test re-derives the
    same set the DR's own enumeration methodology describes (every op in the eager
    `_REGISTRY` whose `OP_KEY_SCOPE.get(op, "none")` resolves to "none") and asserts
    equality with the doc's parsed table. A failure here means either a new/removed op
    changed the none-scoped set, or the op was reclassified — in either case DR-279's
    prose table needs a matching update, not silent drift.
    """
    import coordinator_core.ops  # noqa: F401  (eager import populates _REGISTRY)
    from coordinator_core.ipc import _REGISTRY

    live_none_scoped = frozenset(
        op for op in _REGISTRY if OP_KEY_SCOPE.get(op, "none") == "none"
    )
    doc_enumerated = _dr_279_enumerated_ops()
    assert doc_enumerated == live_none_scoped, (
        "DR-279's blast-radius table has drifted from the live registry.\n"
        f"  In doc but not live registry: {sorted(doc_enumerated - live_none_scoped)}\n"
        f"  In live registry but not doc: {sorted(live_none_scoped - doc_enumerated)}\n"
        "  Update docs/decisions/DR-279-repo-on-a-none-scoped-op-fails-loud.md's "
        "'Blast radius' table to match."
    )
