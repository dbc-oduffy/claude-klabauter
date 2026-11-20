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
from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.fleet import _upstream_delivery
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
def _cadence(monkeypatch: pytest.MonkeyPatch):
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
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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


def test_behind_and_dirty_receiver_gets_the_memo_on_top_of_upstream_and_pushed(world) -> None:
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


def test_default_send_pins_the_commit_and_says_the_push_is_pending(world) -> None:
    sender, receiver = world["sender"], world["receiver"]
    _write_draft(sender, "pin-topic")

    result = _memo_send({"dry_run": False, "topic": "pin-topic"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    item = result["acted"][0]
    sha = item["delivery_commit_sha"]
    assert _git(receiver, "rev-parse", f"{sha}^") == world["upstream_tip"]
    assert _git(receiver, "rev-parse", f"refs/coordinator/inbox-pin/{sha}") == sha
    assert item["push_pending"] is True
    assert [r["commit_sha"] for r in _pending_records(sender)] == [sha]
    assert _git(receiver, "rev-parse", f"refs/heads/{world['day']}") == world["local_tip"]


def test_object_only_delivery_reports_pinned_while_the_branch_lacks_the_memo(world) -> None:
    sender, receiver = world["sender"], world["receiver"]
    _write_draft(sender, "pinned-outcome-topic")

    result = _memo_send({"dry_run": False, "topic": "pinned-outcome-topic"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    assert result["acted"][0]["delivery_outcome"] == "pinned"
    assert "pinned-outcome-topic" not in _git(
        receiver, "ls-tree", "--name-only", f"refs/heads/{world['day']}:cross-repo/inbox"
    )


def test_local_branch_ahead_of_tracking_gets_the_memo_in_its_tip_and_reports_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sender = _make_sender_git_repo(tmp_path)
    receiver = _make_receiver_git_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_HOME", str(_make_claude_home(tmp_path, {"project_rag": receiver})))
    day = f"work/{machine_resolver.compute_machine()}/{daily_day.local_day(str(receiver))}"
    _git(receiver, "checkout", "-q", "-b", day)
    _git(receiver, "push", "-q", "-u", "origin", day)
    tracking_tip = _git(receiver, "rev-parse", day)
    (receiver / "engine-land.txt").write_text("local only\n", encoding="utf-8")
    _git(receiver, "add", "engine-land.txt")
    _git(receiver, "commit", "-q", "-m", "engine land")
    local_tip = _git(receiver, "rev-parse", day)
    assert local_tip != tracking_tip
    _write_draft(sender, "ahead-topic")

    result = _memo_send({"dry_run": False, "topic": "ahead-topic"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    assert result["acted"][0]["delivery_outcome"] == "delivered"
    tip = _git(receiver, "rev-parse", f"refs/heads/{day}")
    assert _git(receiver, "merge-base", "--is-ancestor", local_tip, tip) == ""
    assert "ahead-topic" in _git(receiver, "ls-tree", "--name-only", f"{tip}:cross-repo/inbox")
    assert (receiver / "engine-land.txt").exists()


def test_a_held_pin_lock_refuses_the_send_and_leaves_nothing_owed(world, monkeypatch) -> None:
    sender, receiver = world["sender"], world["receiver"]
    _write_draft(sender, "pin-lost-topic")
    real_build = git_native.build_new_file_commit

    def _build_then_lock_the_pin(rel_path, content, msg_file, repo_path, **kwargs):
        result = real_build(rel_path, content, msg_file, repo_path, **kwargs)
        lock = receiver / ".git" / f"refs/coordinator/inbox-pin/{result.stdout.strip()}.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_bytes(b"held by a peer\n")
        return result

    monkeypatch.setattr(git_native, "build_new_file_commit", _build_then_lock_the_pin)

    result = _memo_send({"dry_run": False, "topic": "pin-lost-topic"}, repo_root=sender)

    assert result["exit_code"] != 0, result
    assert result["acted"] == []
    assert "pin" in result["failed"][0]["reason"]
    assert _pending_records(sender) == []
    assert _git(receiver, "for-each-ref", "refs/coordinator/inbox", "refs/coordinator/inbox-pin") == ""


def test_an_anchor_that_does_not_read_back_refuses_the_send(world, monkeypatch) -> None:
    sender, receiver = world["sender"], world["receiver"]
    _write_draft(sender, "anchor-mismatch-topic")
    real_write_anchor = _upstream_delivery.write_anchor

    def _anchor_then_clobber(common_dir, filename, commit_sha, data):
        blob = real_write_anchor(common_dir, filename, commit_sha, data)
        ref = common_dir / "refs" / "coordinator" / "inbox" / filename / commit_sha
        ref.write_text("0" * 40 + "\n", encoding="utf-8")
        return blob

    monkeypatch.setattr(_upstream_delivery, "write_anchor", _anchor_then_clobber)

    result = _memo_send({"dry_run": False, "topic": "anchor-mismatch-topic"}, repo_root=sender)

    assert result["exit_code"] != 0, result
    assert result["acted"] == []
    assert "anchor" in result["failed"][0]["reason"]
    assert _pending_records(sender) == []
    assert _git(receiver, "for-each-ref", "refs/coordinator/inbox-pin") == ""


def test_a_failed_push_leaves_a_retryable_record_that_lands_on_the_next_tick(world) -> None:
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


def test_upstream_moving_after_the_send_is_rebuilt_onto_and_pushed(world) -> None:
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
