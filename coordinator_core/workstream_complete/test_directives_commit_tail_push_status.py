"""test_directives_commit_tail_push_status — wire-path suite for
`directives_commit_tail.compute_push_landed_gate`'s `push_status` arm
selection.

Spec backlink: docs/plans/2026-08-08-the-push-leg-that-never-asked-which-
branch.md, chunk C6c / AC13. Pins the `"declined"` arm added to close AC5's
exact defect (a network probe plus a wrong "unpushed" verdict on a
deliberate decline) reproduced a second time in this module: prior to this
chunk, `compute_push_landed_gate` short-circuited only on
`push_status == "deferred"`, so `"declined"` fell through to the
`git log origin/<branch>..HEAD` probe.

Run: python3 -m pytest coordinator_core/workstream_complete/test_directives_commit_tail_push_status.py -q -p no:randomly
"""

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
