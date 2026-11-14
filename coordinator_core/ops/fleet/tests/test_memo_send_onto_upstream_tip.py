"""memo.send delivers onto the receiver's upstream tip and the warm engine pushes it.

Exit test for the plan: a receiver whose local branch is behind upstream with a dirty tree gets the
memo on top of upstream, pushed by one `push_cadence.on_idle_tick`, with its HEAD, index and
worktree unchanged; a failed push leaves a retryable outbox record. Receivers are real tmp_path
clones with a bare origin; `_memo_send` runs at its default params.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import daily_day, machine_resolver
from coordinator_core.ops.fleet.memo_send import _memo_send
from coordinator_core.ops.fleet.tests.test_memo_send import (
    _git as _run_git,
    _make_claude_home,
    _make_receiver_git_repo,
    _make_sender_git_repo,
    _pending_records,
    _write_draft,
)
from coordinator_core.warm import push_cadence

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(cwd, *args) -> str:
    out = _run_git(cwd, *args).stdout
    return (out.decode() if isinstance(out, bytes) else out).strip()


@pytest.fixture(autouse=True)
def _cadence(monkeypatch):
    push_cadence.reset_cadence_for_test()
    monkeypatch.setattr(push_cadence, "machine_profile", lambda: "author")
    yield
    push_cadence.reset_cadence_for_test()


def _tick(sender: Path) -> bool:
    return push_cadence.on_idle_tick(
        served_repos=lambda: [sender], clock=lambda: 0.0, interval_secs=0.0
    ) or push_cadence.on_idle_tick(
        served_repos=lambda: [sender], clock=lambda: 1.0, interval_secs=0.0
    )


def _checkout_state(root: Path) -> tuple:
    return (
        _git(root, "rev-parse", "HEAD"),
        _git(root, "symbolic-ref", "HEAD"),
        (root / ".git" / "index").read_bytes(),
        _git(root, "status", "--porcelain"),
    )


def _upstream_moves_on(tmp_path: Path, bare: Path, day: str, name: str) -> str:
    peer = tmp_path / f"peer-{name}"
    _git(tmp_path, "clone", "-q", "-b", day, str(bare), str(peer))
    _git(peer, "config", "user.email", "peer@claude-klabauter.test")
    _git(peer, "config", "user.name", "Peer")
    (peer / f"{name}.txt").write_text("upstream\n", encoding="utf-8")
    _git(peer, "add", "-A")
    _git(peer, "commit", "-q", "-m", f"upstream {name}")
    _git(peer, "push", "-q", "origin", day)
    return _git(peer, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A receiver on its day branch, one commit behind upstream, with a dirty tree."""
    sender = _make_sender_git_repo(tmp_path)
    receiver = _make_receiver_git_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_HOME", str(_make_claude_home(tmp_path, {"project_rag": receiver})))
    bare = tmp_path / "receiver-repo-origin.git"
    day = f"work/{machine_resolver.compute_machine()}/{daily_day.local_day(str(receiver))}"
    _git(receiver, "checkout", "-q", "-b", day)
    _git(receiver, "push", "-q", "-u", "origin", day)
    local_tip = _git(receiver, "rev-parse", day)
    upstream_tip = _upstream_moves_on(tmp_path, bare, day, "ahead")
    _git(receiver, "fetch", "-q", "origin")
    (receiver / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    (receiver / "cross-repo" / "inbox" / ".gitkeep").write_text("edited\n", encoding="utf-8")
    assert _git(receiver, "rev-parse", f"refs/remotes/origin/{day}") == upstream_tip != local_tip
    return {
        "sender": sender, "receiver": receiver, "bare": bare, "day": day,
        "local_tip": local_tip, "upstream_tip": upstream_tip, "tmp": tmp_path,
    }


def test_behind_and_dirty_receiver_gets_the_memo_on_top_of_upstream_and_pushed(world):
    sender, receiver, bare, day = world["sender"], world["receiver"], world["bare"], world["day"]
    _write_draft(sender, "exit-topic")
    state_before = _checkout_state(receiver)

    result = _memo_send({"dry_run": False, "topic": "exit-topic"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    sha = result["acted"][0]["delivery_commit_sha"]
    assert _git(receiver, "rev-parse", f"{sha}^") == world["upstream_tip"]
    assert _git(receiver, "rev-parse", f"refs/heads/{day}") == world["local_tip"]
    assert _checkout_state(receiver) == state_before
    assert [r["commit_sha"] for r in _pending_records(sender)] == [sha]

    assert _tick(sender)

    assert _git(bare, "rev-parse", f"refs/heads/{day}") == sha
    assert _pending_records(sender) == []
    assert _git(receiver, "rev-parse", f"refs/remotes/origin/{day}") == sha
    assert _git(receiver, "rev-parse", f"refs/heads/{day}") == world["local_tip"]
    assert _checkout_state(receiver) == state_before


def test_a_failed_push_leaves_a_retryable_record_that_lands_on_the_next_tick(world):
    sender, receiver, bare, day = world["sender"], world["receiver"], world["bare"], world["day"]
    _write_draft(sender, "retry-topic")
    result = _memo_send({"dry_run": False, "topic": "retry-topic"}, repo_root=sender)
    sha = result["acted"][0]["delivery_commit_sha"]
    _git(receiver, "remote", "set-url", "origin", str(world["tmp"] / "gone.git"))

    assert _tick(sender)

    rows = _pending_records(sender)
    assert [r["commit_sha"] for r in rows] == [sha]
    assert rows[0]["attempts"] == 1
    assert rows[0]["last_error"]
    _git(receiver, "remote", "set-url", "origin", str(bare))
    push_cadence.reset_cadence_for_test()

    assert _tick(sender)

    assert _git(bare, "rev-parse", f"refs/heads/{day}") == sha
    assert _pending_records(sender) == []


def test_upstream_moving_after_the_send_is_rebuilt_onto_and_pushed(world):
    sender, bare, day = world["sender"], world["bare"], world["day"]
    _write_draft(sender, "moved-topic")
    result = _memo_send({"dry_run": False, "topic": "moved-topic"}, repo_root=sender)
    sha = result["acted"][0]["delivery_commit_sha"]
    newer = _upstream_moves_on(world["tmp"], bare, day, "later")

    assert _tick(sender)

    pushed = _git(bare, "rev-parse", f"refs/heads/{day}")
    assert pushed != sha
    assert _git(bare, "rev-parse", f"{pushed}^") == newer
    assert "moved-topic" in _git(bare, "ls-tree", "--name-only", f"{pushed}:cross-repo/inbox")
    assert _pending_records(sender) == []
