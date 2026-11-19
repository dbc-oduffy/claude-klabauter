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


def _git_in(args: list[str], cwd: Path, stdin: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), input=stdin.encode("utf-8"), capture_output=True, check=True,
        **no_console_creationflags(),
    ).stdout.decode("utf-8").strip()


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


@pytest.fixture
def ahead_receiver(receiver) -> dict:
    """BRANCH holds one commit past its tracking ref and is checked out (HEAD arm)."""
    repo = receiver["repo"]
    _git(["update-ref", f"refs/remotes/origin/{BRANCH}", receiver["local_tip"]], repo)
    engine = _commit_file(repo, "engine.txt", "e\n")
    return {**receiver, "tracking": receiver["local_tip"], "local_tip": engine}


@pytest.fixture
def ahead_ref_direct(ahead_receiver) -> dict:
    """Same, with HEAD on another branch so the delivery goes ref-direct."""
    _git(["checkout", "-q", "-b", "other"], ahead_receiver["repo"])
    return ahead_receiver


def _advance_branch(repo: Path, name: str) -> str:
    """An engine land: a commit on BRANCH's current tip, its ref moved by update-ref."""
    tip = _git(["rev-parse", f"refs/heads/{BRANCH}"], repo)
    tree = _git(["rev-parse", f"{tip}^{{tree}}"], repo)
    new = _git(["commit-tree", tree, "-p", tip, "-m", name], repo)
    _git(["update-ref", f"refs/heads/{BRANCH}", new, tip], repo)
    return new


def test_local_ahead_of_tracking_lands_on_the_ref_direct_branch(ahead_ref_direct) -> None:
    repo = ahead_ref_direct["repo"]
    head_before = _git(["rev-parse", "HEAD"], repo)

    out = ud.deliver_onto_upstream(repo, ahead_ref_direct["sender"], PATH, CONTENT, ahead_ref_direct["msg"])

    assert out.ok, out.error
    assert out.on_branch
    assert out.parent_sha == ahead_ref_direct["local_tip"]
    assert _git(["rev-parse", f"refs/heads/{BRANCH}"], repo) == out.commit_sha
    assert _git(["show", f"{out.commit_sha}:{PATH}"], repo) == "memo body"
    assert _git(["show", f"{out.commit_sha}:engine.txt"], repo) == "e"
    assert _git(["rev-parse", "HEAD"], repo) == head_before
    assert _git(["symbolic-ref", "HEAD"], repo) == "refs/heads/other"
    assert ud.verify_delivery(repo, out) is None
    assert [r["commit_sha"] for r in pp.list_pending(ahead_ref_direct["sender"])] == [out.commit_sha]


def test_local_ahead_on_the_checked_out_branch_lands_with_index_and_worktree_in_step(ahead_receiver) -> None:
    repo = ahead_receiver["repo"]

    out = ud.deliver_onto_upstream(repo, ahead_receiver["sender"], PATH, CONTENT, ahead_receiver["msg"])

    assert out.ok, out.error
    assert out.on_branch
    assert out.parent_sha == ahead_receiver["local_tip"]
    assert _git(["rev-parse", "HEAD"], repo) == out.commit_sha
    assert _git(["rev-parse", f"refs/heads/{BRANCH}"], repo) == out.commit_sha
    assert (repo / PATH).read_text(encoding="utf-8") == CONTENT
    assert PATH not in _git(["status", "--porcelain"], repo)
    assert ud.verify_delivery(repo, out) is None


