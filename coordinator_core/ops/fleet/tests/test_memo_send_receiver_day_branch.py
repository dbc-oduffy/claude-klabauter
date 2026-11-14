"""memo.send delivers onto the receiver's upstream tip for its day branch (DR-214 A3).

Receivers are real tmp_path git repos on `main` with a bare origin; the delivery never moves the
receiver's HEAD or refs, and the push is driven through `push_cadence.on_idle_tick` from the
sender's push-pending record, never inline.
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
from coordinator_core.ops.fleet.tests.test_memo_send_cc_delivery import _write_draft_with_cc
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


def _day_branch(root: Path) -> str:
    return f"work/{machine_resolver.compute_machine()}/{daily_day.local_day(str(root))}"


def _origin_of(root: Path) -> Path:
    return root.parent / f"{root.name}-origin.git"


def _tick(sender: Path) -> bool:
    return push_cadence.on_idle_tick(
        served_repos=lambda: [sender], clock=lambda: 0.0, interval_secs=0.0
    ) or push_cadence.on_idle_tick(
        served_repos=lambda: [sender], clock=lambda: 1.0, interval_secs=0.0
    )


def _setup(tmp_path, monkeypatch):
    sender = _make_sender_git_repo(tmp_path)
    receiver = _make_receiver_git_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_HOME", str(_make_claude_home(tmp_path, {"project_rag": receiver})))
    return sender, receiver, _origin_of(receiver)


def _checkout_state(root: Path) -> tuple:
    return (
        _git(root, "rev-parse", "HEAD"),
        _git(root, "symbolic-ref", "HEAD"),
        (root / ".git" / "index").read_bytes(),
    )


def test_main_without_day_branch_neither_mints_nor_switches_and_pushes(tmp_path, monkeypatch):
    sender, receiver, bare = _setup(tmp_path, monkeypatch)
    main_before = _git(receiver, "rev-parse", "refs/heads/main")
    state_before = _checkout_state(receiver)
    _write_draft(sender, "t-a")

    result = _memo_send({"dry_run": False, "topic": "t-a"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    day = _day_branch(receiver)
    sha = result["acted"][0]["delivery_commit_sha"]
    assert _checkout_state(receiver) == state_before
    assert _git(receiver, "branch", "--list", day) == ""
    assert _git(receiver, "rev-parse", "refs/heads/main") == main_before
    assert _git(receiver, "rev-parse", f"{sha}^") == main_before
    assert not list((receiver / "cross-repo" / "inbox").glob("*t-a.md"))
    assert [r["commit_sha"] for r in _pending_records(sender)] == [sha]

    assert _tick(sender)
    assert _git(bare, "rev-parse", f"refs/heads/{day}") == sha
    assert _git(bare, "rev-parse", "refs/heads/main") == main_before
    assert _pending_records(sender) == []


def test_day_branch_at_head_is_delivered_onto_without_moving_it(tmp_path, monkeypatch):
    sender, receiver, bare = _setup(tmp_path, monkeypatch)
    day = _day_branch(receiver)
    _git(receiver, "checkout", "-q", "-b", day)
    _git(receiver, "push", "-q", "-u", "origin", day)
    day_before = _git(receiver, "rev-parse", day)
    state_before = _checkout_state(receiver)
    _write_draft(sender, "t-b")

    result = _memo_send({"dry_run": False, "topic": "t-b"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    sha = result["acted"][0]["delivery_commit_sha"]
    assert _checkout_state(receiver) == state_before
    assert _git(receiver, "rev-parse", day) == day_before
    assert _git(receiver, "rev-parse", f"{sha}^") == day_before

    assert _tick(sender)
    assert _git(bare, "rev-parse", f"refs/heads/{day}") == sha


def test_day_branch_elsewhere_delivers_without_touching_checkout(tmp_path, monkeypatch):
    sender, receiver, bare = _setup(tmp_path, monkeypatch)
    day = _day_branch(receiver)
    _git(receiver, "checkout", "-q", "-b", day)
    (receiver / "other.txt").write_text("x")
    _git(receiver, "add", "other.txt")
    _git(receiver, "commit", "-q", "-m", "day work")
    _git(receiver, "checkout", "-q", "main")
    main_before = _git(receiver, "rev-parse", "main")
    day_before = _git(receiver, "rev-parse", day)
    state_before = _checkout_state(receiver)
    _write_draft(sender, "t-c")

    result = _memo_send({"dry_run": False, "topic": "t-c"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    sha = result["acted"][0]["delivery_commit_sha"]
    assert _checkout_state(receiver) == state_before
    assert _git(receiver, "rev-parse", "main") == main_before
    assert _git(receiver, "rev-parse", day) == day_before
    assert not list((receiver / "cross-repo" / "inbox").glob("*t-c.md"))
    assert _git(receiver, "status", "--porcelain") == ""
    assert _git(receiver, "rev-parse", f"{sha}^") == day_before
    assert "t-c.md" in _git(receiver, "ls-tree", "--name-only", f"{sha}:cross-repo/inbox")

    assert _tick(sender)
    assert _git(bare, "rev-parse", f"refs/heads/{day}") == sha
    assert _git(bare, "rev-parse", "refs/heads/main") == main_before


def test_collision_is_refused_against_the_base_tree(tmp_path, monkeypatch):
    sender, receiver, _bare = _setup(tmp_path, monkeypatch)
    day = _day_branch(receiver)
    _write_draft(sender, "t-d")
    preview = _memo_send({"dry_run": True, "topic": "t-d"}, repo_root=sender)
    name = Path(str(preview["candidates"][0]["target_path"])).name
    _git(receiver, "checkout", "-q", "-b", day)
    (receiver / "cross-repo" / "inbox" / name).write_text("old\n")
    _git(receiver, "add", "-A")
    _git(receiver, "commit", "-q", "-m", "prior delivery")
    _git(receiver, "checkout", "-q", "main")
    day_before = _git(receiver, "rev-parse", day)

    result = _memo_send({"dry_run": False, "topic": "t-d"}, repo_root=sender)

    assert result["exit_code"] != 0
    assert "collision" in str(result)
    assert _git(receiver, "rev-parse", day) == day_before
    assert _pending_records(sender) == []


def test_dry_run_reports_the_upstream_tip_without_minting_or_switching(tmp_path, monkeypatch):
    sender, receiver, _bare = _setup(tmp_path, monkeypatch)
    _write_draft(sender, "t-e")

    result = _memo_send({"dry_run": True, "topic": "t-e"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    row = result["candidates"][0]
    assert row["landing_mode"] == "upstream-tip"
    assert row["landing_ref"] == f"refs/heads/{_day_branch(receiver)}"
    assert row["landing_base"] == _git(receiver, "rev-parse", "refs/heads/main")
    assert _git(receiver, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert _git(receiver, "branch", "--list", _day_branch(receiver)) == ""
    assert _pending_records(sender) == []


def test_a_receiver_without_a_push_remote_is_refused_and_owes_no_push(tmp_path, monkeypatch):
    sender, receiver, _bare = _setup(tmp_path, monkeypatch)
    _git(receiver, "remote", "remove", "origin")
    _write_draft(sender, "t-nr")

    result = _memo_send({"dry_run": False, "topic": "t-nr"}, repo_root=sender)

    assert result["exit_code"] != 0
    assert result["acted"] == []
    assert "no push remote" in result["failed"][0]["reason"]
    assert _pending_records(sender) == []
    assert (sender / "state" / "memo-outbox" / "t-nr.md").exists()


def test_cc_arm_delivers_onto_the_cc_receivers_tip(tmp_path, monkeypatch):
    sender = _make_sender_git_repo(tmp_path)
    to_repo = _make_receiver_git_repo(tmp_path, name="to-repo")
    cc_repo = _make_receiver_git_repo(tmp_path, name="cc-repo")
    cc_bare = _origin_of(cc_repo)
    monkeypatch.setenv(
        "CLAUDE_HOME",
        str(_make_claude_home(tmp_path, {"project_rag": to_repo, "example_cockpit_repo": cc_repo})),
    )
    main_before = _git(cc_repo, "rev-parse", "main")
    state_before = _checkout_state(cc_repo)
    _write_draft_with_cc(sender, "t-f", to="example-retrieval-repo-em", cc="example-cockpit-repo-em")

    result = _memo_send({"dry_run": False, "topic": "t-f"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    day = _day_branch(cc_repo)
    cc_sha = result["acted"][0]["cc_delivered"][0]["delivery_commit_sha"]
    assert _checkout_state(cc_repo) == state_before
    assert _git(cc_repo, "rev-parse", "main") == main_before
    assert cc_sha in {r["commit_sha"] for r in _pending_records(sender)}

    assert _tick(sender)
    assert _git(cc_bare, "rev-parse", f"refs/heads/{day}") == cc_sha
    assert _pending_records(sender) == []
