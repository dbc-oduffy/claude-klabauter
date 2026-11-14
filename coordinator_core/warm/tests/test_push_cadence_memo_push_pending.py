"""Push-pending sweep arm: push_cadence._sweep_memo_push_pending."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet import _memo_push_pending as pp
from coordinator_core.ops.fleet import _upstream_delivery as ud
from coordinator_core.warm import push_cadence

BRANCH = "work/testbox/2026-11-13"
PATH = "cross-repo/inbox/memo.md"
MESSAGE = "deliver memo\n\nSession-Id: s-1\n"

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(cwd, *args):
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
def _state(monkeypatch):
    monkeypatch.setenv("COORDINATOR_MACHINE", "testbox")
    push_cadence.reset_cadence_for_test()
    monkeypatch.setattr(push_cadence, "machine_profile", lambda: "author")
    yield
    push_cadence.reset_cadence_for_test()


@pytest.fixture
def world(tmp_path):
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
    return {"recv": recv, "bare": bare, "sender": sender, "out": out, "tmp": tmp_path}


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


def test_record_is_pushed_and_removed(world):
    _sweep(world["sender"])
    sha = world["out"].commit_sha
    assert _git(world["bare"], "rev-parse", f"refs/heads/{BRANCH}") == sha
    assert pp.list_pending(world["sender"]) == []
    assert _git(world["recv"], "rev-parse", f"refs/remotes/origin/{BRANCH}") == sha


def test_unreachable_remote_keeps_record_and_counts_attempt(world):
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


def test_moved_tip_is_fetched_rebuilt_and_pushed(world):
    moved = _move_remote_tip(world, "other.txt")
    _sweep(world["sender"])
    tip = _git(world["bare"], "rev-parse", f"refs/heads/{BRANCH}")
    assert tip != world["out"].commit_sha
    assert _git(world["bare"], "rev-parse", f"{tip}^") == moved
    assert _git(world["bare"], "show", f"{tip}:{PATH}") == "memo body"
    assert pp.list_pending(world["sender"]) == []


def test_moved_tip_already_holding_path_retires_record_without_push(world):
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


def test_definitive_reject_never_deletes_record(world):
    hook = world["bare"] / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho no >&2\nexit 1\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    _sweep(world["sender"])
    _sweep(world["sender"])
    rows = pp.list_pending(world["sender"])
    assert [r["commit_sha"] for r in rows] == [world["out"].commit_sha]
    assert rows[0]["attempts"] == 2


def test_non_author_profile_pushes_nothing(world, monkeypatch):
    monkeypatch.setattr(push_cadence, "machine_profile", lambda: "consumer")
    _sweep(world["sender"])
    assert len(pp.list_pending(world["sender"])) == 1
    assert _git(world["bare"], "rev-parse", f"refs/heads/{BRANCH}") != world["out"].commit_sha


def test_deadline_shorter_than_ceiling_defers_push(world):
    push_cadence._sweep_memo_push_pending([world["sender"]], deadline=0.0, clock=lambda: 0.0)
    assert len(pp.list_pending(world["sender"])) == 1


def test_resolver_error_does_not_escape_sweep(world, monkeypatch):
    def _boom(*a, **k):
        raise OSError("config unreadable")

    monkeypatch.setattr(push_cadence, "resolve_push_ceiling", _boom)
    _sweep(world["sender"])
    assert len(pp.list_pending(world["sender"])) == 1


def test_sweep_repos_runs_the_arm_over_served(world):
    push_cadence.sweep_repos([world["sender"]])
    assert _git(world["bare"], "rev-parse", f"refs/heads/{BRANCH}") == world["out"].commit_sha
    assert pp.list_pending(world["sender"]) == []


def test_exhausted_record_is_left_unpushed_on_disk(world):
    row = pp.list_pending(world["sender"])[0]
    pp.write_pending(world["sender"], {**row, "attempts": pp.MAX_ATTEMPTS})
    _sweep(world["sender"])
    assert _git(world["bare"], "rev-parse", f"refs/heads/{BRANCH}") != world["out"].commit_sha
    assert [r["commit_sha"] for r in pp.list_pending(world["sender"])] == [world["out"].commit_sha]


def test_below_cap_record_is_still_pushed(world):
    row = pp.list_pending(world["sender"])[0]
    pp.write_pending(world["sender"], {**row, "attempts": pp.MAX_ATTEMPTS - 1})
    _sweep(world["sender"])
    assert pp.list_pending(world["sender"]) == []
