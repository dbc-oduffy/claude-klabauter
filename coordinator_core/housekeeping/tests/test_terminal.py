"""
Tests for coordinator_core.housekeeping.terminal — Step D, the terminal set
computed from memory with no second corpus walk (plan chunk C6b).

Covers: exactly which deployment states are terminal (including the
counter-intuitive `continued` case); the two retention grounds — live
children is NOT one (PM ruling 2026-08-28), a live claim holder IS; `cap`
enforcement, including the absent/zero/negative setup-error cases; and
that the whole computation performs zero file I/O, asserted by a read
count rather than merely a duration.

Spec backlink: docs/plans/2026-08-29-the-housekeeping-cycle-stops-committing.md
  § C6b; docs/research/2026-08-29-housekeeping-v2-target-shape.md § 2 step D.

Negative-spec: this file does not test C3's corpus read, C4's archive
index, or C6's gate-clear mechanics — only what `compute_terminal_set`
does with the records dict and predicate it is handed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from coordinator_core.housekeeping.terminal import (
    TERMINAL_DEPLOYMENT_STATES,
    TerminalEntry,
    TerminalSetCapError,
    compute_terminal_set,
)


def _record(deployment_state: str, **extra: Any) -> Dict[str, Any]:
    rec = {"handoff_id": "hnd-x", "deployment_state": deployment_state}
    rec.update(extra)
    return rec


# ---------------------------------------------------------------------------
# Terminal-state membership
# ---------------------------------------------------------------------------


def test_terminal_states_are_exactly_closed_abandoned_continued_shipped():
    assert TERMINAL_DEPLOYMENT_STATES == frozenset(
        {"closed", "abandoned", "continued", "shipped"}
    )


@pytest.mark.parametrize("state", ["closed", "abandoned", "continued", "shipped"])
def test_each_terminal_state_is_selected(state):
    records = {Path("a.md"): _record(state)}
    entries = compute_terminal_set(records, cap=10)
    assert [e.path for e in entries] == [Path("a.md")]


def test_continued_is_terminal_not_retained():
    """`continued` IS terminal — a record with a successor is finished,
    not retained. Counter-intuitive enough to deserve its own test
    (plan chunk C6b body, verbatim)."""
    records = {Path("continued.md"): _record("continued")}
    entries = compute_terminal_set(records, cap=10)
    assert len(entries) == 1
    assert entries[0].record["deployment_state"] == "continued"


@pytest.mark.parametrize(
    "state", ["awaiting_gate", "ready_to_fire", "in_progress", "open", "blocked"]
)
def test_non_terminal_states_are_excluded(state):
    records = {Path("a.md"): _record(state)}
    entries = compute_terminal_set(records, cap=10)
    assert entries == []


# ---------------------------------------------------------------------------
# Retention grounds — children is NOT one, live claim holder IS
# ---------------------------------------------------------------------------


def test_record_with_children_is_still_archivable():
    """No live-children / childlessness ground for retention (PM ruling,
    2026-08-28) — a terminal record carrying children is archivable, same
    as one without. This module never even inspects a children field."""
    records = {
        Path("has-children.md"): _record("closed", children=["hnd-y", "hnd-z"]),
    }
    entries = compute_terminal_set(records, cap=10)
    assert [e.path for e in entries] == [Path("has-children.md")]


def test_record_with_live_claim_holder_is_not_archivable():
    """A live claim holder IS a still-valid retention ground — a terminal
    record currently held by a live claim is excluded from the terminal
    set."""
    held_path = Path("held.md")
    free_path = Path("free.md")
    records = {
        held_path: _record("closed", claim_holder="peer-session"),
        free_path: _record("closed"),
    }

    def claim_holder_live(path: Path, record: Dict[str, Any]) -> bool:
        return path == held_path

    entries = compute_terminal_set(records, cap=10, claim_holder_live=claim_holder_live)
    assert [e.path for e in entries] == [free_path]


def test_no_claim_holder_predicate_supplied_defaults_to_no_retention():
    records = {Path("a.md"): _record("closed")}
    entries = compute_terminal_set(records, cap=10)
    assert [e.path for e in entries] == [Path("a.md")]


# ---------------------------------------------------------------------------
# `cap` — required and positive
# ---------------------------------------------------------------------------


def test_cap_absent_raises():
    with pytest.raises(TypeError):
        compute_terminal_set({Path("a.md"): _record("closed")})  # type: ignore[call-arg]


def test_cap_zero_raises_setup_error_not_full_sweep():
    with pytest.raises(TerminalSetCapError):
        compute_terminal_set({Path("a.md"): _record("closed")}, cap=0)


def test_cap_negative_raises_setup_error():
    with pytest.raises(TerminalSetCapError):
        compute_terminal_set({Path("a.md"): _record("closed")}, cap=-1)


def test_cap_bounds_the_returned_set():
    records = {
        Path(f"r{i}.md"): _record("closed") for i in range(5)
    }
    entries = compute_terminal_set(records, cap=2)
    assert len(entries) == 2


# ---------------------------------------------------------------------------
# Zero I/O — a read-count assertion, never merely a duration
# ---------------------------------------------------------------------------


def test_compute_terminal_set_performs_zero_file_io(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("compute_terminal_set must not touch disk")

    monkeypatch.setattr(Path, "read_text", _boom)
    monkeypatch.setattr(Path, "read_bytes", _boom)
    monkeypatch.setattr("builtins.open", _boom)

    records = {
        Path("a.md"): _record("closed"),
        Path("b.md"): _record("awaiting_gate"),
        Path("c.md"): _record("continued", children=["x"]),
    }
    entries = compute_terminal_set(records, cap=10)
    assert {e.path for e in entries} == {Path("a.md"), Path("c.md")}


def test_terminal_entry_carries_the_record_used_to_select_it():
    rec = _record("shipped", shipped_in="abc123")
    entries = compute_terminal_set({Path("a.md"): rec}, cap=10)
    assert entries[0] == TerminalEntry(path=Path("a.md"), record=rec)
