"""A stalled agent's journal key holds no result, so a resume re-dispatches it rather than replaying."""

from __future__ import annotations

import json
import os

from coordinator_core.workflow_watch import stall
from coordinator_core.workflow_watch.stamp import stamp_terminal
from coordinator_core.workflow_watch.terminal import TerminalRecord

NOW = 2_000_000_000.0
OLD = NOW - stall.STALL_BOUND_SECONDS - 600


def _journal(tmp_path):
    run_dir = tmp_path / "wf_cache"
    run_dir.mkdir()
    events = [
        {"type": "started", "agentId": "a1", "label": "L-a1", "key": "K1"},
        {"type": "result", "agentId": "a1", "key": "K1", "result": "ok"},
        {"type": "started", "agentId": "a2", "label": "L-a2", "key": "K2"},
    ]
    journal = run_dir / "journal.jsonl"
    journal.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    os.utime(journal, (OLD, OLD))
    transcript = run_dir / "agent-a2.jsonl"
    transcript.write_text('{"type":"assistant"}\n', encoding="utf-8")
    os.utime(transcript, (OLD, OLD))
    return journal


def _unsettled(journal):
    return stall._unsettled(journal.read_text(encoding="utf-8"))


def test_scan_names_k2_and_writes_nothing(tmp_path):
    journal = _journal(tmp_path)
    before = journal.read_bytes()
    found = stall.scan_for_stall(str(journal), NOW)
    assert found is not None and found.agent_id == "a2"
    assert journal.read_bytes() == before


def test_stamp_terminal_does_not_settle_k2(tmp_path):
    journal = _journal(tmp_path)
    assert stamp_terminal(str(journal), "task1", TerminalRecord(status="killed")) is True
    stamped = json.loads(journal.read_text(encoding="utf-8").strip().split("\n")[-1])
    assert "key" not in stamped and stamped["source"] == "workflow_watch"
    assert _unsettled(journal) == [("a2", "L-a2")]


def test_k1_is_settled_so_resume_replays_it(tmp_path):
    journal = _journal(tmp_path)
    assert ("a1", "L-a1") not in _unsettled(journal)


def test_resumed_started_then_result_settles_k2(tmp_path):
    journal = _journal(tmp_path)
    with open(journal, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "started", "agentId": "a3", "label": "L-a3", "key": "K2"}) + "\n")
    assert _unsettled(journal) == [("a3", "L-a3")]
    with open(journal, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "result", "agentId": "a3", "key": "K2", "result": "ok"}) + "\n")
    assert _unsettled(journal) == []
