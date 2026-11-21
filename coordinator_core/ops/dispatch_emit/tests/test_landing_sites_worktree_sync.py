"""Landing sites surface `CommitOutcome.sync_refused` as `worktree_sync_refused` plus
`sync_incomplete: true` and never deny the landing. Real git fixture for the checkpoint stamp
site; a stand-in `commit_paths` outcome for the sites whose set-up is the whole run machinery."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.git import commit as git_commit
from coordinator_core.git.commit import CommitOutcome, sync_report

_BASE = b"".join(b"line%02d\n" % i for i in range(12))


def _outcome(refused: tuple = ()) -> CommitOutcome:
    return CommitOutcome(sha="a" * 40, staged_preferred=(), sync_refused=refused)


def test_sync_report_is_empty_when_nothing_was_refused() -> None:
    assert sync_report(_outcome()) == {}


def test_sync_report_names_refused_paths_and_flags_incomplete() -> None:
    assert sync_report(_outcome(("a.py", "b.py"))) == {
        "worktree_sync_refused": ["a.py", "b.py"], "sync_incomplete": True,
    }


def test_checkpoint_stamp_land_without_a_report_dict_still_returns_the_sha(
    tmp_path: Path, monkeypatch
) -> None:
    from coordinator_core.ops.dispatch_emit.checkpoint_stamp import _land_stamp

    monkeypatch.setattr(git_commit, "commit_paths", lambda *a, **k: _outcome(("a.py",)))
    monkeypatch.setattr(
        "coordinator_core.session.scope.release_committed_claims_or_retain", lambda *a, **k: None
    )

    assert _land_stamp(tmp_path, ["a.py"], "m", None) == "a" * 40


def test_halted_run_landed_records_commit_reports_refused_paths(tmp_path: Path, monkeypatch) -> None:
    from coordinator_core.ops.dispatch_emit import halted_run_landed, run_artifacts

    run_id = "warp-2026-01-01-x-aaaaaa-20260101T000000-bbbbbb"
    record_dir = tmp_path / "state" / "warp" / run_id
    record_dir.mkdir(parents=True)
    (record_dir / "halted.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(run_artifacts, "record_dir_residue", lambda root, rid: [])
    monkeypatch.setattr(git_commit, "commit_paths", lambda *a, **k: _outcome(("state/x.md",)))

    reply = halted_run_landed._commit_records(tmp_path, run_id, record_dir, "b" * 40)

    assert reply["records_commit"] == "a" * 40
    assert reply["worktree_sync_refused"] == ["state/x.md"]
    assert reply["sync_incomplete"] is True


def test_land_run_merge_artifacts_commit_reports_refused_paths(tmp_path: Path, monkeypatch) -> None:
    from coordinator_core.ops.dispatch_emit import land_run

    monkeypatch.setattr(git_commit, "commit_paths", lambda *a, **k: _outcome(("r.json",)))

    reply = land_run._land_merge_artifacts(
        tmp_path, {"paths": ["r.json"], "deleted": []}, "subject", "c" * 40
    )

    assert reply["committed"] is True
    assert reply["worktree_sync_refused"] == ["r.json"]
    assert reply["sync_incomplete"] is True
