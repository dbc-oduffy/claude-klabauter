"""test_workflow_journal.py -- pins workflow-journal's per-agent summary shape.

Loaded by file path since `coordinator/bin/workflow-journal.py` is a
standalone CLI, not a package module.

Run:
    python -m pytest coordinator/bin/tests/test_workflow_journal.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_workflow_journal():
    path = _BIN_DIR / "workflow-journal.py"
    loader = importlib.machinery.SourceFileLoader("workflow_journal_cli", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


wj = _load_workflow_journal()


def _write_journal(tmp_path, events):
    path = tmp_path / "journal.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


def test_summarize_orders_started_then_result(tmp_path):
    events = [
        {"agentId": "a1", "type": "started", "label": "executor-C1"},
        {"agentId": "a1", "type": "result", "result": "DONE: C1\nfiles changed: x.py"},
    ]
    journal = _write_journal(tmp_path, events)
    summary = wj.summarize(str(journal))
    assert summary["a1"]["label"] == "executor-C1"
    assert summary["a1"]["status"] == "result"
    assert summary["a1"]["result"] == "DONE: C1"


def test_failed_agent_reports_failed_status(tmp_path):
    events = [
        {"agentId": "a2", "type": "started", "label": "reviewer-slice-1"},
        {"agentId": "a2", "type": "failed", "error": "timeout after 900s"},
    ]
    journal = _write_journal(tmp_path, events)
    summary = wj.summarize(str(journal))
    assert summary["a2"]["status"] == "FAILED"
    assert summary["a2"]["result"] == "timeout after 900s"


def test_malformed_line_and_terminal_stamp_are_skipped_not_fatal(tmp_path):
    journal = tmp_path / "journal.jsonl"
    journal.write_text(
        "not json at all\n"
        + json.dumps({"type": "terminal", "source": "workflow_watch", "status": "committed"})
        + "\n"
        + json.dumps({"agentId": "a1", "type": "started", "label": "x"})
        + "\n",
        encoding="utf-8",
    )
    summary = wj.summarize(str(journal))
    assert list(summary.keys()) == ["a1"]


def test_cli_label_filter_and_missing_label_exit_code(tmp_path, capsys):
    events = [
        {"agentId": "a1", "type": "started", "label": "executor-C1"},
        {"agentId": "a1", "type": "result", "result": "DONE: C1"},
        {"agentId": "a2", "type": "started", "label": "executor-C2"},
    ]
    journal = _write_journal(tmp_path, events)

    rc = wj.main(["workflow-journal", str(journal), "--label", "executor-C1"])
    out = capsys.readouterr().out
    assert rc == wj.EXIT_OK
    assert "executor-C1  result: DONE: C1" in out
    assert "executor-C2" not in out

    rc_missing = wj.main(["workflow-journal", str(journal), "--label", "no-such-label"])
    assert rc_missing == wj.EXIT_USAGE