def test_the_checked_out_branch_landing_spawns_nothing(ahead_receiver, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = ahead_receiver["repo"]
    _no_spawn(monkeypatch)

    out = ud.deliver_onto_upstream(repo, ahead_receiver["sender"], PATH, CONTENT, ahead_receiver["msg"])
    monkeypatch.undo()

    assert out.ok and out.on_branch, out.error
    assert f"A\t{PATH}" not in _git(["diff", "--cached", "--name-status"], repo)
    assert f"D\t{PATH}" not in _git(["diff", "--cached", "--name-status"], repo)


def test_an_unspliceable_index_is_left_byte_identical_and_the_delivery_still_lands(
    ahead_receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = ahead_receiver["repo"]
    _git(["update-index", "--index-version", "4"], repo)
    index_before = (repo / ".git" / "index").read_bytes()
    _no_spawn(monkeypatch)

    out = ud.deliver_onto_upstream(repo, ahead_receiver["sender"], PATH, CONTENT, ahead_receiver["msg"])
    monkeypatch.undo()

    assert out.ok and out.on_branch, out.error
    assert (repo / ".git" / "index").read_bytes() == index_before


def test_an_unreadable_tip_tree_refuses_before_any_worktree_write_and_spawns_nothing(
    ahead_receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = ahead_receiver["repo"]
    monkeypatch.setattr(ud.git_state, "read_tree_spine", lambda *a, **k: None)
    monkeypatch.setattr(ud.git_native, "read_tree_spine", lambda *a, **k: None)
    _no_spawn(monkeypatch)

    out = ud.deliver_onto_upstream(repo, ahead_receiver["sender"], PATH, CONTENT, ahead_receiver["msg"])
    monkeypatch.undo()

    assert not out.ok
    assert not (repo / PATH).exists()
    assert pp.list_pending(ahead_receiver["sender"]) == []


def test_engine_land_and_memo_delivery_sharing_one_parent_both_survive(ahead_ref_direct, monkeypatch) -> None:
    repo = ahead_ref_direct["repo"]
    real_build = ud.git_native.build_new_file_commit
    lands: list[str] = []

    def _build_then_engine_lands(*args, **kwargs):
        result = real_build(*args, **kwargs)
        if not lands:
            lands.append(_advance_branch(repo, "engine land"))
        return result

    monkeypatch.setattr(ud.git_native, "build_new_file_commit", _build_then_engine_lands)

    out = ud.deliver_onto_upstream(repo, ahead_ref_direct["sender"], PATH, CONTENT, ahead_ref_direct["msg"])

    assert out.ok and out.on_branch, out.error
    assert out.parent_sha == lands[0]
    assert _git(["rev-parse", f"refs/heads/{BRANCH}"], repo) == out.commit_sha
    history = _git(["rev-list", f"refs/heads/{BRANCH}"], repo).split()
    assert lands[0] in history and ahead_ref_direct["local_tip"] in history
    assert _git(["show", f"{out.commit_sha}:{PATH}"], repo) == "memo body"
    assert len(pp.list_pending(ahead_ref_direct["sender"])) == 1
    assert _git(["for-each-ref", PIN_PREFIX], repo).count("\n") == 0


def test_a_lost_cas_whose_new_tip_holds_the_path_is_a_collision_with_no_leftovers(ahead_ref_direct, monkeypatch) -> None:
    repo = ahead_ref_direct["repo"]
    real_build = ud.git_native.build_new_file_commit
    done: list[bool] = []

    def _build_then_a_peer_lands_the_path(*args, **kwargs):
        result = real_build(*args, **kwargs)
        if not done:
            done.append(True)
            tip = _git(["rev-parse", f"refs/heads/{BRANCH}"], repo)
            blob = _git_in(["hash-object", "-w", "--stdin"], repo, CONTENT)
            sub = _git_in(["mktree"], repo, f"100644 blob {blob}\t{PATH.rsplit('/', 1)[-1]}\n")
            mid = _git_in(["mktree"], repo, f"040000 tree {sub}\tinbox\n")
            top = _git_in(["mktree"], repo, f"040000 tree {mid}\tcross-repo\n")
            new = _git(["commit-tree", top, "-p", tip, "-m", "peer"], repo)
            _git(["update-ref", f"refs/heads/{BRANCH}", new, tip], repo)
        return result

    monkeypatch.setattr(ud.git_native, "build_new_file_commit", _build_then_a_peer_lands_the_path)

    out = ud.deliver_onto_upstream(repo, ahead_ref_direct["sender"], PATH, CONTENT, ahead_ref_direct["msg"])

    assert not out.ok
    assert out.status == ud.STATUS_COLLISION
    assert pp.list_pending(ahead_ref_direct["sender"]) == []
    assert _git(["for-each-ref", PIN_PREFIX, "refs/coordinator/inbox"], repo) == ""


def test_an_exhausted_cas_bound_falls_back_to_the_pinned_tracking_tip_delivery(ahead_ref_direct, monkeypatch) -> None:
    repo = ahead_ref_direct["repo"]
    real_build = ud.git_native.build_new_file_commit
    lands: list[str] = []

    def _build_then_engine_lands_every_time(*args, **kwargs):
        result = real_build(*args, **kwargs)
        lands.append(_advance_branch(repo, f"engine land {len(lands)}"))
        return result

    monkeypatch.setattr(ud.git_native, "build_new_file_commit", _build_then_engine_lands_every_time)

    out = ud.deliver_onto_upstream(repo, ahead_ref_direct["sender"], PATH, CONTENT, ahead_ref_direct["msg"])

    assert out.ok, out.error
    assert not out.on_branch
    assert out.parent_sha == ahead_ref_direct["tracking"]
    assert len(lands) == ud._CAS_ATTEMPTS + 1
    assert _git(["rev-parse", f"refs/heads/{BRANCH}"], repo) == lands[-1]
    assert [r["commit_sha"] for r in pp.list_pending(ahead_ref_direct["sender"])] == [out.commit_sha]
    assert _git(["rev-parse", PIN_PREFIX + out.commit_sha], repo) == out.commit_sha
    assert len(_git(["for-each-ref", PIN_PREFIX], repo).splitlines()) == 1


def test_an_exhausted_head_branch_bound_removes_the_worktree_file_and_pins(ahead_receiver, monkeypatch) -> None:
    repo = ahead_receiver["repo"]
    calls: list[str] = []

    def _always_lose(path, *args, **kwargs):
        calls.append(path)
        return ud.git_native.GitResult(returncode=1, stdout="", stderr="compare-and-swap failed")

    monkeypatch.setattr(ud.git_native, "commit_authored_new_file", _always_lose)

    out = ud.deliver_onto_upstream(repo, ahead_receiver["sender"], PATH, CONTENT, ahead_receiver["msg"])

    assert out.ok and not out.on_branch, out.error
    assert len(calls) == ud._CAS_ATTEMPTS
    assert not (repo / PATH).exists()
    assert _git(["rev-parse", f"refs/heads/{BRANCH}"], repo) == ahead_receiver["local_tip"]
    assert out.parent_sha == ahead_receiver["tracking"]


def test_an_anchor_failure_after_the_branch_landed_is_ok_with_a_warning_never_failed(ahead_receiver, monkeypatch) -> None:
    repo = ahead_receiver["repo"]
    monkeypatch.setattr(ud, "write_anchor", lambda *a, **k: None)

    out = ud.deliver_onto_upstream(repo, ahead_receiver["sender"], PATH, CONTENT, ahead_receiver["msg"])

    assert out.ok and out.status == ud.STATUS_DELIVERED, out.error
    assert out.on_branch
    assert "anchor" in out.warning
    assert _git(["rev-parse", f"refs/heads/{BRANCH}"], repo) == out.commit_sha
    assert _git(["show", f"{out.commit_sha}:{PATH}"], repo) == "memo body"
    assert pp.list_pending(ahead_receiver["sender"]) == []
    assert ud.verify_delivery(repo, out) is None


def test_a_worktree_file_already_on_disk_is_never_clobbered_and_the_delivery_pins(ahead_receiver) -> None:
    repo = ahead_receiver["repo"]
    (repo / PATH).parent.mkdir(parents=True)
    (repo / PATH).write_text("someone else's\n", encoding="utf-8")

    out = ud.deliver_onto_upstream(repo, ahead_receiver["sender"], PATH, CONTENT, ahead_receiver["msg"])

    assert out.ok and not out.on_branch, out.error
    assert (repo / PATH).read_text(encoding="utf-8") == "someone else's\n"
    assert _git(["rev-parse", f"refs/heads/{BRANCH}"], repo) == ahead_receiver["local_tip"]


def test_a_local_behind_tracking_delivery_stays_object_only_and_off_branch(receiver) -> None:
    repo = receiver["repo"]

    out = ud.deliver_onto_upstream(repo, receiver["sender"], PATH, CONTENT, receiver["msg"])

    assert out.ok and not out.on_branch
    assert _git(["rev-parse", f"refs/heads/{BRANCH}"], repo) == receiver["local_tip"]


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
