"""locked_rmw declares its write to declared_writes only for sizing-home targets."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.locked_write import MutateAbort, locked_rmw
from coordinator_core.session import declared_writes


def _repo(tmp_path: Path) -> Path:
    git = tmp_path / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return tmp_path


def test_sizing_target_is_declared(tmp_path):
    root = _repo(tmp_path)
    target = root / "state" / "sizings" / "x.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("a\n", encoding="utf-8")
    with declared_writes.collecting() as got:
        locked_rmw(target, lambda t: t + "b\n", repo_root=root)
    assert [Path(p).name for p in got] == ["x.yaml"]


def test_local_root_sizing_target_is_declared(tmp_path):
    root = _repo(tmp_path)
    target = root / ".coordinator-local" / "sizings" / "y.yaml"
    with declared_writes.collecting() as got:
        locked_rmw(target, lambda t: "n\n", repo_root=root, missing_ok=True)
    assert [Path(p).name for p in got] == ["y.yaml"]


def test_non_sizing_target_declares_nothing(tmp_path):
    root = _repo(tmp_path)
    target = root / "state" / "other" / "x.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("a\n", encoding="utf-8")
    with declared_writes.collecting() as got:
        locked_rmw(target, lambda t: t + "b\n", repo_root=root)
    assert got == []


def test_noop_rewrite_declares_nothing(tmp_path):
    root = _repo(tmp_path)
    target = root / "state" / "sizings" / "x.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("a\n", encoding="utf-8")
    with declared_writes.collecting() as got:
        locked_rmw(target, lambda t: t, repo_root=root)
    assert got == []


def test_mutate_abort_declares_nothing(tmp_path):
    root = _repo(tmp_path)
    target = root / "state" / "sizings" / "x.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("a\n", encoding="utf-8")

    def abort(_t):
        raise MutateAbort("no")

    with declared_writes.collecting() as got:
        with pytest.raises(MutateAbort):
            locked_rmw(target, abort, repo_root=root)
    assert got == []


def _git_repo(tmp_path: Path) -> Path:
    import subprocess

    kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    for args in (
        ["init", "-q", "-b", "work/p"],
        ["config", "user.email", "t@local"],
        ["config", "user.name", "t"],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, **kw)
    (tmp_path / "seed.txt").write_text("x\n", encoding="utf-8")
    sizing = tmp_path / "state" / "sizings" / "s.yaml"
    sizing.parent.mkdir(parents=True)
    sizing.write_text("a\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True, **kw)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=tmp_path, check=True, capture_output=True, **kw)
    return tmp_path


def _dispatch_sizing_write(repo: Path, monkeypatch, sid: str):
    import asyncio

    from coordinator_core import ipc

    monkeypatch.setenv("CLAUDE_SESSION_ID", sid)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    target = repo / "state" / "sizings" / "s.yaml"

    def handler(params, ctx=None, repo_root=None):
        locked_rmw(target, lambda t: t + "b\n", repo_root=repo)
        return {"ok": True}

    ipc._REGISTRY["test.sizing_write"] = handler
    try:
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "test.sizing_write",
            "params": {},
            "_origin_worktree": str(repo),
        }
        return asyncio.run(ipc.dispatch_message(msg))
    finally:
        ipc._REGISTRY.pop("test.sizing_write", None)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_dispatch_message_records_sizing_in_own_path_claims(tmp_path, monkeypatch):
    from coordinator_core.session import scope

    repo = _git_repo(tmp_path)
    out = _dispatch_sizing_write(repo, monkeypatch, "sid-c2")
    assert "error" not in out, out
    claims = scope.own_path_claims("sid-c2", str(repo))
    assert "state/sizings/s.yaml" in claims, claims


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_commit_v2_commits_declared_sizing_without_orphan_refusal(tmp_path, monkeypatch):
    import subprocess

    from coordinator_core.ops.ceremony import commit_v2

    repo = _git_repo(tmp_path)
    out = _dispatch_sizing_write(repo, monkeypatch, "sid-c2")
    assert "error" not in out, out
    res = commit_v2._handler(
        {"paths": ["state/sizings/s.yaml"], "message": "sizing: record\n"},
        repo_root=repo / ".git",
    )
    assert res.get("committed") is True, res
    names = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.split()
    assert names == ["state/sizings/s.yaml"]


def test_outside_collection_is_noop(tmp_path):
    root = _repo(tmp_path)
    target = root / "state" / "sizings" / "x.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("a\n", encoding="utf-8")
    assert locked_rmw(target, lambda t: t + "b\n", repo_root=root) == "a\nb\n"
