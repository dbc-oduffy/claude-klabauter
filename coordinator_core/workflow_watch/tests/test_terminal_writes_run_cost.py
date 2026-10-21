"""_watch writes run-cost.json once after the terminal line and names ratchet breaches."""

from __future__ import annotations

import json

from coordinator_core import workflow_watch as ww
from coordinator_core.workflow_watch import run_report


class _Rec:
    status = "completed"


class _Watcher:
    def __init__(self, *_a):
        pass

    def check_record(self):
        return _Rec()


def _run(monkeypatch, tmp_path, capsys, write_record):
    monkeypatch.setattr(ww, "TerminalWatcher", _Watcher)
    monkeypatch.setattr(ww, "stamp_terminal", lambda *a: None)
    monkeypatch.setattr(run_report, "write_record", write_record)
    journal = tmp_path / "journal.jsonl"
    journal.write_text("", encoding="utf-8")
    rc = ww._watch("t", str(journal), "tid", 0.0, 1.0)
    return rc, capsys.readouterr().out


def _writer(tmp_path, verdicts):
    calls = []

    def write_record(journal_path):
        calls.append(journal_path)
        out = tmp_path / "run-cost.json"
        out.write_text(json.dumps({"ratchet": {m: {"verdict": v} for m, v in verdicts.items()}}))
        return str(out)

    write_record.calls = calls
    return write_record


def test_writes_once_and_prints_breach_after_terminal(monkeypatch, tmp_path, capsys):
    w = _writer(tmp_path, {"peak_context": "breach", "tool_calls": "ok", "max_same_file_reads": "breach"})
    rc, out = _run(monkeypatch, tmp_path, capsys, w)
    assert rc == 0
    assert out.splitlines() == ["terminal: completed", "cost: breach max_same_file_reads peak_context"]
    assert len(w.calls) == 1


def test_no_breach_prints_only_terminal(monkeypatch, tmp_path, capsys):
    rc, out = _run(monkeypatch, tmp_path, capsys, _writer(tmp_path, {"peak_context": "ok"}))
    assert rc == 0
    assert out == "terminal: completed\n"


def test_write_failure_keeps_exit_zero_and_silent(monkeypatch, tmp_path, capsys):
    def boom(_p):
        raise OSError("disk")

    rc, out = _run(monkeypatch, tmp_path, capsys, boom)
    assert rc == 0
    assert out == "terminal: completed\n"
