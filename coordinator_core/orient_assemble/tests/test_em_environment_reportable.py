
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
    monkeypatch.setenv("HOME", str(tmp_path / "does-not-exist"))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.setattr(rco, "_resolve_effort", lambda proj, uc: ("high", "project"))
    monkeypatch.setattr(rco, "_resolve_transcript", lambda a, b, c: "some-transcript")
    monkeypatch.setattr(rco, "_latest_model", lambda p: "claude-sonnet-5")


def _quiet_everything_else(monkeypatch):
    monkeypatch.setattr(rco, "_scan_addon_health_run", lambda mode: ([], 0))
    monkeypatch.setattr(rco, "_read_memo_surface", lambda mode, *, repo_root=None: ReaderResult())
    monkeypatch.setattr(rco, "_read_rag_staleness", lambda: ReaderResult())
    monkeypatch.setattr(rco, "_read_worktree_sweep", lambda *, repo_root=None: ReaderResult())

    monkeypatch.setattr(rht, "collect", lambda cadence, *, repo_root=None: ReaderResult())
    monkeypatch.setattr(rbr, "collect", lambda cadence, *, repo_root=None: ReaderResult())
    monkeypatch.setattr(rhr, "collect", lambda cadence, *, repo_root=None: ReaderResult())


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
        lambda *, repo_root=None: ReaderResult(directives=[fake_directive]),
    )

    envelope = brief("day")

    judgment_point_ids = {jp["id"] for jp in envelope["judgment_points"]}
    assert "j-em-env-effort" in judgment_point_ids
    assert "j-em-env-model" not in judgment_point_ids
