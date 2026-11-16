"""`dispatch.land_blobs` finishes a landing by syncing each landed path into the worktree: a path ends equal to
HEAD or carrying only dirt rebased on HEAD, and a path that cannot be reconciled is reported and left byte-intact.

A peer edit lands in the window between the landing's file read and the sync, so each case writes the worktree
from a wrapper around `land_blobs._publish`, after the real ref move."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_blobs, land_blobs_contract as contract
from coordinator_core.ops.dispatch_emit import land_blobs_worktree_sync as wsync
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process

X = "pkg/shared.py"
RUN = "ask-20261114T100000-aaaaaa"
LINES = 120
OWN = "x_115 = 'OWN'"


def _git(args, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
                          **no_console_creationflags()).stdout.strip()


def _body(**edits: str) -> bytes:
    rows = [f"x_{i} = {i}" for i in range(1, LINES + 1)]
    for line, text in edits.items():
        rows[int(line[1:]) - 1] = text
    return ("\n".join(rows) + "\n").encode("utf-8")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    (root / "pkg").mkdir()
    (root / X).write_bytes(_body())
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _fire(root: Path, paths=(X,)) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=tuple(paths)),))
    (root / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    params = {"script_path": "run.mjs", "run_id": RUN, "inline_review": reviewed(), "incomplete_chunks": []}
    return land_blobs._handler(params, repo_root=root / ".git")


def _peer_writes_after_the_ref_moves(monkeypatch, root: Path, data: bytes) -> None:
    real = land_blobs._publish

    def publish(*args, **kwargs):
        out = real(*args, **kwargs)
        (root / X).write_bytes(data)
        return out

    monkeypatch.setattr(land_blobs, "_publish", publish)


class _Spawns:
    def __init__(self, monkeypatch) -> None:
        self.argvs: list = []
        original = subprocess.Popen.__init__
        spawns = self

        def spy(popen, args, *a, **kw):
            spawns.argvs.append(args)
            original(popen, args, *a, **kw)

        monkeypatch.setattr(subprocess.Popen, "__init__", spy)


def test_a_clean_landed_path_equals_head_and_reports_nothing_to_sync(repo, monkeypatch):
    (repo / X).write_bytes(_body(l115=OWN))
    spawns = _Spawns(monkeypatch)

    out = _fire(repo)

    assert out["committed"] is True, out
    assert spawns.argvs == []
    assert "worktree_sync_refused" not in out and "worktree_rebased" not in out and "sync_incomplete" not in out
    assert (repo / X).read_bytes() == _body(l115=OWN)
    assert _git(["status", "--porcelain", "--", X], repo) == ""


def test_a_cr_bearing_landed_path_under_autocrlf_false_is_head_equal_not_a_refusal(repo):
    crlf = _body(l115=OWN).replace(b"\n", b"\r\n")
    (repo / X).write_bytes(crlf)

    out = _fire(repo)

    assert out["committed"] is True, out
    assert "worktree_sync_refused" not in out and "sync_incomplete" not in out
    assert (repo / X).read_bytes() == crlf


def test_a_worktree_the_peer_reverted_to_the_base_blob_is_written_with_the_head_blob(repo, monkeypatch):
    (repo / X).write_bytes(_body(l115=OWN))
    _peer_writes_after_the_ref_moves(monkeypatch, repo, _body())

    out = _fire(repo)

    assert out["committed"] is True, out
    assert "worktree_sync_refused" not in out and "worktree_rebased" not in out
    assert (repo / X).read_bytes() == _body(l115=OWN)
    assert _git(["rev-parse", f"HEAD:{X}"], repo) == _git(["hash-object", X], repo)


def test_peer_dirt_on_another_hunk_is_rebased_onto_the_landed_blob(repo, monkeypatch):
    (repo / X).write_bytes(_body(l115=OWN))
    _peer_writes_after_the_ref_moves(monkeypatch, repo, _body(l5="x_5 = 'PEER'"))
    spawns = _Spawns(monkeypatch)

    out = _fire(repo)

    assert out["committed"] is True, out
    assert contract.unledgered(spawns.argvs) == []
    assert out["worktree_rebased"] == [X]
    assert "worktree_sync_refused" not in out
    assert (repo / X).read_bytes() == _body(l5="x_5 = 'PEER'", l115=OWN)
    assert _git(["show", f"HEAD:{X}"], repo).splitlines()[4] == "x_5 = 5"
    assert _git(["show", f"HEAD:{X}"], repo).splitlines()[114] == OWN


def test_a_conflicting_peer_edit_is_reported_by_path_and_its_bytes_are_untouched(repo, monkeypatch):
    head = _git(["rev-parse", "HEAD"], repo)
    (repo / X).write_bytes(_body(l115=OWN))
    peer = _body(l115="x_115 = 'PEER'")
    _peer_writes_after_the_ref_moves(monkeypatch, repo, peer)

    out = _fire(repo)

    assert out["committed"] is True and out["sha"] != head
    assert out["worktree_sync_refused"] == [X]
    assert out["sync_incomplete"] is True
    assert "refused" not in out
    assert (repo / X).read_bytes() == peer
    assert _git(["show", f"HEAD:{X}"], repo).splitlines()[114] == OWN


def test_a_conflict_on_one_path_leaves_the_other_landed_path_synced(repo, monkeypatch):
    other = "pkg/other.py"
    (repo / other).write_bytes(_body())
    _git(["add", other], repo)
    _git(["commit", "-q", "-m", "other"], repo)
    (repo / X).write_bytes(_body(l115=OWN))
    (repo / other).write_bytes(_body(l115=OWN))
    real = land_blobs._publish
    peer = _body(l115="x_115 = 'PEER'")

    def publish(*args, **kwargs):
        out = real(*args, **kwargs)
        (repo / X).write_bytes(peer)
        (repo / other).write_bytes(_body())
        return out

    monkeypatch.setattr(land_blobs, "_publish", publish)

    out = _fire(repo, paths=(X, other))

    assert out["worktree_sync_refused"] == [X]
    assert (repo / X).read_bytes() == peer
    assert (repo / other).read_bytes() == _body(l115=OWN)


def test_sync_plan_pairs_the_head_entry_with_the_staged_entry_and_a_deletion_with_none():
    spine = {"pkg": {"a.py": (0o100644, "a" * 40), "gone.py": (0o100644, "g" * 40)}}
    trees = {"pkg": {"a.py": (0o100644, "b" * 40)}, "": {}}

    plan = wsync.sync_plan(spine, trees, ["pkg/a.py"], ["pkg/gone.py"])

    assert plan == {"pkg/a.py": ((0o100644, "a" * 40), (0o100644, "b" * 40)),
                    "pkg/gone.py": ((0o100644, "g" * 40), None)}
