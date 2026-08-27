"""Tests for coordinator_core.ops.emit.priority_resolve — the nearest-explicit-
ancestor resolver.

Fixture nodes are plain frontmatter'd .md files on disk (dag.walk_forward reads
real files) written directly under a tmp dir; ledger entries are injected via
``resolve_priority(..., ledger_entries=...)`` rather than round-tripped through
``load_priority_ledger``'s disk read — the resolution algorithm is this
module's subject under test, not the ledger loader's I/O.

Spec backlink: DoE-claude DoE-claude:pln-priority-ledger-durable-pm-pri-817d40 § C5, § C10.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.ops.emit.priority_resolve import (
    load_priority_ledger,
    resolve_priority,
)

# Review: coordinator:code-reviewer — Finding 1: _write_node/_ledger extracted to conftest.py
# (shared across the five priority-ledger test modules that used a byte-for-byte copy).
from coordinator_core.ops.emit.tests.conftest import _ledger, _write_node  # noqa: F401


@pytest.fixture()
def node_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state" / "handoffs"
    d.mkdir(parents=True)
    return d


# ---------------------------------------------------------------------------
# THE ACCEPTANCE ORACLE — A(explicit high) -> B(explicit low) -> C(no explicit)
# C resolves to low, NEVER high.
# ---------------------------------------------------------------------------


def test_worked_example_resolves_to_nearest_explicit_ancestor_low(node_dir: Path):
    _write_node(node_dir, "A.md", handoff_id="A_id", predecessor=None)
    _write_node(node_dir, "B.md", handoff_id="B_id", predecessor="A.md")
    c_path = _write_node(node_dir, "C.md", handoff_id="C_id", predecessor="B.md")

    ledger = _ledger(A_id="high", B_id="low")

    result = resolve_priority(str(c_path), "C_id", ledger_entries=ledger)

    assert result["effective_priority"] == "low"
    assert result["origin"] == "inherited"
    assert result["source_id"] == "B_id"


def test_worked_example_does_not_resolve_to_top_of_chain_high(node_dir: Path):
    """The 'top of chain' reading is RETIRED — any implementation that yields
    'high' for C is wrong, however plausible its reading of older prose."""
    _write_node(node_dir, "A.md", handoff_id="A_id", predecessor=None)
    _write_node(node_dir, "B.md", handoff_id="B_id", predecessor="A.md")
    c_path = _write_node(node_dir, "C.md", handoff_id="C_id", predecessor="B.md")

    ledger = _ledger(A_id="high", B_id="low")

    result = resolve_priority(str(c_path), "C_id", ledger_entries=ledger)

    assert result["effective_priority"] != "high"


# ---------------------------------------------------------------------------
# Explicit entry on N itself short-circuits the walk entirely.
# ---------------------------------------------------------------------------


def test_explicit_entry_on_node_itself_wins(node_dir: Path):
    _write_node(node_dir, "A.md", handoff_id="A_id", predecessor=None)
    b_path = _write_node(node_dir, "B.md", handoff_id="B_id", predecessor="A.md")

    ledger = _ledger(A_id="high", B_id="urgent")

    result = resolve_priority(str(b_path), "B_id", ledger_entries=ledger)

    assert result == {"effective_priority": "urgent", "origin": "explicit", "source_id": "B_id"}


# ---------------------------------------------------------------------------
# predecessor: none halts the walk — the spinoff wall falls out of the
# spine's shape, no special-case branch.
# ---------------------------------------------------------------------------


def test_predecessor_none_halts_walk_no_suggested(node_dir: Path):
    d_path = _write_node(node_dir, "D.md", handoff_id="D_id", predecessor=None)

    result = resolve_priority(str(d_path), "D_id", ledger_entries=_ledger())

    assert result == {"effective_priority": None, "origin": "none", "source_id": None}


def test_predecessor_none_halts_walk_falls_through_to_suggested(node_dir: Path):
    d_path = _write_node(
        node_dir, "D.md", handoff_id="D_id", predecessor=None, suggested_priority="medium"
    )

    result = resolve_priority(str(d_path), "D_id", ledger_entries=_ledger())

    assert result == {"effective_priority": "medium", "origin": "suggested", "source_id": None}


# ---------------------------------------------------------------------------
# suggested_priority on N loses to any nearest explicit ancestor found.
# ---------------------------------------------------------------------------


def test_suggested_priority_loses_to_explicit_ancestor(node_dir: Path):
    _write_node(node_dir, "A.md", handoff_id="A_id", predecessor=None)
    _write_node(node_dir, "B.md", handoff_id="B_id", predecessor="A.md")
    c_path = _write_node(
        node_dir, "C.md", handoff_id="C_id", predecessor="B.md", suggested_priority="urgent"
    )

    ledger = _ledger(A_id="high", B_id="low")

    result = resolve_priority(str(c_path), "C_id", ledger_entries=ledger)

    assert result["effective_priority"] == "low"
    assert result["origin"] == "inherited"


# ---------------------------------------------------------------------------
# The `none` sentinel on an ancestor terminates the walk (that ancestor IS
# the nearest explicit entry) and is distinguishable from no entry existing
# anywhere (origin: "none").
# ---------------------------------------------------------------------------


def test_none_sentinel_ancestor_terminates_walk_and_is_distinguishable(node_dir: Path):
    _write_node(node_dir, "F.md", handoff_id="F_id", predecessor=None)
    e_path = _write_node(node_dir, "E.md", handoff_id="E_id", predecessor="F.md")

    ledger = _ledger(F_id="none")

    result = resolve_priority(str(e_path), "E_id", ledger_entries=ledger)

    assert result["effective_priority"] is None
    assert result["origin"] == "inherited"
    assert result["source_id"] == "F_id"

    # Contrast: an E with no ancestor entry at all gets origin "none", not
    # "inherited" — same effective_priority (None) but a different, and
    # distinguishable, provenance.
    g_path = _write_node(node_dir, "G.md", handoff_id="G_id", predecessor=None)
    no_entry_result = resolve_priority(str(g_path), "G_id", ledger_entries=_ledger())
    assert no_entry_result["origin"] == "none"
    assert no_entry_result["source_id"] is None


# ---------------------------------------------------------------------------
# Fan-in with differing parent values: no value, origin "ambiguous". Never
# silently pick one parent.
# ---------------------------------------------------------------------------


def test_fan_in_differing_priorities_yields_ambiguous(node_dir: Path):
    _write_node(node_dir, "H.md", handoff_id="H_id", predecessor=None)
    _write_node(node_dir, "I.md", handoff_id="I_id", predecessor=None)
    j_path = _write_node(
        node_dir,
        "J.md",
        handoff_id="J_id",
        predecessor="H.md",
        additional_predecessors=["I.md"],
    )

    ledger = _ledger(H_id="high", I_id="low")

    result = resolve_priority(str(j_path), "J_id", ledger_entries=ledger)

    assert result == {"effective_priority": None, "origin": "ambiguous", "source_id": None}


def test_fan_in_agreeing_priorities_resolves_inherited(node_dir: Path):
    _write_node(node_dir, "H.md", handoff_id="H_id", predecessor=None)
    _write_node(node_dir, "I.md", handoff_id="I_id", predecessor=None)
    j_path = _write_node(
        node_dir,
        "J.md",
        handoff_id="J_id",
        predecessor="H.md",
        additional_predecessors=["I.md"],
    )

    ledger = _ledger(H_id="medium", I_id="medium")

    result = resolve_priority(str(j_path), "J_id", ledger_entries=ledger)

    assert result["effective_priority"] == "medium"
    assert result["origin"] == "inherited"


# ---------------------------------------------------------------------------
# forked_from / origin_handoff / etc. are NON-EDGES — must not be traversed.
# ---------------------------------------------------------------------------


def test_forked_from_is_not_traversed(node_dir: Path):
    _write_node(node_dir, "K.md", handoff_id="K_id", predecessor=None)
    forked_path = _write_node(
        node_dir, "L.md", handoff_id="L_id", predecessor=None, forked_from="K.md"
    )

    ledger = _ledger(K_id="urgent")

    result = resolve_priority(str(forked_path), "L_id", ledger_entries=ledger)

    # L's own predecessor is none (spinoff-shaped); forked_from must not be
    # walked, so K's urgent priority is never seen.
    assert result == {"effective_priority": None, "origin": "none", "source_id": None}


# ---------------------------------------------------------------------------
# load_priority_ledger — live + archive union, target_id keyed by filename.
# ---------------------------------------------------------------------------


def test_load_priority_ledger_unions_live_and_archive(tmp_path: Path):
    state_root = tmp_path / "state"
    live_dir = state_root / "priority-ledger"
    live_dir.mkdir(parents=True)
    (live_dir / "live-target.yaml").write_text(
        "target_id: live-target\ntarget_kind: handoff\npriority: high\nsource: op\n",
        encoding="utf-8",
    )

    archive_month_dir = tmp_path / "archive" / "priority-ledger" / "2026-06"
    archive_month_dir.mkdir(parents=True)
    (archive_month_dir / "archived-target.yaml").write_text(
        "target_id: archived-target\ntarget_kind: handoff\npriority: low\nsource: op\n",
        encoding="utf-8",
    )

    ledger = load_priority_ledger(state_root=str(state_root))

    assert ledger["live-target"]["priority"] == "high"
    assert ledger["archived-target"]["priority"] == "low"


def test_load_priority_ledger_live_wins_on_collision(tmp_path: Path):
    state_root = tmp_path / "state"
    live_dir = state_root / "priority-ledger"
    live_dir.mkdir(parents=True)
    (live_dir / "dup.yaml").write_text(
        "target_id: dup\ntarget_kind: handoff\npriority: urgent\nsource: op\n",
        encoding="utf-8",
    )

    archive_month_dir = tmp_path / "archive" / "priority-ledger" / "2026-06"
    archive_month_dir.mkdir(parents=True)
    (archive_month_dir / "dup.yaml").write_text(
        "target_id: dup\ntarget_kind: handoff\npriority: low\nsource: op\n",
        encoding="utf-8",
    )

    ledger = load_priority_ledger(state_root=str(state_root))

    assert ledger["dup"]["priority"] == "urgent"


def test_load_priority_ledger_absent_dirs_yield_empty(tmp_path: Path):
    state_root = tmp_path / "state"
    state_root.mkdir()

    assert load_priority_ledger(state_root=str(state_root)) == {}


def test_load_priority_ledger_unresolvable_central_root_yields_empty(monkeypatch):
    """An unresolvable central state root (StateRootError -- e.g.
    repos.claude_klabauter not configured, or a sandboxed test monkeypatching
    HOME/COORDINATOR_SETTINGS_HOME) must degrade to an empty ledger, not
    propagate -- this is the emit-graceful-absent contract: absent/unreachable
    ledger input is ordinary, not fatal. Regression for the emit-suite crash
    where an unguarded ``load_priority_ledger()`` call in
    ``ops/emit/sections/handoffs.py`` turned StateRootError into a hard
    failure across the graceful-absent test battery.
    """
    from coordinator_core.ops.emit import priority_resolve
    from coordinator_core.state_root import StateRootError

    def _raise_state_root_error(**kwargs):
        raise StateRootError("central state root unresolvable in test")

    monkeypatch.setattr(priority_resolve, "coordinator_state_root", _raise_state_root_error)

    with pytest.warns(UserWarning, match="central state root unresolvable"):
        result = load_priority_ledger()

    assert result == {}
