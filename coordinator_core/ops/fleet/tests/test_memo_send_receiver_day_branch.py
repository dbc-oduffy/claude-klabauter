"""memo.send lands a receiver delivery on the receiver's day branch (DR-214 A3).

Receivers are real tmp_path git repos on `main` with a bare origin; the push is driven through
`push_cadence.on_idle_tick`, never inline.
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


def _with_origin(tmp_path: Path, root: Path, name: str) -> Path:
    bare = tmp_path / f"{name}-origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    _git(root, "remote", "add", "origin", str(bare))
    _git(root, "push", "origin", "main")
    return bare


def _tick() -> bool:
    return push_cadence.on_idle_tick(
        served_repos=lambda: [], clock=lambda: 0.0, interval_secs=0.0
    ) or push_cadence.on_idle_tick(
        served_repos=lambda: [], clock=lambda: 1.0, interval_secs=0.0
    )


def _setup(tmp_path, monkeypatch):
    sender = _make_sender_git_repo(tmp_path)
    receiver = _make_receiver_git_repo(tmp_path)
    bare = _with_origin(tmp_path, receiver, "recv")
    monkeypatch.setenv("CLAUDE_HOME", str(_make_claude_home(tmp_path, {"project_rag": receiver})))
    return sender, receiver, bare


def test_main_without_day_branch_mints_switches_and_pushes(tmp_path, monkeypatch):
    sender, receiver, bare = _setup(tmp_path, monkeypatch)
    main_before = _git(receiver, "rev-parse", "refs/heads/main")
    _write_draft(sender, "t-a")

    result = _memo_send({"dry_run": False, "topic": "t-a"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    day = _day_branch(receiver)
    assert _git(receiver, "rev-parse", "--abbrev-ref", "HEAD") == day
    assert _git(receiver, "rev-parse", "refs/heads/main") == main_before
    assert _git(receiver, "rev-parse", f"refs/heads/{day}^") == main_before
    assert list((receiver / "cross-repo" / "inbox").glob("*t-a.md"))
    assert (receiver, day) in push_cadence.foreign_deliveries()

    assert _tick()
    assert _git(bare, "rev-parse", f"refs/heads/{day}") == _git(receiver, "rev-parse", "HEAD")
    assert _git(bare, "rev-parse", "refs/heads/main") == main_before


def test_day_branch_at_head_is_switched_and_committed(tmp_path, monkeypatch):
    sender, receiver, _bare = _setup(tmp_path, monkeypatch)
    day = _day_branch(receiver)
    _git(receiver, "branch", day)
    _write_draft(sender, "t-b")

    result = _memo_send({"dry_run": False, "topic": "t-b"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    assert _git(receiver, "rev-parse", "--abbrev-ref", "HEAD") == day
    assert _git(receiver, "rev-parse", f"{day}^") == _git(receiver, "rev-parse", "main")


def test_day_branch_elsewhere_commits_ref_direct_without_touching_checkout(tmp_path, monkeypatch):
    sender, receiver, bare = _setup(tmp_path, monkeypatch)
    day = _day_branch(receiver)
    _git(receiver, "checkout", "-q", "-b", day)
    (receiver / "other.txt").write_text("x")
    _git(receiver, "add", "other.txt")
    _git(receiver, "commit", "-q", "-m", "day work")
    _git(receiver, "checkout", "-q", "main")
    main_before = _git(receiver, "rev-parse", "main")
    day_before = _git(receiver, "rev-parse", day)
    index_before = (receiver / ".git" / "index").read_bytes()
    _write_draft(sender, "t-c")

    result = _memo_send({"dry_run": False, "topic": "t-c"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    assert _git(receiver, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert _git(receiver, "rev-parse", "main") == main_before
    assert (receiver / ".git" / "index").read_bytes() == index_before
    assert not list((receiver / "cross-repo" / "inbox").glob("*t-c.md"))
    assert _git(receiver, "status", "--porcelain") == ""
    assert _git(receiver, "rev-parse", f"{day}^") == day_before
    assert "t-c.md" in _git(receiver, "ls-tree", "--name-only", f"{day}:cross-repo/inbox")
    assert (receiver, day) in push_cadence.foreign_deliveries()

    assert _tick()
    assert _git(bare, "rev-parse", f"refs/heads/{day}") == _git(receiver, "rev-parse", day)
    assert _git(bare, "rev-parse", "refs/heads/main") == main_before


def test_ref_direct_collision_is_refused_against_the_landing_tree(tmp_path, monkeypatch):
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


def test_dry_run_reports_landing_without_minting_or_switching(tmp_path, monkeypatch):
    sender, receiver, _bare = _setup(tmp_path, monkeypatch)
    _write_draft(sender, "t-e")

    result = _memo_send({"dry_run": True, "topic": "t-e"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    assert _git(receiver, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert _git(receiver, "branch", "--list", _day_branch(receiver)) == ""
    assert "switched" in str(result)


def test_cc_arm_lands_on_the_day_branch(tmp_path, monkeypatch):
    sender = _make_sender_git_repo(tmp_path)
    to_repo = _make_receiver_git_repo(tmp_path, name="to-repo")
    cc_repo = _make_receiver_git_repo(tmp_path, name="cc-repo")
    cc_bare = _with_origin(tmp_path, cc_repo, "cc")
    _with_origin(tmp_path, to_repo, "to")
    monkeypatch.setenv(
        "CLAUDE_HOME",
        str(_make_claude_home(tmp_path, {"project_rag": to_repo, "example_cockpit_repo": cc_repo})),
    )
    main_before = _git(cc_repo, "rev-parse", "main")
    _write_draft_with_cc(sender, "t-f", to="example-retrieval-repo-em", cc="example-cockpit-repo-em")

    result = _memo_send({"dry_run": False, "topic": "t-f"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    day = _day_branch(cc_repo)
    assert _git(cc_repo, "rev-parse", "--abbrev-ref", "HEAD") == day
    assert _git(cc_repo, "rev-parse", "main") == main_before
    assert (cc_repo, day) in push_cadence.foreign_deliveries()

    assert _tick()
    assert _git(cc_bare, "rev-parse", f"refs/heads/{day}") == _git(cc_repo, "rev-parse", "HEAD")
