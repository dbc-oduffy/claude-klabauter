"""Acceptance battery, part 1 — core resolution semantics for
coordinator_core.ops.emit.priority_resolve.resolve_priority.

One test per ratified acceptance criterion (AC3, AC4, AC6, AC7, AC8). These
tests assert against the RATIFIED SEMANTICS frozen in priority_resolve.py's
module docstring — a test that contradicts them is wrong, not the code.

Fixture/style conventions mirror test_priority_resolve.py: plain
frontmatter'd .md files on disk (dag.walk_forward reads real files); ledger
entries injected via ``resolve_priority(..., ledger_entries=...)`` rather
than round-tripped through ``load_priority_ledger``'s disk read.

Spec backlink: coordinator-claude docs/plans/2026-07-26-priority-ledger.md § Acceptance Criteria (AC3, AC4, AC6, AC7, AC8).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.emit.priority_resolve import resolve_priority

# Review: coordinator:code-reviewer — Finding 1: _write_node/_ledger extracted to conftest.py
# (shared across the five priority-ledger test modules that used a byte-for-byte copy).
from coordinator_core.ops.emit.tests.conftest import _ledger, _write_node  # noqa: F401


@pytest.fixture()
def node_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state" / "handoffs"
    d.mkdir(parents=True)
    return d


# ---------------------------------------------------------------------------
# AC3 — a CONTINUATION inherits its predecessor priority.
# ---------------------------------------------------------------------------


def test_ac3_continuation_inherits_predecessor_priority(node_dir: Path):
    _write_node(node_dir, "PARENT.md", handoff_id="PARENT_id", predecessor=None)
    child_path = _write_node(
        node_dir, "CHILD.md", handoff_id="CHILD_id", predecessor="PARENT.md"
    )

    ledger = _ledger(PARENT_id="high")

    result = resolve_priority(str(child_path), "CHILD_id", ledger_entries=ledger)

    assert result["effective_priority"] == "high"
    assert result["origin"] == "inherited"
    assert result["source_id"] == "PARENT_id"


# ---------------------------------------------------------------------------
# AC4 — MID-CHAIN OVERRIDE wins. This is the acceptance oracle, not an
# illustration:
#     A (explicit: high)
#     +-- B (explicit: low)      <- mid-chain PM override
#         +-- C (no explicit call)
# C resolves to low, and — separately, explicitly — does NOT resolve to high.
# ---------------------------------------------------------------------------


def test_ac4_mid_chain_override_wins(node_dir: Path):
    _write_node(node_dir, "A.md", handoff_id="A_id", predecessor=None)
    _write_node(node_dir, "B.md", handoff_id="B_id", predecessor="A.md")
    c_path = _write_node(node_dir, "C.md", handoff_id="C_id", predecessor="B.md")

    ledger = _ledger(A_id="high", B_id="low")

    result = resolve_priority(str(c_path), "C_id", ledger_entries=ledger)

    assert result["effective_priority"] == "low"
    assert result["origin"] == "inherited"
    assert result["source_id"] == "B_id"


def test_ac4_mid_chain_override_does_not_resolve_to_top_of_chain(node_dir: Path):
    """The 'top of chain' reading is RETIRED from the spec — a regression to
    it must fail this assertion loudly, independent of the positive check
    above (which would also fail on a completely broken resolver)."""
    _write_node(node_dir, "A.md", handoff_id="A_id", predecessor=None)
    _write_node(node_dir, "B.md", handoff_id="B_id", predecessor="A.md")
    c_path = _write_node(node_dir, "C.md", handoff_id="C_id", predecessor="B.md")

    ledger = _ledger(A_id="high", B_id="low")

    result = resolve_priority(str(c_path), "C_id", ledger_entries=ledger)

    assert result["effective_priority"] != "high"


# ---------------------------------------------------------------------------
# AC6 — suggested_priority NEVER overrides an explicit PM call.
# ---------------------------------------------------------------------------


def test_ac6_suggested_priority_loses_to_explicit_ancestor(node_dir: Path):
    _write_node(node_dir, "A.md", handoff_id="A_id", predecessor=None)
    c_path = _write_node(
        node_dir,
        "C.md",
        handoff_id="C_id",
        predecessor="A.md",
        suggested_priority="urgent",
    )

    ledger = _ledger(A_id="medium")

    result = resolve_priority(str(c_path), "C_id", ledger_entries=ledger)

    assert result["effective_priority"] == "medium"
    assert result["origin"] == "inherited"
    assert result["source_id"] == "A_id"


def test_ac6_suggested_priority_used_when_no_explicit_ancestor_anywhere(node_dir: Path):
    d_path = _write_node(
        node_dir,
        "D.md",
        handoff_id="D_id",
        predecessor=None,
        suggested_priority="medium",
    )

    result = resolve_priority(str(d_path), "D_id", ledger_entries=_ledger())

    assert result["effective_priority"] == "medium"
    assert result["origin"] == "suggested"


# ---------------------------------------------------------------------------
# AC7 — the `none` sentinel terminates the walk and is distinguishable from
# unset (no entry at all).
# ---------------------------------------------------------------------------


def test_ac7_none_sentinel_terminates_walk_vs_absent_entry(node_dir: Path):
    # (i) A (explicit: urgent) -> B (explicit: none) -> C (nothing)
    #     C must resolve to the cleared state via B, NOT to urgent.
    _write_node(node_dir, "A.md", handoff_id="A_id", predecessor=None)
    _write_node(node_dir, "B.md", handoff_id="B_id", predecessor="A.md")
    c_path = _write_node(node_dir, "C.md", handoff_id="C_id", predecessor="B.md")

    ledger_cleared = _ledger(A_id="urgent", B_id="none")
    result_cleared = resolve_priority(str(c_path), "C_id", ledger_entries=ledger_cleared)

    assert result_cleared["effective_priority"] is None
    assert result_cleared["origin"] == "inherited"
    assert result_cleared["source_id"] == "B_id"

    # (ii) A (explicit: urgent) -> B (NO entry at all) -> C (nothing)
    #      C must resolve to urgent — absence is not a clear.
    _write_node(node_dir, "A2.md", handoff_id="A2_id", predecessor=None)
    _write_node(node_dir, "B2.md", handoff_id="B2_id", predecessor="A2.md")
    c2_path = _write_node(node_dir, "C2.md", handoff_id="C2_id", predecessor="B2.md")

    ledger_absent = _ledger(A2_id="urgent")
    result_absent = resolve_priority(str(c2_path), "C2_id", ledger_entries=ledger_absent)

    assert result_absent["effective_priority"] == "urgent"
    assert result_absent["origin"] == "inherited"
    assert result_absent["source_id"] == "A2_id"

    # The two cases must produce DIFFERENT results — a test checking only
    # (i) would pass against an implementation that treats deletion and
    # clearing alike.
    assert result_cleared["effective_priority"] != result_absent["effective_priority"]
    assert result_cleared["source_id"] != result_absent["source_id"]


# ---------------------------------------------------------------------------
# AC8 — fan-in at differing priorities yields NO value and origin
# "ambiguous"; fan-in where parents agree resolves normally (ambiguity is
# disagreement, not arity).
# ---------------------------------------------------------------------------


def test_ac8_fan_in_differing_priorities_yields_ambiguous(node_dir: Path):
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

    assert result["effective_priority"] is None
    assert result["origin"] == "ambiguous"


def test_ac8_fan_in_agreeing_priorities_resolves_normally(node_dir: Path):
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
