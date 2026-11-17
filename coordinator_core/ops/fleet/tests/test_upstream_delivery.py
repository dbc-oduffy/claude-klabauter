"""deliver_onto_upstream builds on the receiver's upstream tip and touches nothing else.

The receiver is a real git repo built with git; the call under test runs with Popen
patched to raise, so the delivery path is proven zero-spawn.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony.tests.fixtures.real_git import real_git_repo
from coordinator_core.ops.fleet import _memo_push_pending as pp
from coordinator_core.ops.fleet import _upstream_delivery as ud
from coordinator_core.ops.fleet._memo_anchor import retire_pin, write_pin
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

BRANCH = "work/testbox/2026-11-13"
PATH = "cross-repo/inbox/memo.md"
PIN_PREFIX = "refs/coordinator/inbox-pin/"
CONTENT = "memo body\n"
MESSAGE = "deliver memo\n\nSession-Id: s-1\n"


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _commit_file(repo: Path, name: str, text: str) -> str:
    (repo / name).write_text(text, encoding="utf-8")
    _git(["add", name], repo)
    _git(["commit", "-q", "-m", f"add {name}"], repo)
    return _git(["rev-parse", "HEAD"], repo)


@pytest.fixture
def receiver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Branch BRANCH one commit behind refs/remotes/origin/BRANCH, with a dirty tree."""
    monkeypatch.setenv("COORDINATOR_MACHINE", "testbox")
    repo = real_git_repo(tmp_path)
    _git(["checkout", "-q", "-b", BRANCH], repo)
    local_tip = _git(["rev-parse", "HEAD"], repo)
    upstream_tip = _commit_file(repo, "upstream-only.txt", "u\n")
    _git(["update-ref", f"refs/remotes/origin/{BRANCH}", upstream_tip], repo)
    _git(["reset", "-q", "--hard", local_tip], repo)
    _git(["remote", "add", "origin", str(tmp_path / "remote.git")], repo)
    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    (tmp_path / "msg.txt").write_text(MESSAGE, encoding="utf-8")
    sender = tmp_path / "sender"
    sender.mkdir()
    return {
        "repo": repo, "sender": sender, "local_tip": local_tip,
        "upstream_tip": upstream_tip, "msg": tmp_path / "msg.txt",
    }


def _snapshot(repo: Path) -> dict:
    return {
        "head": _git(["rev-parse", "HEAD"], repo),
        "head_ref": _git(["symbolic-ref", "HEAD"], repo),
        "heads": _git(["for-each-ref", "refs/heads"], repo),
        "remotes": _git(["for-each-ref", "refs/remotes"], repo),
        "index": (repo / ".git" / "index").read_bytes(),
        "status": _git(["status", "--porcelain"], repo),
        "head_log": (repo / ".git" / "logs" / "HEAD").read_bytes(),
    }


def _no_spawn(monkeypatch) -> None:
    def _boom(*a, **k):
        raise AssertionError("a process was spawned on the delivery path")

    monkeypatch.setattr(subprocess, "Popen", _boom)


