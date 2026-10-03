"""memo.send prompts once when the receiver holds a gate keyed on the answered thread."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from coordinator_core.ops.fleet.memo_send import _memo_send, _parked_thread_gate
from coordinator_core.ops.fleet.tests.test_memo_send import (
    _make_claude_home,
    _make_receiver_git_repo,
    _make_sender_git_repo,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

THREAD = "2026-09-01-the-question"


def _plan(receiver: Path, name: str, *, owner="claude-klabauter-engine", cleared=False,
          kind="memo-thread", thread=THREAD) -> None:
    plans = receiver / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / name).write_text(
        "# Plan\n\n```yaml plan-tasks\n"
        "- id: R1\n"
        "  external_gate:\n"
        "    - requires: landed-work\n"
        f"      owner_repo: {owner}\n"
        f"      cleared: {'true' if cleared else 'false'}\n"
        "      closure_key:\n"
        f"        kind: {kind}\n"
        f"        id: {thread}\n"
        "```\n",
        encoding="utf-8",
    )


def _draft(sender: Path, topic: str, *, discharges: bool = False) -> None:
    outbox = sender / "state" / "memo-outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    block = (
        "discharges:\n  - kind: memo-thread\n    id: " + THREAD + "\n"
        if discharges else ""
    )
    (outbox / f"{topic}.md").write_text(
        "---\n"
        'title: "A reply"\n'
        'from: "claude-klabauter-engine"\n'
        'to: "example-retrieval-repo-em"\n'
        "created: 2026-09-11\n"
        "status: draft\n"
        "delivery_mode: receiver-repo\n"
        'summary: "a one-line summary"\n'
        'kind: "ask"\n'
        'sent_by: "session-a"\n'
        f'in_reply_to: "{THREAD}.md"\n'
        f"{block}"
        "---\n\nBody prose.\n",
        encoding="utf-8", newline="\n",
    )


@pytest.fixture()
def pair(tmp_path, monkeypatch):
    sender = _make_sender_git_repo(tmp_path)
    receiver = _make_receiver_git_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_HOME", str(_make_claude_home(tmp_path, {"project_rag": receiver})))
    return sender, receiver


def _inbox(receiver: Path) -> list:
    return [p for p in (receiver / "cross-repo" / "inbox").glob("*.md")]


def test_matching_gate_refuses_first_attempt_then_sends(pair, caplog):
    sender, receiver = pair
    _plan(receiver, "2026-09-02-p.md")
    _draft(sender, "t1")
    first = _memo_send({"dry_run": False, "topic": "t1"}, repo_root=sender)
    assert first["exit_code"] == 1
    assert "emit_discharge" in caplog.text and "R1" in caplog.text
    assert "2026-09-02-p.md" in caplog.text
    assert _inbox(receiver) == []
    second = _memo_send({"dry_run": False, "topic": "t1"}, repo_root=sender)
    assert second["exit_code"] == 0, second
    assert len(_inbox(receiver)) == 1


def test_receiver_plans_are_never_written(pair):
    sender, receiver = pair
    _plan(receiver, "2026-09-02-p.md")
    before = (receiver / "docs" / "plans" / "2026-09-02-p.md").read_bytes()
    _draft(sender, "t1")
    _memo_send({"dry_run": False, "topic": "t1"}, repo_root=sender)
    assert (receiver / "docs" / "plans" / "2026-09-02-p.md").read_bytes() == before


def test_dry_run_neither_warns_nor_spends_the_ack(pair):
    sender, receiver = pair
    _plan(receiver, "2026-09-02-p.md")
    _draft(sender, "t1")
    assert _memo_send({"dry_run": True, "topic": "t1"}, repo_root=sender)["exit_code"] == 0
    assert _memo_send({"dry_run": False, "topic": "t1"}, repo_root=sender)["exit_code"] == 1


def test_draft_with_discharges_sends_without_warning(pair):
    sender, receiver = pair
    _plan(receiver, "2026-09-02-p.md")
    _draft(sender, "t1", discharges=True)
    assert _memo_send({"dry_run": False, "topic": "t1"}, repo_root=sender)["exit_code"] == 0


@pytest.mark.parametrize("variant", [
    {"cleared": True}, {"owner": "someone-else"}, {"kind": "deliverable"},
    {"thread": "2026-09-01-another-thread"},
])
def test_non_matching_gate_sends_without_warning(pair, variant):
    sender, receiver = pair
    _plan(receiver, "2026-09-02-p.md", **variant)
    _draft(sender, "t1")
    assert _memo_send({"dry_run": False, "topic": "t1"}, repo_root=sender)["exit_code"] == 0


def test_no_receiver_plans_sends_without_warning(pair):
    sender, _ = pair
    _draft(sender, "t1")
    assert _memo_send({"dry_run": False, "topic": "t1"}, repo_root=sender)["exit_code"] == 0


def test_scan_over_568_plan_receiver_is_under_200ms(tmp_path):
    receiver = tmp_path / "big"
    for i in range(567):
        plans = receiver / "docs" / "plans"
        plans.mkdir(parents=True, exist_ok=True)
        (plans / f"2026-01-01-filler-{i}.md").write_text(
            "# Filler\n\n" + "prose line\n" * 400, encoding="utf-8",
        )
    _plan(receiver, "2026-09-02-p.md")
    start = time.process_time()
    hit = _parked_thread_gate(receiver, THREAD, {"claude-klabauter-engine"})
    elapsed_ms = (time.process_time() - start) * 1000
    assert hit == ("2026-09-02-p.md", "R1")
    assert elapsed_ms < 200, elapsed_ms
