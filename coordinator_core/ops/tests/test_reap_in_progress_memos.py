"""
Tests for coordinator_core.ops.reap_in_progress_memos — the return-data
survey of cross-repo memos stranded at `status: in_progress` whose claiming
session is no longer live (C6 of
docs/plans/2026-09-11-handoff-lifecycle-one-legal-state-table.md).

Every memo is built under `tmp_path`, never taken from a real record.
Liveness is monkeypatched at `session_live`/`session_verdict`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops import reap_in_progress_memos as mod


def _write_memo(memo_dir, name, *, status, picked_up_by=None):
    lines = ["---", 'title: "test memo"', f"status: {status}"]
    if picked_up_by is not None:
        lines.append(f"picked_up_by: {picked_up_by}")
    lines.append("---")
    lines.append("body")
    path = Path(memo_dir) / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def repo_root(tmp_path):
    (tmp_path / "state" / "cross-repo" / "inbox").mkdir(parents=True)
    (tmp_path / "state" / "cross-repo" / "outbox").mkdir(parents=True)
    (tmp_path / "state" / "cross-repo" / "archive").mkdir(parents=True)
    return tmp_path


def test_dead_holder_gives_one_release_and_names_the_deciding_arm(monkeypatch, repo_root):
    _write_memo(
        repo_root / "state/cross-repo/inbox", "a.md",
        status="in_progress", picked_up_by="dead-session",
    )
    monkeypatch.setattr(mod, "session_live", lambda sid, cwd=None: False)
    monkeypatch.setattr(mod, "session_verdict", lambda sid, cwd=None: ("verdict", "layer1-dead"))

    result = mod.survey(repo_root)

    assert result.would_release == 1
    assert len(result.dispositions) == 1
    d = result.dispositions[0]
    assert d.verdict == mod._VERDICT_RELEASE
    assert d.holder == "dead-session"
    assert "layer1-dead" in d.detail


def test_live_holder_gives_no_release(monkeypatch, repo_root):
    """Load-bearing: the case the 2026-08-22 handoff-reaper bug got wrong."""
    _write_memo(
        repo_root / "state/cross-repo/inbox", "a.md",
        status="in_progress", picked_up_by="live-session",
    )
    monkeypatch.setattr(mod, "session_live", lambda sid, cwd=None: True)

    def _boom(*a, **k):
        raise AssertionError("session_verdict must not be called for a live holder")

    monkeypatch.setattr(mod, "session_verdict", _boom)

    result = mod.survey(repo_root)

    assert result.would_release == 0
    assert result.dispositions == []


def test_empty_picked_up_by_gives_a_skip_not_a_release(monkeypatch, repo_root):
    _write_memo(repo_root / "state/cross-repo/inbox", "a.md", status="in_progress")
    monkeypatch.setattr(mod, "session_live", lambda sid, cwd=None: (_ for _ in ()).throw(
        AssertionError("must not check liveness with no holder")))

    result = mod.survey(repo_root)

    assert result.would_release == 0
    assert len(result.dispositions) == 1
    assert result.dispositions[0].verdict == mod._VERDICT_SKIP_EMPTY_HOLDER


@pytest.mark.parametrize("status", ["open", "actioned", "superseded", "draft"])
def test_non_in_progress_statuses_are_never_candidates(monkeypatch, repo_root, status):
    _write_memo(
        repo_root / "state/cross-repo/inbox", "a.md",
        status=status, picked_up_by="whoever",
    )
    monkeypatch.setattr(mod, "session_live", lambda sid, cwd=None: (_ for _ in ()).throw(
        AssertionError("must not check liveness for a non-in_progress memo")))

    result = mod.survey(repo_root)

    assert result.would_release == 0
    assert result.dispositions == []


def test_archived_in_progress_memo_is_never_read(monkeypatch, repo_root):
    _write_memo(
        repo_root / "state/cross-repo/archive", "a.md",
        status="in_progress", picked_up_by="dead-session",
    )
    monkeypatch.setattr(mod, "session_live", lambda sid, cwd=None: (_ for _ in ()).throw(
        AssertionError("archive must never be walked")))

    result = mod.survey(repo_root)

    assert result.would_release == 0
    assert result.dispositions == []


def test_apply_dispositions_calls_release_once_per_release_only(monkeypatch):
    calls = []

    def fake_release(path, return_result=False):
        calls.append(path)
        assert return_result is True
        return {"exit_code": 0}

    monkeypatch.setattr(mod, "cs_release_memo_revert", fake_release)

    dispositions = [
        mod.Disposition("state/cross-repo/inbox/a.md", "dead1", mod._VERDICT_RELEASE, "d"),
        mod.Disposition("state/cross-repo/inbox/b.md", "", mod._VERDICT_SKIP_EMPTY_HOLDER, "d"),
    ]
    applied, failed = mod.apply_dispositions(dispositions)

    assert calls == ["state/cross-repo/inbox/a.md"]
    assert applied == ["state/cross-repo/inbox/a.md"]
    assert failed == []


def test_apply_dispositions_reports_a_failed_release_rather_than_swallowing_it(monkeypatch):
    def fake_release(path, return_result=False):
        return {"exit_code": 1, "error": "boom"}

    monkeypatch.setattr(mod, "cs_release_memo_revert", fake_release)

    dispositions = [
        mod.Disposition("state/cross-repo/inbox/a.md", "dead1", mod._VERDICT_RELEASE, "d"),
    ]
    applied, failed = mod.apply_dispositions(dispositions)

    assert applied == []
    assert len(failed) == 1
    assert "boom" in failed[0]


def test_zero_candidates_spawns_no_subprocess(repo_root):
    """Zero `in_progress` candidates in the whole corpus. `survey()` never
    imports `subprocess` at all (module-level absence, asserted directly),
    so there is nothing to patch-and-catch a spawn with."""
    _write_memo(
        repo_root / "state/cross-repo/inbox", "a.md",
        status="open", picked_up_by="whoever",
    )

    assert not hasattr(mod, "subprocess")

    result = mod.survey(repo_root)
    assert result.would_release == 0
