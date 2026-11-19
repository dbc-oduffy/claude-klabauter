"""Push-pending sweep arm: push_cadence._sweep_memo_push_pending."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet import _memo_anchor as ma
from coordinator_core.ops.fleet import _memo_push_pending as pp
from coordinator_core.ops.fleet import _upstream_delivery as ud
from coordinator_core.warm import push_cadence

BRANCH = "work/testbox/2026-11-13"
PATH = "cross-repo/inbox/memo.md"
MESSAGE = "deliver memo\n\nSession-Id: s-1\n"

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(cwd, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _commit(repo: Path, name: str, text: str) -> str:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


def _config(repo: Path) -> None:
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")


@pytest.fixture(autouse=True)
def _state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COORDINATOR_MACHINE", "testbox")
    push_cadence.reset_cadence_for_test()
    monkeypatch.setattr(push_cadence, "machine_profile", lambda: "author")
    yield
    push_cadence.reset_cadence_for_test()


@pytest.fixture
def world(tmp_path: Path):
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    recv = tmp_path / "recv"
    _git(tmp_path, "init", "-b", "main", str(recv))
    _config(recv)
    _commit(recv, "a.txt", "a")
    _git(recv, "remote", "add", "origin", str(bare))
    _git(recv, "push", "-q", "origin", "main")
    _git(recv, "checkout", "-q", "-b", BRANCH)
    _git(recv, "push", "-q", "origin", BRANCH)
    _git(recv, "fetch", "-q", "origin")
    (tmp_path / "msg.txt").write_text(MESSAGE, encoding="utf-8")
    sender = tmp_path / "sender"
    sender.mkdir()
    out = ud.deliver_onto_upstream(recv, sender, PATH, "memo body\n", tmp_path / "msg.txt")
    assert out.ok, out.error
    if out.on_branch:
        _git(recv, "reset", "-q", "--hard", out.parent_sha)
    assert ma.write_pin(recv / ".git", out.commit_sha)
    return {"recv": recv, "bare": bare, "sender": sender, "out": out, "tmp": tmp_path}


def _anchor(recv: Path, sha: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ma.ANCHOR_REF_PREFIX}memo.md/{sha}"],
        cwd=str(recv), capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _sweep(sender: Path) -> None:
    push_cadence._sweep_memo_push_pending([sender], deadline=1e9, clock=lambda: 0.0)


def _move_remote_tip(world, name: str) -> str:
    other = world["tmp"] / "other"
    _git(world["tmp"], "clone", "-q", str(world["bare"]), str(other))
    _config(other)
    _git(other, "checkout", "-q", BRANCH)
    tip = _commit(other, name, "moved\n")
    _git(other, "push", "-q", "origin", BRANCH)
    return tip


def test_record_is_pushed_and_removed(world) -> None:
    _sweep(world["sender"])
    sha = world["out"].commit_sha
    assert _git(world["bare"], "rev-parse", f"refs/heads/{BRANCH}") == sha
    assert pp.list_pending(world["sender"]) == []
    assert _git(world["recv"], "rev-parse", f"refs/remotes/origin/{BRANCH}") == sha
    assert ma.read_pin(world["recv"] / ".git", sha) is None
    assert _anchor(world["recv"], sha)


def test_unreachable_remote_keeps_record_and_counts_attempt(world) -> None:
    _git(world["recv"], "remote", "set-url", "origin", str(world["tmp"] / "gone.git"))
    _sweep(world["sender"])
    rows = pp.list_pending(world["sender"])
    assert [r["commit_sha"] for r in rows] == [world["out"].commit_sha]
    assert rows[0]["attempts"] == 1
    assert rows[0]["last_error"]
    _git(world["recv"], "remote", "set-url", "origin", str(world["bare"]))
    _sweep(world["sender"])
    assert pp.list_pending(world["sender"]) == []
    assert _git(world["bare"], "rev-parse", f"refs/heads/{BRANCH}") == world["out"].commit_sha


def test_moved_tip_is_fetched_rebuilt_and_pushed(world) -> None:
    moved = _move_remote_tip(world, "other.txt")
    _sweep(world["sender"])
    tip = _git(world["bare"], "rev-parse", f"refs/heads/{BRANCH}")
    assert tip != world["out"].commit_sha
    assert _git(world["bare"], "rev-parse", f"{tip}^") == moved
    assert _git(world["bare"], "show", f"{tip}:{PATH}") == "memo body"
    assert pp.list_pending(world["sender"]) == []
    common = world["recv"] / ".git"
    old = world["out"].commit_sha
    assert ma.read_pin(common, old) is None
    assert ma.read_pin(common, tip) is None
    assert not _anchor(world["recv"], old)
    assert _anchor(world["recv"], tip)


def test_failed_push_keeps_the_pin(world) -> None:
    _git(world["recv"], "remote", "set-url", "origin", str(world["tmp"] / "gone.git"))
    _sweep(world["sender"])
    sha = world["out"].commit_sha
    assert ma.read_pin(world["recv"] / ".git", sha) == sha


def test_moved_tip_already_holding_path_retires_record_without_push(world) -> None:
    other = world["tmp"] / "other"
    _git(world["tmp"], "clone", "-q", str(world["bare"]), str(other))
    _config(other)
    _git(other, "checkout", "-q", BRANCH)
    (other / "cross-repo/inbox").mkdir(parents=True)
    moved = _commit(other, PATH, "someone else\n")
    _git(other, "push", "-q", "origin", BRANCH)
    _sweep(world["sender"])
    assert _git(world["bare"], "rev-parse", f"refs/heads/{BRANCH}") == moved
    assert pp.list_pending(world["sender"]) == []


def _bare_tip(world) -> str:
    return _git(world["bare"], "rev-parse", f"refs/heads/{BRANCH}")


def test_path_already_on_local_tip_retires_record_without_push(world) -> None:
    recv = world["recv"]
    before = _bare_tip(world)
    (recv / "cross-repo/inbox").mkdir(parents=True)
    _commit(recv, PATH, "restored by hand\n")
    _sweep(world["sender"])
    assert _bare_tip(world) == before
    assert pp.list_pending(world["sender"]) == []
    assert ma.read_pin(recv / ".git", world["out"].commit_sha) is None


def test_parent_diverged_from_local_is_never_pushed_and_keeps_record(world) -> None:
    recv = world["recv"]
    sha = world["out"].commit_sha
    before = _bare_tip(world)
    _git(recv, "commit", "-q", "--amend", "--allow-empty", "-m", "rewritten")
    assert _git(recv, "rev-parse", "HEAD") != before
    _sweep(world["sender"])
    assert _bare_tip(world) == before
    rows = pp.list_pending(world["sender"])
    assert [r["commit_sha"] for r in rows] == [sha]
    assert rows[0]["attempts"] == 1
    assert ma.read_pin(recv / ".git", sha) == sha


def test_delivery_under_local_is_still_pushed(world) -> None:
    sha = world["out"].commit_sha
    _git(world["recv"], "merge", "-q", "--ff-only", sha)
    _sweep(world["sender"])
    assert _bare_tip(world) == sha
    assert pp.list_pending(world["sender"]) == []


def test_local_behind_the_delivery_parent_is_still_pushed(world) -> None:
    recv = world["recv"]
    pp.remove_pending(world["sender"], world["out"].commit_sha)
    base = _git(recv, "rev-parse", "HEAD")
    ahead = _commit(recv, "b.txt", "b")
    _git(recv, "push", "-q", "origin", BRANCH)
    _git(recv, "fetch", "-q", "origin")
    out = ud.deliver_onto_upstream(
        recv, world["sender"], "cross-repo/inbox/second.md", "second\n", world["tmp"] / "msg.txt"
    )
    assert out.ok, out.error
    _git(recv, "update-ref", f"refs/heads/{BRANCH}", base)
    _sweep(world["sender"])
    assert _bare_tip(world) == out.commit_sha
    assert _git(world["bare"], "rev-parse", f"{out.commit_sha}^") == ahead
    assert pp.list_pending(world["sender"]) == []


def test_definitive_reject_never_deletes_record(world) -> None:
    hook = world["bare"] / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho no >&2\nexit 1\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    _sweep(world["sender"])
    _sweep(world["sender"])
    rows = pp.list_pending(world["sender"])
    assert [r["commit_sha"] for r in rows] == [world["out"].commit_sha]
    assert rows[0]["attempts"] == 2


def test_non_author_profile_pushes_nothing(world, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(push_cadence, "machine_profile", lambda: "consumer")
    _sweep(world["sender"])
    assert len(pp.list_pending(world["sender"])) == 1
    assert _git(world["bare"], "rev-parse", f"refs/heads/{BRANCH}") != world["out"].commit_sha


def test_deadline_shorter_than_ceiling_defers_push(world) -> None:
    push_cadence._sweep_memo_push_pending([world["sender"]], deadline=0.0, clock=lambda: 0.0)
    assert len(pp.list_pending(world["sender"])) == 1


def test_resolver_error_does_not_escape_sweep(world, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a, **k):
        raise OSError("config unreadable")

    monkeypatch.setattr(push_cadence, "resolve_push_ceiling", _boom)
    _sweep(world["sender"])
    assert len(pp.list_pending(world["sender"])) == 1


def test_sweep_repos_runs_the_arm_over_served(world) -> None:
    push_cadence.sweep_repos([world["sender"]])
    assert _git(world["bare"], "rev-parse", f"refs/heads/{BRANCH}") == world["out"].commit_sha
    assert pp.list_pending(world["sender"]) == []


def test_exhausted_record_is_left_unpushed_on_disk(world) -> None:
    row = pp.list_pending(world["sender"])[0]
    pp.write_pending(world["sender"], {**row, "attempts": pp.MAX_ATTEMPTS})
    _sweep(world["sender"])
    assert _git(world["bare"], "rev-parse", f"refs/heads/{BRANCH}") != world["out"].commit_sha
    assert [r["commit_sha"] for r in pp.list_pending(world["sender"])] == [world["out"].commit_sha]


def test_below_cap_record_is_still_pushed(world) -> None:
    row = pp.list_pending(world["sender"])[0]
    pp.write_pending(world["sender"], {**row, "attempts": pp.MAX_ATTEMPTS - 1})
    _sweep(world["sender"])
    assert pp.list_pending(world["sender"]) == []