def test_delivers_onto_tracking_tip_and_touches_nothing(receiver, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = receiver["repo"]
    before = _snapshot(repo)
    _no_spawn(monkeypatch)

    out = ud.deliver_onto_upstream(repo, receiver["sender"], PATH, CONTENT, receiver["msg"])
    monkeypatch.undo()

    assert out.ok, out.error
    assert out.status == ud.STATUS_DELIVERED
    assert (out.remote, out.branch) == ("origin", BRANCH)
    assert out.parent_sha == receiver["upstream_tip"]
    assert _git(["rev-parse", f"{out.commit_sha}^"], repo) == receiver["upstream_tip"]
    assert _git(["diff-tree", "--no-commit-id", "--name-status", "-r", out.commit_sha], repo) == f"A\t{PATH}"
    assert _git(["show", f"{out.commit_sha}:upstream-only.txt"], repo) == "u"
    assert _snapshot(repo) == before
    assert not (repo / PATH).exists()
    anchor = _git(["rev-parse", out.anchor_ref], repo)
    assert anchor == out.blob_sha
    rows = pp.list_pending(receiver["sender"])
    assert len(rows) == 1
    assert {k: rows[0][k] for k in pp.REQUIRED_KEYS} == {
        "receiver_root": str(repo), "remote": "origin", "branch": BRANCH, "path": PATH,
        "blob_sha": out.blob_sha, "message": MESSAGE, "commit_sha": out.commit_sha,
        "parent_sha": receiver["upstream_tip"],
    }


def test_pin_resolves_to_the_delivery_commit_and_verify_delivery_passes(receiver) -> None:
    repo = receiver["repo"]
    out = ud.deliver_onto_upstream(repo, receiver["sender"], PATH, CONTENT, receiver["msg"])

    assert out.ok, out.error
    assert _git(["rev-parse", PIN_PREFIX + out.commit_sha], repo) == out.commit_sha
    assert ud.verify_delivery(repo, out) is None


def test_pin_lost_cas_refuses_with_no_record_no_pin_and_no_anchor(receiver, monkeypatch) -> None:
    repo = receiver["repo"]
    real_build = ud.git_native.build_new_file_commit

    def _build_then_lock_the_pin(rel_path, content, msg_file, repo_path, **kwargs):
        result = real_build(rel_path, content, msg_file, repo_path, **kwargs)
        lock = repo / ".git" / (PIN_PREFIX + result.stdout.strip() + ".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_bytes(b"held by a peer\n")
        return result

    monkeypatch.setattr(ud.git_native, "build_new_file_commit", _build_then_lock_the_pin)

    out = ud.deliver_onto_upstream(repo, receiver["sender"], PATH, CONTENT, receiver["msg"])

    assert not out.ok
    assert out.status == ud.STATUS_ANCHOR_FAILED
    assert pp.list_pending(receiver["sender"]) == []
    assert _git(["for-each-ref", "refs/coordinator/inbox"], repo) == ""
    assert _git(["for-each-ref", PIN_PREFIX], repo) == ""


def test_verify_delivery_refuses_a_missing_pin_a_wrong_anchor_and_an_unreadable_tree(receiver) -> None:
    repo = receiver["repo"]
    out = ud.deliver_onto_upstream(repo, receiver["sender"], PATH, CONTENT, receiver["msg"])

    assert "anchor" in ud.verify_delivery(repo, out._replace(blob_sha="0" * 40))
    assert write_pin(repo / ".git", "1" * 40)
    assert "tree" in ud.verify_delivery(repo, out._replace(commit_sha="1" * 40))
    assert retire_pin(repo / ".git", "1" * 40)
    ud.retire_delivery_refs(repo, out)
    assert "pin" in ud.verify_delivery(repo, out)
    assert _git(["for-each-ref", PIN_PREFIX, "refs/coordinator/inbox"], repo) == ""


def test_rebuild_writes_a_pin_for_the_rebuilt_commit(receiver) -> None:
    repo = receiver["repo"]
    ud.deliver_onto_upstream(repo, receiver["sender"], PATH, CONTENT, receiver["msg"])
    moved = _commit_file(repo, "moved.txt", "m\n")
    _git(["update-ref", f"refs/remotes/origin/{BRANCH}", moved], repo)
    _git(["reset", "-q", "--hard", receiver["local_tip"]], repo)
    old = pp.list_pending(receiver["sender"])[0]

    out = ud.rebuild_and_retarget(old, moved, sender_root=receiver["sender"])

    assert out.ok, out.error
    assert _git(["rev-parse", PIN_PREFIX + out.commit_sha], repo) == out.commit_sha
    assert ud.verify_delivery(repo, out) is None


def test_without_a_tracking_ref_the_local_branch_tip_is_the_base(receiver) -> None:
    repo = receiver["repo"]
    _git(["update-ref", "-d", f"refs/remotes/origin/{BRANCH}"], repo)

    out = ud.deliver_onto_upstream(repo, receiver["sender"], PATH, CONTENT, receiver["msg"])

    assert out.ok, out.error
    assert out.parent_sha == receiver["local_tip"]


def test_a_path_already_in_the_base_tree_is_a_collision_with_no_record(receiver) -> None:
    repo = receiver["repo"]
    out = ud.deliver_onto_upstream(repo, receiver["sender"], "upstream-only.txt", "x\n", receiver["msg"])

    assert not out.ok
    assert out.status == ud.STATUS_COLLISION
    assert pp.list_pending(receiver["sender"]) == []


def test_no_resolvable_remote_is_reported_not_delivered(receiver) -> None:
    repo = receiver["repo"]
    _git(["remote", "remove", "origin"], repo)

    out = ud.deliver_onto_upstream(repo, receiver["sender"], PATH, CONTENT, receiver["msg"])

    assert not out.ok
    assert out.status == ud.STATUS_NO_REMOTE
    assert pp.list_pending(receiver["sender"]) == []


def test_rebuild_moves_the_record_onto_the_new_parent(receiver) -> None:
    repo = receiver["repo"]
    first = ud.deliver_onto_upstream(repo, receiver["sender"], PATH, CONTENT, receiver["msg"])
    moved = _commit_file(repo, "moved.txt", "m\n")
    _git(["update-ref", f"refs/remotes/origin/{BRANCH}", moved], repo)
    _git(["reset", "-q", "--hard", receiver["local_tip"]], repo)
    old = pp.list_pending(receiver["sender"])[0]

    out = ud.rebuild_and_retarget(old, moved, sender_root=receiver["sender"])

    assert out.ok, out.error
    assert out.commit_sha != first.commit_sha
    assert _git(["rev-parse", f"{out.commit_sha}^"], repo) == moved
    assert _git(["show", f"{out.commit_sha}:{PATH}"], repo) == "memo body"
    assert _git(["log", "-1", "--format=%B", out.commit_sha], repo).strip() == MESSAGE.strip()
    rows = pp.list_pending(receiver["sender"])
    assert [r["commit_sha"] for r in rows] == [out.commit_sha]
    assert rows[0]["parent_sha"] == moved


def test_rebuild_onto_a_tree_holding_the_path_is_a_collision_and_keeps_the_record(receiver) -> None:
    repo = receiver["repo"]
    ud.deliver_onto_upstream(repo, receiver["sender"], PATH, CONTENT, receiver["msg"])
    (repo / "cross-repo" / "inbox").mkdir(parents=True)
    holder = _commit_file(repo, PATH, CONTENT)
    old = pp.list_pending(receiver["sender"])[0]

    out = ud.rebuild_and_retarget(old, holder, sender_root=receiver["sender"])

    assert not out.ok
    assert out.status == ud.STATUS_COLLISION
    assert [r["commit_sha"] for r in pp.list_pending(receiver["sender"])] == [old["commit_sha"]]
