"""memo.send at default params lands on the receiver repo's live work branch, never a sender-box branch.

The op handler is driven with `{"dry_run": False, "topic": ...}` against a tmp_path receiver clone and
bare origin. The clone's HEAD sits on a stale `work/mac-mini/<day>` branch (or on `main`) while
`origin/work/machine-a/<day>` carries the freshest committer time; the push-pending record must name
the machine-a branch and no ref named for the sending box may appear.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import machine_resolver
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

DAY = "2026-10-20"
STALE = f"work/mac-mini/{DAY}"
LIVE = f"work/machine-a/{DAY}"
SENDER_BOX = "senderbox"
OLD, NEW = "2026-10-20T08:00:00Z", "2026-10-20T20:00:00Z"


def _git(cwd, *args, **kw) -> str:
    out = _run_git(cwd, *args, **kw).stdout
    return (out.decode() if isinstance(out, bytes) else out).strip()


@pytest.fixture(autouse=True)
def _cadence(monkeypatch):
    push_cadence.reset_cadence_for_test()
    monkeypatch.setattr(push_cadence, "machine_profile", lambda: "author")
    monkeypatch.setattr(machine_resolver, "compute_machine", lambda *a, **k: SENDER_BOX)
    yield
    push_cadence.reset_cadence_for_test()


def _commit_at(root: Path, when: str, monkeypatch, msg: str) -> None:
    monkeypatch.setenv("GIT_COMMITTER_DATE", when)
    monkeypatch.setenv("GIT_AUTHOR_DATE", when)
    _git(root, "commit", "-q", "--allow-empty", "-m", msg)
    monkeypatch.delenv("GIT_COMMITTER_DATE")
    monkeypatch.delenv("GIT_AUTHOR_DATE")


def _cut_branch(root: Path, name: str, when: str, monkeypatch, *, push: bool, keep_local: bool) -> None:
    _git(root, "checkout", "-q", "-b", name, "main")
    _commit_at(root, when, monkeypatch, f"work on {name}")
    if push:
        _git(root, "push", "-q", "origin", name)
    if not keep_local:
        _git(root, "checkout", "-q", "main")
        _git(root, "branch", "-q", "-D", name)


def _setup(tmp_path, monkeypatch):
    sender = _make_sender_git_repo(tmp_path)
    receiver = _make_receiver_git_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_HOME", str(_make_claude_home(tmp_path, {"project_rag": receiver})))
    return sender, receiver


def _work_refs(root: Path) -> list[str]:
    return _git(root, "for-each-ref", "--format=%(refname)", "refs/heads/work", "refs/remotes/origin/work").splitlines()


def test_default_send_lands_on_the_fresher_live_branch_not_the_clone_head_branch(tmp_path, monkeypatch):
    sender, receiver = _setup(tmp_path, monkeypatch)
    _cut_branch(receiver, LIVE, NEW, monkeypatch, push=True, keep_local=False)
    _cut_branch(receiver, STALE, OLD, monkeypatch, push=False, keep_local=True)
    head_before = _git(receiver, "rev-parse", "HEAD")
    live_tip = _git(receiver, "rev-parse", f"refs/remotes/origin/{LIVE}")
    _write_draft(sender, "t-live")

    result = _memo_send({"dry_run": False, "topic": "t-live"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    sha = result["acted"][0]["delivery_commit_sha"]
    records = _pending_records(sender)
    assert [(r["commit_sha"], r["branch"]) for r in records] == [(sha, LIVE)]
    assert _git(receiver, "rev-parse", f"{sha}^") == live_tip
    assert _git(receiver, "symbolic-ref", "HEAD") == f"refs/heads/{STALE}"
    assert _git(receiver, "rev-parse", "HEAD") == head_before
    assert not [r for r in _work_refs(receiver) if f"/{SENDER_BOX}/" in r]


def test_default_send_from_a_main_clone_mints_no_sender_box_ref_and_keeps_head(tmp_path, monkeypatch):
    sender, receiver = _setup(tmp_path, monkeypatch)
    _cut_branch(receiver, LIVE, NEW, monkeypatch, push=True, keep_local=False)
    refs_before = _work_refs(receiver)
    head_before = _git(receiver, "rev-parse", "HEAD")
    _write_draft(sender, "t-main")

    result = _memo_send({"dry_run": False, "topic": "t-main"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    assert [r["branch"] for r in _pending_records(sender)] == [LIVE]
    assert _work_refs(receiver) == refs_before
    assert not [r for r in _work_refs(receiver) if f"/{SENDER_BOX}/" in r]
    assert _git(receiver, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert _git(receiver, "rev-parse", "HEAD") == head_before


def test_a_fresher_clone_head_branch_still_wins_so_the_rule_is_recency_not_remote_preference(tmp_path, monkeypatch):
    sender, receiver = _setup(tmp_path, monkeypatch)
    _cut_branch(receiver, LIVE, OLD, monkeypatch, push=True, keep_local=False)
    _cut_branch(receiver, STALE, NEW, monkeypatch, push=False, keep_local=True)
    _write_draft(sender, "t-fresh")

    result = _memo_send({"dry_run": False, "topic": "t-fresh"}, repo_root=sender)

    assert result["exit_code"] == 0, result
    assert [r["branch"] for r in _pending_records(sender)] == [STALE]
