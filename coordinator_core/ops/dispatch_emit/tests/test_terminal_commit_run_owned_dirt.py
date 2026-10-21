"""dispatch.terminal_commit lands a dirty file beside the declared writes only when this run itself
touched it (a chunk report, ``prep.slice_files`` or a run artifact) and no live peer contests it
(``run_owned``). A session claim alone never lands a file: a sibling run of the same session in a
shared directory is reported as ``undeclared_dirty``, like every other undeclared dirty file."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.ops.dispatch_emit.worktree_snapshot import Snapshot
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-000000000001"
PEER = "5e1f0000-0000-4000-8000-000000000002"
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8", newline="")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("a\n", encoding="utf-8", newline="")
    return root


def _run(repo: Path, **params) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("src/a.py",)),))
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8", newline="")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED, **params},
        repo_root=repo / ".git",
    )


def _committed(repo: Path) -> list:
    out = _git(["show", "--numstat", "--format=", "HEAD"], repo)
    return sorted(ln.split("\t")[-1] for ln in out.splitlines() if ln.strip())


def _reviewed_touching(*files: str) -> dict:
    return {**_REVIEWED, "prep": {"slice_files": list(files)}}


def test_a_run_owned_undeclared_file_lands(repo):
    (repo / "src" / "own.py").write_text("own\n", encoding="utf-8", newline="")
    out = _run(repo, session_id=SELF, inline_review=_reviewed_touching("src/own.py"))
    assert out["committed"] is True, out
    assert "src/own.py" in _committed(repo)
    assert out["run_owned"] == ["src/own.py"]
    assert "src/own.py" not in out["undeclared_dirty"]


def test_a_session_claimed_file_the_run_never_touched_is_reported_not_committed(repo):
    (repo / "src" / "own.py").write_text("own\n", encoding="utf-8", newline="")
    session_scope.touch(SELF, "src/own.py", cwd=str(repo))
    out = _run(repo, session_id=SELF)
    assert out["committed"] is True, out
    assert "src/own.py" not in _committed(repo)
    assert out["run_owned"] == []
    assert "src/own.py" in out["undeclared_dirty"]


def test_sibling_run_same_session_same_directory_is_not_staged(repo):
    (repo / "src" / "a_new.py").write_text("a_new = 1\n", encoding="utf-8", newline="")
    (repo / "src" / "b.py").write_text("b\n", encoding="utf-8", newline="")
    (repo / "src" / "b_other.py").write_text("b_other = 1\n", encoding="utf-8", newline="")
    session_scope.touch(SELF, "src/b.py", cwd=str(repo))
    session_scope.touch(SELF, "src/b_other.py", cwd=str(repo))
    out = _run(repo, session_id=SELF, inline_review=_reviewed_touching("src/a_new.py"))
    assert out["committed"] is True, out
    assert _committed(repo) == ["src/a.py", "src/a_new.py"]
    assert out["run_owned"] == ["src/a_new.py"]
    for sibling in ("src/b.py", "src/b_other.py"):
        assert sibling in out["undeclared_dirty"]
        assert sibling in _git(["status", "--porcelain", "-uall", "--", "src"], repo)


def test_a_peer_claimed_file_in_the_same_directory_is_reported_not_committed(repo):
    (repo / "src" / "own.py").write_text("own\n", encoding="utf-8", newline="")
    (repo / "src" / "theirs.py").write_text("theirs\n", encoding="utf-8", newline="")
    session_core.init(PEER, cwd=str(repo))
    session_scope.touch(PEER, "src/theirs.py", cwd=str(repo))
    out = _run(repo, session_id=SELF, inline_review=_reviewed_touching("src/own.py", "src/theirs.py"))
    assert out["committed"] is True, out
    assert _committed(repo) == ["src/a.py", "src/own.py"]
    assert out["run_owned"] == ["src/own.py"]
    assert "src/theirs.py" in out["undeclared_dirty"] or out.get("undeclared_dirty_peer_held")
    assert out["peer_held_left"] == [{"path": "src/theirs.py", "sessions": [PEER]}]


def test_an_unclaimed_undeclared_file_is_reported_not_committed(repo):
    (repo / "src" / "stray.py").write_text("stray\n", encoding="utf-8", newline="")
    out = _run(repo, session_id=SELF)
    assert out["committed"] is True, out
    assert "src/stray.py" not in _committed(repo)
    assert out["run_owned"] == []
    assert "src/stray.py" in out["undeclared_dirty"]


def test_a_claimed_file_outside_the_declared_directories_is_not_run_owned(repo):
    (repo / "elsewhere").mkdir()
    (repo / "elsewhere" / "other-run.py").write_text("x\n", encoding="utf-8", newline="")
    session_scope.touch(SELF, "elsewhere/other-run.py", cwd=str(repo))
    out = _run(repo, session_id=SELF)
    assert out["committed"] is True, out
    assert "elsewhere/other-run.py" not in _committed(repo)
    assert out["run_owned"] == []


def _track(repo: Path, *names: str) -> None:
    for name in names:
        (repo / "src" / name).write_text(name + "\n", encoding="utf-8", newline="")
    _git(["add", "src"], repo)
    _git(["commit", "-q", "-m", "track"], repo)
    (repo / "src" / "a.py").write_text("a_edited = 1\n", encoding="utf-8", newline="")


def _run_stranded(repo: Path, **params) -> dict:
    request = CommitRequest(chunks=(
        ChunkCommit(id="C1", title="t1", paths=("src/a.py",)),
        ChunkCommit(id="C2", title="t2", paths=("src/gone.py",)),
    ))
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8", newline="")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": ["C2"], "inline_review": _REVIEWED, **params},
        repo_root=repo / ".git",
    )


def test_a_stranded_rows_declared_deletion_is_held_back_and_reported(repo):
    # The credit gate outranks run-owned dirt: a path a stranded chunk declares never rides
    # the commit by a side route, a deletion included. It is reported, not silently dropped.
    _track(repo, "gone.py")
    (repo / "src" / "gone.py").unlink()
    (repo / "src" / "own.py").write_text("own\n", encoding="utf-8", newline="")
    out = _run_stranded(repo, session_id=SELF, inline_review=_reviewed_touching("src/own.py"))
    assert out["committed"] is True, out
    assert _committed(repo) == ["src/a.py", "src/own.py"]
    assert "src/gone.py" not in out.get("deleted_paths", [])
    assert "src/gone.py" in out["stranded_residue"]["C2"]
    assert "src/gone.py" in _git(["ls-tree", "-r", "--name-only", "HEAD"], repo).split()


def test_an_undeclared_unclaimed_deleted_tracked_file_is_not_committed(repo):
    _track(repo, "stray.py")
    (repo / "src" / "stray.py").unlink()
    out = _run(repo, session_id=SELF)
    assert out["committed"] is True, out
    assert "src/stray.py" not in _committed(repo)
    assert "src/stray.py" not in out["deleted_paths"]
    assert "src/stray.py" in _git(["ls-tree", "-r", "--name-only", "HEAD"], repo).split()


def test_a_peer_contested_deleted_file_is_not_committed(repo):
    _track(repo, "gone.py")
    (repo / "src" / "gone.py").unlink()
    session_core.init(PEER, cwd=str(repo))
    session_scope.touch(PEER, "src/gone.py", cwd=str(repo))
    out = _run_stranded(repo, session_id=SELF)
    assert "src/gone.py" not in out.get("deleted_paths", [])
    assert "src/gone.py" in _git(["ls-tree", "-r", "--name-only", "HEAD"], repo).split()


def test_an_unreadable_head_spine_joins_no_run_owned_deletions(repo, monkeypatch):
    _track(repo, "gone.py")
    (repo / "src" / "gone.py").unlink()
    from coordinator_core.git import commit as git_commit

    calls: list = []

    def partition(root, rels):
        calls.append(list(rels))
        return None

    monkeypatch.setattr(git_commit, "partition_declared_deletions", partition)
    request = CommitRequest(chunks=(
        ChunkCommit(id="C1", title="t1", paths=("src/a.py",)),
        ChunkCommit(id="C2", title="t2", paths=("src/gone.py",)),
    ))
    snap = Snapshot({"src/gone.py", "src/a.py"}, [], {"src/gone.py"}, [])
    files, deletions = terminal_commit._run_owned_dirt(
        repo, request, snap, SELF, ["src/a.py"], [], frozenset({"src/gone.py"})
    )
    assert (files, deletions) == ([], [])
    assert calls == [["src/gone.py"]]


def test_a_call_without_a_session_id_keeps_the_declared_only_behaviour(repo):
    (repo / "src" / "own.py").write_text("own\n", encoding="utf-8", newline="")
    session_scope.touch(SELF, "src/own.py", cwd=str(repo))
    out = _run(repo)
    assert out["committed"] is True, out
    assert _committed(repo) == ["src/a.py"]
    assert out["run_owned"] == []
    assert "src/own.py" in out["undeclared_dirty"]
