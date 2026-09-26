
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


def test_record_with_children_is_still_archivable():
    records = {
        Path("has-children.md"): _record("closed", children=["hnd-y", "hnd-z"]),
    }
    entries = compute_terminal_set(records, cap=10)
    assert [e.path for e in entries] == [Path("has-children.md")]


def test_no_claim_holder_predicate_supplied_defaults_to_no_retention():
    records = {Path("a.md"): _record("closed")}
    entries = compute_terminal_set(records, cap=10)
    assert [e.path for e in entries] == [Path("a.md")]


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


class TestRetainedHook:
    def test_a_retained_record_is_excluded_from_the_terminal_set(self) -> None:
        keep = Path("state/handoffs/dirty.md")
        take = Path("state/handoffs/clean.md")
        records = {
            keep: _record("shipped"),
            take: _record("shipped"),
        }

        entries = compute_terminal_set(
            records, cap=10, retained=lambda path, record: path == keep
        )

        assert [e.path for e in entries] == [take]

    def test_retention_is_applied_before_cap_slots(self) -> None:
        retained_path = Path("state/handoffs/a-retained.md")
        records = {
            retained_path: _record("shipped"),
            Path("state/handoffs/b.md"): _record("shipped"),
            Path("state/handoffs/c.md"): _record("shipped"),
        }

        entries = compute_terminal_set(
            records, cap=2, retained=lambda path, record: path == retained_path
        )

        assert len(entries) == 2
        assert retained_path not in [e.path for e in entries]

    def test_no_retained_predicate_retains_nothing(self) -> None:
        records = {Path("state/handoffs/x.md"): _record("shipped")}

        entries = compute_terminal_set(records, cap=10, retained=None)

        assert len(entries) == 1

