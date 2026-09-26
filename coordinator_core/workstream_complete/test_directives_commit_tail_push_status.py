
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.workstream_complete import directives_commit_tail as _tail


def test_declined_returns_non_failing_gate_naming_branch_policy(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("compute_push_landed_gate must not spawn a subprocess on 'declined'")

    monkeypatch.setattr(_tail.subprocess, "run", _boom)

    gate = _tail.compute_push_landed_gate(Path("/repo"), "main", push_status="declined")

    assert gate.pushed is not False
    assert "branch policy" in gate.summary_line.lower()


def test_declined_and_deferred_fields_do_not_collapse(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("must not spawn a subprocess on 'declined'/'deferred'")

    monkeypatch.setattr(_tail.subprocess, "run", _boom)

    declined_gate = _tail.compute_push_landed_gate(Path("/repo"), "main", push_status="declined")
    deferred_gate = _tail.compute_push_landed_gate(Path("/repo"), "main", push_status="deferred")

    assert declined_gate.declined is True
    assert declined_gate.deferred is False

    assert deferred_gate.deferred is True
    assert deferred_gate.declined is False


def test_declined_issues_no_git_log_probe(monkeypatch):
    calls = []

    def _spy(*args, **kwargs):
        calls.append(args)
        raise AssertionError("should not be reached")

    monkeypatch.setattr(_tail.subprocess, "run", _spy)

    _tail.compute_push_landed_gate(Path("/repo"), "main", push_status="declined")

    assert calls == []


def test_deferred_arm_unchanged(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("compute_push_landed_gate must not spawn a subprocess on 'deferred'")

    monkeypatch.setattr(_tail.subprocess, "run", _boom)

    gate = _tail.compute_push_landed_gate(Path("/repo"), "main", push_status="deferred")

    assert gate.pushed is None
    assert gate.deferred is True
    assert "deferred" in gate.summary_line.lower()


def test_normal_status_still_probes(monkeypatch):
    calls = []

    class _Proc:
        returncode = 0
        stdout = ""

    def _spy(args, **kwargs):
        calls.append(args)
        return _Proc()

    monkeypatch.setattr(_tail.subprocess, "run", _spy)

    gate = _tail.compute_push_landed_gate(Path("/repo"), "main", push_status="pushed")

    assert len(calls) == 1
    assert "log" in calls[0]
    assert gate.pushed is True
# regime later: under DR-329 a close commit is made at PUSH_MODE_NEVER and


def test_cadence_pending_issues_no_git_log_probe(monkeypatch):
    calls = []

    def _spy(*args, **kwargs):
        calls.append(args)
        raise AssertionError("must not probe origin on 'cadence-pending'")

    monkeypatch.setattr(_tail.subprocess, "run", _spy)

    gate = _tail.compute_push_landed_gate(Path("/repo"), "main", push_status="cadence-pending")

    assert calls == []
    assert gate.pushed is not False
    assert "cadence-pending" in gate.summary_line.lower()


def test_cadence_pending_collapses_into_neither_deferred_nor_declined(monkeypatch):

    def _boom(*args, **kwargs):
        raise AssertionError("no arm under test may probe origin")

    monkeypatch.setattr(_tail.subprocess, "run", _boom)

    cadence = _tail.compute_push_landed_gate(Path("/repo"), "main", push_status="cadence-pending")
    declined = _tail.compute_push_landed_gate(Path("/repo"), "main", push_status="declined")
    deferred = _tail.compute_push_landed_gate(Path("/repo"), "main", push_status="deferred")

    assert cadence.cadence_pending is True
    assert cadence.deferred is False
    assert cadence.declined is False

    assert declined.cadence_pending is False
    assert deferred.cadence_pending is False

    assert "in flight" in deferred.summary_line.lower()
    assert "nothing in flight" in cadence.summary_line.lower()


def test_not_attempted_still_probes_and_is_never_read_as_cadence_pending(monkeypatch):
    """`derive_push_status` reports "not-attempted" for every push that
    never happened, INCLUDING ones nothing will ever publish. Promoting it
    to the cadence arm here would pass those silently, so the probe must
    still run and the arm must key on the richer member only."""
    calls = []

    class _Proc:
        returncode = 0
        stdout = "abc123" + chr(10)

    def _spy(args, **kwargs):
        calls.append(args)
        return _Proc()

    monkeypatch.setattr(_tail.subprocess, "run", _spy)

    gate = _tail.compute_push_landed_gate(Path("/repo"), "main", push_status="not-attempted")

    assert len(calls) == 1
    assert gate.cadence_pending is False
    assert gate.pushed is False
