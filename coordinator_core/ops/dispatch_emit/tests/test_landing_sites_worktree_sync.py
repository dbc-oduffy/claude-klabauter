"""Landing sites surface `CommitOutcome.sync_refused` as `worktree_sync_refused` plus
`sync_incomplete: true` and never deny the landing. Real git fixture for the checkpoint stamp
site; a stand-in `commit_paths` outcome for the sites whose set-up is the whole run machinery."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import commit as git_commit
from coordinator_core.git.commit import CommitOutcome, sync_report

_BASE = b"".join(b"line%02d\n" % i for i in range(12))


def _git(repo: Path, *args: str, data: bytes | None = None) -> str:
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(repo.parent))
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, input=data, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.decode().strip()


def _outcome(refused: tuple = ()) -> CommitOutcome:
    return CommitOutcome(sha="a" * 40, staged_preferred=(), sync_refused=refused)


def test_sync_report_is_empty_when_nothing_was_refused() -> None:
    assert sync_report(_outcome()) == {}


def test_sync_report_names_refused_paths_and_flags_incomplete() -> None:
    assert sync_report(_outcome(("a.py", "b.py"))) == {
        "worktree_sync_refused": ["a.py", "b.py"], "sync_incomplete": True,
    }


@pytest.mark.spawns_process
def test_checkpoint_stamp_land_reports_a_colliding_path_and_keeps_the_landing(
    tmp_path: Path, monkeypatch
) -> None:
    from coordinator_core.ops.dispatch_emit.checkpoint_stamp import _land_stamp

    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "a.py").write_bytes(_BASE)
    _git(repo, "add", "--", "a.py")
    _git(repo, "commit", "-q", "-m", "seed")
    real = git_commit.commit_paths
    landed = _BASE.replace(b"line05\n", b"line05 landed\n")
    blob = _git(repo, "hash-object", "-w", "--stdin", data=landed)
    collide = _BASE.replace(b"line05\n", b"line05 peer\n")
    (repo / "a.py").write_bytes(collide)

    monkeypatch.setattr(
        git_commit, "commit_paths",
        lambda root, paths, msg, **kw: real(root, paths, msg, supplied_blobs={"a.py": blob}),
    )
    report: dict = {}
    sha = _land_stamp(
        repo, ["a.py"], "m\n\nSession-Id: 44444444-4444-4444-8444-444444444444", None, report=report
    )

    assert sha
    assert report == {"worktree_sync_refused": ["a.py"], "sync_incomplete": True}
    assert (repo / "a.py").read_bytes() == collide
    assert _git(repo, "rev-parse", "HEAD:a.py") == blob


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
