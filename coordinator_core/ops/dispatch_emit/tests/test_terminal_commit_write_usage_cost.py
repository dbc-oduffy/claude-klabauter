"""terminal_commit._write_usage reads the run's own wf_ journal once per consumer, in proportion to the run."""

from __future__ import annotations

import builtins
import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_usage import RUN, SID, _stage
from coordinator_core.usage import run_locate


def _journal_reads(monkeypatch, wf: Path) -> list:
    reads: list = []
    journal = str(wf / "journal.jsonl")
    real_open = builtins.open
    real_read_text = Path.read_text

    def spy_open(file, *a, **kw):
        if str(file) == journal:
            reads.append("open")
        return real_open(file, *a, **kw)

    def spy_read_text(self, *a, **kw):
        if str(self) == journal:
            reads.append("read_text")
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(Path, "read_text", spy_read_text)
    return reads


def test_journal_read_once_per_consumer_after_locate(tmp_path, monkeypatch):
    root = _stage(tmp_path, monkeypatch)
    session = run_locate.session_dir_for(SID)
    wf = run_locate.locate_run_dir(session, RUN)
    assert wf is not None
    monkeypatch.setattr(run_locate, "locate_run_dir", lambda *_a, **_k: wf)
    reads = _journal_reads(monkeypatch, wf)

    tc._write_usage(root, SID, RUN)

    assert sorted(reads) == ["open", "read_text"]


def test_unlocated_run_opens_only_unsettled_heads_and_writes_nothing(tmp_path, monkeypatch):
    root = _stage(tmp_path, monkeypatch, ambiguous=True)
    opened: list = []
    real_open = builtins.open

    def spy_open(file, *a, **kw):
        if str(file).endswith(".jsonl") and "agent-" in str(file):
            opened.append(str(file))
        return real_open(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", spy_open)

    reply = tc._write_usage(root, SID, RUN)

    assert reply == {"usage_total": None, "usage_reason": "run dir not located"}
    assert {Path(p).name for p in opened} <= {"agent-c1.jsonl"}
    assert not list(root.rglob("usage.json"))


def test_usage_reads_only_the_runs_own_dir(tmp_path, monkeypatch):
    root = _stage(tmp_path, monkeypatch)
    session = run_locate.session_dir_for(SID)
    other = session / "subagents" / "workflows" / "wf_other"
    other.mkdir()
    (other / "journal.jsonl").write_text(
        json.dumps({"type": "started", "agentId": "z9", "label": "z"}) + "\n", encoding="utf-8")
    reply = tc._write_usage(root, SID, RUN)
    body = json.loads(next(root.rglob("usage.json")).read_text(encoding="utf-8"))

    assert reply["usage_total"]["total_tokens"] == 1500
    assert "z9" not in {a["agent_id"] for a in body["agents"]}
