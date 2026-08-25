"""
coordinator_core.orient_assemble.tests.test_em_environment_reportable — C5
AC: `j-em-env-effort`/`j-em-env-model` (readers_clean_ops::_read_em_environment)
name no directive, so `partition_reportable` (C1,
`contract/decision_object/judgment.py`) classifies them `reported` at the
`brief()` envelope seam in `coordinator_core/orient_assemble/__init__.py` —
never inside `readers_clean_ops`, which only ever sees its own directives.

Load-bearing note (why this chunk exists on its own): both points emit
ONLY when EM effort is off `medium` OR the model is off Opus. A sweep run
under default/ambient session conditions observes zero of them and would
wrongly conclude they do not exist — exactly the under-count the plan
names as the defect this chunk fixes. This test therefore FORCES both
drift conditions explicitly (non-medium effort AND non-Opus model) rather
than relying on whatever the ambient test-runner session happens to be.

Spec backlink: docs/plans/2026-08-15-judgment-points-that-gate-nothing-stop-being-questions.md, chunk C5
"""

from __future__ import annotations

from coordinator_core.orient_assemble import brief
from coordinator_core.orient_assemble import (
    readers_branch_reconcile as rbr,
    readers_clean_ops as rco,
    readers_handoff_triage as rht,
    readers_health_reaper as rhr,
)
from coordinator_core.orient_assemble.readers_clean_ops import ReaderResult


def _force_em_environment_drift(monkeypatch, tmp_path):
    """Force BOTH drift conditions explicitly — non-medium effort AND a
    non-Opus model — so the two `j-em-env-*` points are guaranteed to
    emit regardless of the ambient session's real effort/model state."""
    monkeypatch.setenv("HOME", str(tmp_path / "does-not-exist"))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.setattr(rco, "_resolve_effort", lambda proj, uc: ("high", "project"))
    monkeypatch.setattr(rco, "_resolve_transcript", lambda a, b, c: "some-transcript")
    monkeypatch.setattr(rco, "_latest_model", lambda p: "claude-sonnet-5")


def _quiet_everything_else(monkeypatch):
    """Silence every OTHER `_read_*` probe in readers_clean_ops, and every
    other reader family's `collect`, so the only judgment points in the
    envelope are the two forced `j-em-env-*` points — isolates the
    partition behavior under test from unrelated ambient findings."""
    monkeypatch.setattr(rco, "_scan_addon_health_run", lambda mode: ([], 0))
    monkeypatch.setattr(rco, "_read_memo_surface", lambda mode: ReaderResult())
    monkeypatch.setattr(rco, "_read_rag_staleness", lambda: ReaderResult())
    monkeypatch.setattr(rco, "_read_worktree_sweep", lambda: ReaderResult())

    monkeypatch.setattr(rht, "collect", lambda cadence: ReaderResult())
    monkeypatch.setattr(rbr, "collect", lambda cadence: ReaderResult())
    monkeypatch.setattr(rhr, "collect", lambda cadence: ReaderResult())


def test_em_environment_points_are_reported_not_asked_when_both_conditions_drift(
    monkeypatch, tmp_path
):
    _force_em_environment_drift(monkeypatch, tmp_path)
    _quiet_everything_else(monkeypatch)

    envelope = brief("day")

    judgment_point_ids = {jp["id"] for jp in envelope["judgment_points"]}
    assert "j-em-env-effort" not in judgment_point_ids
    assert "j-em-env-model" not in judgment_point_ids

    assert "EM effort is" in envelope["narration"]
    assert "pin it?" in envelope["narration"]
    assert "medium is the cost-calibrated default for EM work" in envelope["narration"]
    assert "switch?" in envelope["narration"]

    assert "Reported (gate nothing, not asked):" in envelope["narration"]


def test_em_environment_points_are_asked_not_reported_when_a_directive_names_them(
    monkeypatch, tmp_path
):
    """Control case for the predicate itself (not this chunk's main claim):
    a point stays `asked` when a directive depends on it, proving the
    demotion is genuinely conditional on the directive relationship and
    not an unconditional drop of these two ids."""
    _force_em_environment_drift(monkeypatch, tmp_path)
    _quiet_everything_else(monkeypatch)

    fake_directive = {
        "id": "d-fake-gate",
        "cli": "true",
        "args": [],
        "depends_on": "j-em-env-effort",
        "already_satisfied": False,
    }
    monkeypatch.setattr(
        rco,
        "_read_worktree_sweep",
        lambda: ReaderResult(directives=[fake_directive]),
    )

    envelope = brief("day")

    judgment_point_ids = {jp["id"] for jp in envelope["judgment_points"]}
    assert "j-em-env-effort" in judgment_point_ids
    assert "j-em-env-model" not in judgment_point_ids
