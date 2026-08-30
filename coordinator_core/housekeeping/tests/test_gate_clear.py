"""
Tests for coordinator_core.housekeeping.gate_clear — Step C, gate clearing
under a lock that touches exactly one file (plan chunk C6).

Covers: gate evaluation resolving ONLY through the caller-supplied resolver
(zero file reads of its own, asserted by a call-counting stub, not merely
stated); the pure in-memory frontmatter computation; a clean lock-and-write
CLEARED path; a genuine race producing CONFLICT (mutating the file between
the caller's pre-lock read and the lock acquisition); and the in-memory
record update that never re-reads disk.

Spec backlink: docs/plans/2026-08-29-the-housekeeping-cycle-stops-committing.md
  § C6.

Negative-spec: this file does not test C5's resolver internals
(test_resolve.py owns that) or C3/C4's corpus/index mechanics — only what
this module does with a resolver it is handed, and the lock mechanics of
its own `apply_gate_clear`.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict

import pytest

from coordinator_core.housekeeping.gate_clear import (
    CLEARED,
    CONFLICT,
    GateClearError,
    GateVerdict,
    apply_gate_clear,
    compute_cleared_frontmatter,
    evaluate_gate_clear,
    record_after_clear,
)
from coordinator_core.housekeeping.resolve import BlockerState


def _write(path: Path, fields: Dict[str, Any], body: str = "body\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for key, value in fields.items():
        if value is None:
            continue
        lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# evaluate_gate_clear — zero I/O of its own, resolves ONLY through `resolve`
# ---------------------------------------------------------------------------


def test_evaluate_gate_clear_calls_resolver_exactly_once_and_performs_no_io(tmp_path, monkeypatch):
    calls = []

    def resolve(blocker_id: str) -> BlockerState:
        calls.append(blocker_id)
        return BlockerState(
            deployment_state="shipped", closed_reason=None, continued_into=None, resolved=True
        )

    # INVARIANT: gate evaluation performs zero file reads of its own — patch
    # Path.read_text/read_bytes to explode if this function ever reaches disk
    # directly instead of going through `resolve`.
    def _boom(*args, **kwargs):
        raise AssertionError("evaluate_gate_clear must not touch disk directly")

    monkeypatch.setattr(Path, "read_text", _boom)
    monkeypatch.setattr(Path, "read_bytes", _boom)

    record = {"handoff_id": "hnd-a", "deployment_state": "awaiting_gate", "blocked_by": ["hnd-b"]}
    verdict = evaluate_gate_clear(record, resolve)

    assert calls == ["hnd-b"]
    assert verdict == GateVerdict(clears=True, blocker_id="hnd-b", resolved_deployment_state="shipped")


def test_evaluate_gate_clear_non_terminal_blocker_does_not_clear():
    def resolve(blocker_id: str) -> BlockerState:
        return BlockerState(
            deployment_state="in_flight", closed_reason=None, continued_into=None, resolved=True
        )

    record = {"blocked_by": ["hnd-b"]}
    verdict = evaluate_gate_clear(record, resolve)
    assert verdict.clears is False
    assert verdict.resolved_deployment_state == "in_flight"


def test_evaluate_gate_clear_unresolved_blocker_does_not_clear():
    def resolve(blocker_id: str) -> BlockerState:
        return BlockerState(deployment_state=None, closed_reason=None, continued_into=None, resolved=False)

    record = {"blocked_by": ["hnd-b"]}
    verdict = evaluate_gate_clear(record, resolve)
    assert verdict.clears is False


def test_evaluate_gate_clear_missing_blocker_id_never_calls_resolver():
    def resolve(blocker_id: str) -> BlockerState:
        raise AssertionError("must not be called when blocked_by is absent")

    verdict = evaluate_gate_clear({"deployment_state": "awaiting_gate"}, resolve)
    assert verdict.clears is False
    assert verdict.blocker_id is None


def test_evaluate_gate_clear_empty_blocked_by_list_never_calls_resolver():
    def resolve(blocker_id: str) -> BlockerState:
        raise AssertionError("must not be called when blocked_by is empty")

    verdict = evaluate_gate_clear({"deployment_state": "awaiting_gate", "blocked_by": []}, resolve)
    assert verdict.clears is False
    assert verdict.blocker_id is None


@pytest.mark.parametrize("terminal_state", ["closed", "abandoned", "continued", "shipped"])
def test_every_terminal_state_clears(terminal_state):
    def resolve(blocker_id: str) -> BlockerState:
        return BlockerState(
            deployment_state=terminal_state, closed_reason=None, continued_into=None, resolved=True
        )

    verdict = evaluate_gate_clear({"blocked_by": ["hnd-b"]}, resolve)
    assert verdict.clears is True


def test_evaluate_gate_clear_two_blockers_both_terminal_clears():
    def resolve(blocker_id: str) -> BlockerState:
        return BlockerState(
            deployment_state="shipped", closed_reason=None, continued_into=None, resolved=True
        )

    verdict = evaluate_gate_clear({"blocked_by": ["hnd-b1", "hnd-b2"]}, resolve)
    assert verdict.clears is True


def test_evaluate_gate_clear_two_blockers_one_non_terminal_does_not_clear():
    def resolve(blocker_id: str) -> BlockerState:
        state = "shipped" if blocker_id == "hnd-b1" else "in_flight"
        return BlockerState(
            deployment_state=state, closed_reason=None, continued_into=None, resolved=True
        )

    verdict = evaluate_gate_clear({"blocked_by": ["hnd-b1", "hnd-b2"]}, resolve)
    assert verdict.clears is False
    assert verdict.blocker_id == "hnd-b2"
    assert verdict.resolved_deployment_state == "in_flight"


# ---------------------------------------------------------------------------
# compute_cleared_frontmatter — pure, in-memory
# ---------------------------------------------------------------------------


def test_compute_cleared_frontmatter_flips_state_and_strips_blocker():
    old_text = (
        "---\n"
        "handoff_id: hnd-a\n"
        "deployment_state: awaiting_gate\n"
        "blocked_by: [hnd-b]\n"
        "---\n"
        "\n"
        "body text\n"
    )
    new_text = compute_cleared_frontmatter(old_text)
    assert "deployment_state: ready_to_fire" in new_text
    assert "blocked_by" not in new_text
    assert "handoff_id: hnd-a" in new_text
    assert new_text.endswith("body text\n")


def test_compute_cleared_frontmatter_rejects_non_awaiting_gate_state():
    old_text = "---\ndeployment_state: ready_to_fire\n---\n\nbody\n"
    with pytest.raises(GateClearError):
        compute_cleared_frontmatter(old_text)


def test_compute_cleared_frontmatter_rejects_unparseable_frontmatter():
    with pytest.raises(GateClearError):
        compute_cleared_frontmatter("no frontmatter here\n")


# ---------------------------------------------------------------------------
# apply_gate_clear — the lock, CLEARED path
# ---------------------------------------------------------------------------


def test_apply_gate_clear_lands_the_write(tmp_path):
    repo_root = tmp_path
    (repo_root / ".git").mkdir()
    target = repo_root / "state" / "handoffs" / "a.md"
    _write(target, {"handoff_id": "hnd-a", "deployment_state": "awaiting_gate", "blocked_by": ["hnd-b"]})

    result = apply_gate_clear(target, repo_root)

    assert result.status == CLEARED
    on_disk = target.read_text(encoding="utf-8")
    assert "deployment_state: ready_to_fire" in on_disk
    assert "blocked_by" not in on_disk
    assert on_disk == result.new_text


def test_apply_gate_clear_accepts_caller_supplied_old_text(tmp_path):
    repo_root = tmp_path
    (repo_root / ".git").mkdir()
    target = repo_root / "state" / "handoffs" / "a.md"
    _write(target, {"handoff_id": "hnd-a", "deployment_state": "awaiting_gate", "blocked_by": ["hnd-b"]})
    old_text = target.read_text(encoding="utf-8")

    result = apply_gate_clear(target, repo_root, old_text=old_text)

    assert result.status == CLEARED


# ---------------------------------------------------------------------------
# apply_gate_clear — the lock, CONFLICT path (a genuine race)
# ---------------------------------------------------------------------------


def test_apply_gate_clear_reports_conflict_when_file_moves_between_read_and_lock(tmp_path):
    repo_root = tmp_path
    (repo_root / ".git").mkdir()
    target = repo_root / "state" / "handoffs" / "a.md"
    _write(target, {"handoff_id": "hnd-a", "deployment_state": "awaiting_gate", "blocked_by": ["hnd-b"]})

    # The caller's pre-lock read, exactly as apply_gate_clear would do it.
    old_text = target.read_text(encoding="utf-8")

    # Someone else moves the file (a peer's write) AFTER the caller's read,
    # BEFORE apply_gate_clear acquires the lock -- the actual race, not a
    # mocked-out shortcut.
    _write(target, {"handoff_id": "hnd-a", "deployment_state": "awaiting_gate", "blocked_by": ["hnd-c"]})

    result = apply_gate_clear(target, repo_root, old_text=old_text)

    assert result.status == CONFLICT
    assert result.new_text is None
    # No silent overwrite: the peer's write survives untouched.
    on_disk = target.read_text(encoding="utf-8")
    assert "blocked_by: ['hnd-c']" in on_disk


def test_apply_gate_clear_race_under_concurrent_threads_never_double_applies(tmp_path):
    """A real race, not a mocked one: two threads both read the same
    pre-lock old_text and both call apply_gate_clear concurrently. Exactly
    one must land CLEARED; the other must see CONFLICT (the first thread's
    write moved the file out from under it) -- never two silent writes."""
    repo_root = tmp_path
    (repo_root / ".git").mkdir()
    target = repo_root / "state" / "handoffs" / "a.md"
    _write(target, {"handoff_id": "hnd-a", "deployment_state": "awaiting_gate", "blocked_by": ["hnd-b"]})
    old_text = target.read_text(encoding="utf-8")

    results = []

    def worker():
        results.append(apply_gate_clear(target, repo_root, old_text=old_text).status)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [CLEARED, CONFLICT]


# ---------------------------------------------------------------------------
# record_after_clear — in-memory update, no re-read
# ---------------------------------------------------------------------------


def test_record_after_clear_updates_in_memory_without_reading_disk(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("record_after_clear must not touch disk")

    monkeypatch.setattr(Path, "read_text", _boom)
    monkeypatch.setattr(Path, "read_bytes", _boom)

    record = {"handoff_id": "hnd-a", "deployment_state": "awaiting_gate", "blocked_by": ["hnd-b"]}
    updated = record_after_clear(record)

    assert updated["deployment_state"] == "ready_to_fire"
    assert "blocked_by" not in updated
    # Original left untouched.
    assert record["deployment_state"] == "awaiting_gate"
    assert record["blocked_by"] == ["hnd-b"]
