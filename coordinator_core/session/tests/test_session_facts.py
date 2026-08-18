"""Contract test for `coordinator_core.session.session_facts` — DR-319's return-shape
contract (docs/decisions/DR-319-session-fact-facade-shape-and-failure-posture.md).

This is what discharges AC3, AC6, and AC8
(docs/plans/2026-08-18-session-fact-facade-and-failure-posture.md § C2): a TEST that the
posture holds, not a document describing it (the Staff Engineer F9/F10).

Git failure is simulated by monkeypatching the producer seam
(`branch_resolution._git_run`), not by requiring a broken repo — a broken git repo is a
flaky, environment-dependent way to exercise a code path that is really "the subprocess
call returned nonzero," which is trivially and deterministically faked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import branch_resolution
from coordinator_core.session import session_facts

_SID = "sess-c2-contract-test-001"


def _fake_result(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_computed_record_is_distinguishable_from_a_degraded_record(tmp_path, monkeypatch):
    """The whole point of the posture: a degraded read and a genuinely-zero read must
    not collapse into the same value at the call site."""
    monkeypatch.setattr(
        branch_resolution,
        "_git_run",
        lambda args, cwd: _fake_result(0, stdout=""),
    )
    zero_record = session_facts.session_magnitude_attributed(tmp_path, _SID)

    monkeypatch.setattr(
        branch_resolution,
        "_git_run",
        lambda args, cwd: _fake_result(128, stderr="fatal: not a git repository"),
    )
    degraded_record = session_facts.session_magnitude_attributed(tmp_path, _SID)

    assert zero_record["degraded"] is False
    assert zero_record["value"] == 0
    assert degraded_record["degraded"] is True
    assert "value" not in degraded_record

    # The distinguishing property under test: these are not the same record, and a
    # caller cannot mistake one for the other by reading `.get("value")` alone.
    assert zero_record != degraded_record
    assert degraded_record.get("value") is None


def test_computed_record_carries_every_required_key_never_a_fabricated_default(tmp_path, monkeypatch):
    """A silently-missing field fails loudly: assert every DR-319 key is actually
    present, not merely that `.get()` returns something plausible."""
    monkeypatch.setattr(
        branch_resolution,
        "_git_run",
        lambda args, cwd: _fake_result(0, stdout="abc123 one\ndef456 two\n"),
    )
    record = session_facts.session_magnitude_attributed(tmp_path, _SID)

    for key in ("degraded", "value", "source", "collision"):
        assert key in record, f"computed record missing required key {key!r}"
    assert record["degraded"] is False
    assert record["value"] == 2
    assert isinstance(record["source"], str) and record["source"]
    # collision is ALWAYS present on a computed record (R-11) — None here because this
    # fact has no peer-mutable surface, never coerced to False.
    assert record["collision"] is None


def test_collision_key_is_present_unconditionally_on_a_computed_record(tmp_path, monkeypatch):
    """`"collision" in record` must hold unconditionally on a computed record — an
    omitted key declares nothing, which is the absent-vs-clean conflation R-11
    forbids."""
    monkeypatch.setattr(
        branch_resolution,
        "_git_run",
        lambda args, cwd: _fake_result(0, stdout=""),
    )
    record = session_facts.session_magnitude_attributed(tmp_path, _SID)
    assert "collision" in record
    assert record["collision"] is None
    # Never coerced to False (R-11 negative-spec) — a bare truthiness/`is False` check
    # would wrongly treat "no collision mode exists" as "checked, clean."
    assert record["collision"] is not False


def test_degraded_record_carries_evidence_and_source_never_a_value_key(tmp_path, monkeypatch):
    monkeypatch.setattr(
        branch_resolution,
        "_git_run",
        lambda args, cwd: _fake_result(1, stderr="git log failed"),
    )
    record = session_facts.session_magnitude_attributed(tmp_path, _SID)

    assert record["degraded"] is True
    assert "value" not in record
    assert isinstance(record["evidence"], str) and record["evidence"]
    assert isinstance(record["source"], str) and record["source"]
    assert set(record) == {"degraded", "evidence", "source"}


def test_served_fact_carries_no_verdict_field(tmp_path, monkeypatch):
    """AC8, the detect/decide split: neither shape may carry a verdict, recommendation,
    disposition, or action key — this facade emits evidence and collision state, never
    a decision replacing an EM judgment."""
    forbidden = {"verdict", "recommendation", "disposition", "action"}

    monkeypatch.setattr(
        branch_resolution,
        "_git_run",
        lambda args, cwd: _fake_result(0, stdout="abc123 one\n"),
    )
    computed = session_facts.session_magnitude_attributed(tmp_path, _SID)
    assert forbidden.isdisjoint(computed), f"computed record leaked a verdict key: {set(computed) & forbidden}"

    monkeypatch.setattr(
        branch_resolution,
        "_git_run",
        lambda args, cwd: _fake_result(1, stderr="boom"),
    )
    degraded = session_facts.session_magnitude_attributed(tmp_path, _SID)
    assert forbidden.isdisjoint(degraded), f"degraded record leaked a verdict key: {set(degraded) & forbidden}"


def test_computed_shape_is_exactly_the_dr319_key_set(tmp_path, monkeypatch):
    """No shape other than DR-319's two is legal, regardless of internal consistency."""
    monkeypatch.setattr(
        branch_resolution,
        "_git_run",
        lambda args, cwd: _fake_result(0, stdout="abc123 one\n"),
    )
    record = session_facts.session_magnitude_attributed(tmp_path, _SID)
    assert set(record) == {"degraded", "value", "source", "collision"}
