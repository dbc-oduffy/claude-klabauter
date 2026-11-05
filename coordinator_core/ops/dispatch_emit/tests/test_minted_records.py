"""minted_records: manifest round-trip, one-commit settle, reap-on-refusal, peer withhold."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import commit as commit_mod
from coordinator_core.ops.dispatch_emit import minted_records

pytestmark = pytest.mark.spawns_process

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True, creationflags=_NOWIN
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "state").mkdir()
    (root / "state" / "reused.md").write_text("old\n")
    _git(root, "add", "state/reused.md")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _mint(root: Path, run_dir: Path) -> None:
    (root / "state" / "new1.md").write_text("one\n")
    (root / "state" / "new2.md").write_text("two\n")
    (root / "state" / "reused.md").write_text("edited\n")
    minted_records.record(
        run_dir,
        [
            {"path": "state/new1.md", "created": True},
            {"path": "state/new2.md", "created": True},
            {"path": "state/reused.md", "created": False},
        ],
    )


def _no_peer(_root, _paths):
    return set()


def test_record_read_round_trip_dedupes_and_ors_created(tmp_path):
    run_dir = tmp_path / "run"
    minted_records.record(run_dir, [{"path": "a.md", "created": False}, {"path": "b.md", "created": True}])
    minted_records.record(run_dir, [{"path": "a.md", "created": True}])
    assert minted_records.read(run_dir) == [
        {"path": "a.md", "created": True},
        {"path": "b.md", "created": True},
    ]
    assert minted_records.read(tmp_path / "absent") == []


@pytest.mark.parametrize("bad", [{"path": "../x", "created": True}, {"path": "a.md", "created": "yes"}, {"created": True}])
def test_record_rejects_a_bad_entry_before_writing(tmp_path, bad):
    run_dir = tmp_path / "run"
    with pytest.raises(ValueError):
        minted_records.record(run_dir, [{"path": "ok.md", "created": True}, bad])
    assert minted_records.read(run_dir) == []


def test_settle_lands_every_path_in_one_commit(repo, tmp_path):
    run_dir = tmp_path / "run"
    _mint(repo, run_dir)
    before = int(_git(repo, "rev-list", "--count", "HEAD").strip())

    out = minted_records.settle(repo, repo, "r1", run_dir, message="mint r1", peer_held=_no_peer)

    assert out["sha"] == _git(repo, "rev-parse", "HEAD").strip()
    assert out["reaped"] == [] and out["withheld"] == []
    assert int(_git(repo, "rev-list", "--count", "HEAD").strip()) == before + 1
    assert sorted(_git(repo, "show", "--name-only", "--format=", "HEAD").split()) == [
        "state/new1.md", "state/new2.md", "state/reused.md",
    ]
    assert _git(repo, "status", "--porcelain", "--", "state").strip() == ""


def test_settle_twice_is_nothing_to_commit_and_reaps_nothing(repo, tmp_path):
    run_dir = tmp_path / "run"
    _mint(repo, run_dir)
    minted_records.settle(repo, repo, "r1", run_dir, message="mint r1", peer_held=_no_peer)
    again = minted_records.settle(repo, repo, "r1", run_dir, message="mint r1", peer_held=_no_peer)
    assert again == {"sha": None, "reaped": [], "withheld": []}
    assert (repo / "state" / "new1.md").is_file()


def test_empty_manifest_settles_to_nothing(repo, tmp_path):
    assert minted_records.settle(repo, repo, "r1", tmp_path / "run", message="m", peer_held=_no_peer) == {
        "sha": None, "reaped": [], "withheld": [],
    }


def test_refused_commit_reaps_only_created_untracked_paths(repo, tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    _mint(repo, run_dir)

    def refuse(*_a, **_k):
        raise commit_mod.CommitRefused("lost the CAS race")

    monkeypatch.setattr(commit_mod, "commit_paths", refuse)
    out = minted_records.settle(repo, repo, "r1", run_dir, message="mint r1", peer_held=_no_peer)

    assert out["sha"] is None and out["refused"] == "lost the CAS race"
    assert sorted(out["reaped"]) == ["state/new1.md", "state/new2.md"]
    assert not (repo / "state" / "new1.md").exists()
    assert (repo / "state" / "reused.md").read_text() == "edited\n"
    assert out["kept"] == ["state/reused.md"]


def test_created_flag_on_a_tracked_path_is_never_reaped(repo, tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    minted_records.record(run_dir, [{"path": "state/reused.md", "created": True}])
    (repo / "state" / "reused.md").write_text("edited\n")
    monkeypatch.setattr(commit_mod, "commit_paths", lambda *a, **k: (_ for _ in ()).throw(commit_mod.CommitRefused("x")))
    out = minted_records.settle(repo, repo, "r1", run_dir, message="m", peer_held=_no_peer)
    assert out["reaped"] == []
    assert (repo / "state" / "reused.md").is_file()


def test_peer_claimed_path_is_withheld_from_commit_and_from_reaping(repo, tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    _mint(repo, run_dir)
    peer = lambda _root, paths: {"state/new2.md"} & set(paths)  # noqa: E731

    landed = minted_records.settle(repo, repo, "r1", run_dir, message="mint r1", peer_held=peer)
    assert landed["withheld"] == ["state/new2.md"]
    assert "state/new2.md" not in _git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert (repo / "state" / "new2.md").is_file()

    run2 = tmp_path / "run2"
    (repo / "state" / "new3.md").write_text("three\n")
    minted_records.record(run2, [{"path": "state/new3.md", "created": True}, {"path": "state/new2.md", "created": True}])
    monkeypatch.setattr(commit_mod, "commit_paths", lambda *a, **k: (_ for _ in ()).throw(commit_mod.CommitRefused("x")))
    out = minted_records.settle(repo, repo, "r2", run2, message="m", peer_held=peer)
    assert out["reaped"] == ["state/new3.md"]
    assert out["withheld"] == ["state/new2.md"]
    assert (repo / "state" / "new2.md").is_file()
