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


def _agent(tmp_path, agent_id, phase, model, records):
    (tmp_path / f"agent-{agent_id}.meta.json").write_text(
        json.dumps({"workflowPhase": phase, "model": model, "agentType": "x"}), encoding="utf-8")
    if records is not None:
        (tmp_path / f"agent-{agent_id}.jsonl").write_text(
            "not json\n" + "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _rec(msg_id, inp, cc, out, cr):
    return {"type": "assistant", "message": {"id": msg_id, "usage": {
        "input_tokens": inp, "cache_creation_input_tokens": cc,
        "output_tokens": out, "cache_read_input_tokens": cr}}}


def test_phase_tokens_dedups_by_message_id_and_groups_phase(tmp_path):
    journal = _write_journal(tmp_path, [])
    _agent(tmp_path, "a", "Prep gate", "sonnet", [_rec("m1", 1, 2, 3, 10), _rec("m1", 1, 2, 3, 10)])
    _agent(tmp_path, "b", "Prep gate", "haiku", [_rec("m2", 0, 0, 4, 5)])
    phases = wj.phase_tokens(str(journal))
    assert phases["Prep gate"] == {
        "agents": 2, "models": ["sonnet", "haiku"], "fresh": 10, "cache_read": 15}


def test_phase_tokens_skips_meta_without_transcript(tmp_path):
    journal = _write_journal(tmp_path, [])
    _agent(tmp_path, "a", "stage", "haiku", None)
    assert wj.phase_tokens(str(journal)) == {}


def test_cli_tokens_prints_table_exit_zero(tmp_path, capsys):
    journal = _write_journal(tmp_path, [])
    _agent(tmp_path, "a", "stage", "haiku", [_rec("m1", 1, 1, 1, 7)])
    assert wj.main(["workflow-journal", str(journal), "--tokens"]) == wj.EXIT_OK
    out = capsys.readouterr().out
    assert "stage" in out and "TOTAL" in out and "7" in out


def _timed(tmp_path, agent_id, phase, start, end):
    stamp = lambda s: f"2026-01-01T00:00:{s:02d}Z"
    _agent(tmp_path, agent_id, phase, "m", [
        {"type": "user", "timestamp": stamp(start)},
        {"type": "assistant", "timestamp": stamp(end), "message": {}},
    ])


def test_critical_path_three_serial_agents_depth_three(tmp_path):
    journal = _write_journal(tmp_path, [{"agentId": "a", "type": "started", "label": "plan"}])
    _timed(tmp_path, "a", "Plan", 0, 10)
    _timed(tmp_path, "b", "Review", 10, 20)
    _timed(tmp_path, "c", "Execute", 25, 40)
    r = wj.critical_path(str(journal))
    assert (r["legs"], r["depth"], r["span"], r["unplaced"]) == (3, 3, 40.0, [])
    assert r["chain"] == [("plan", "Plan"), ("b", "Review"), ("c", "Execute")]


def test_critical_path_overlap_shortens_depth(tmp_path):
    journal = _write_journal(tmp_path, [])
    _timed(tmp_path, "a", "Review", 0, 20)
    _timed(tmp_path, "b", "Premise", 5, 15)
    _timed(tmp_path, "c", "Execute", 30, 40)
    r = wj.critical_path(str(journal))
    assert (r["legs"], r["depth"]) == (3, 2)


def test_critical_path_unreadable_transcript_is_unplaced(tmp_path, capsys):
    journal = _write_journal(tmp_path, [])
    _timed(tmp_path, "a", "Plan", 0, 10)
    _agent(tmp_path, "b", "Review", "m", None)
    r = wj.critical_path(str(journal))
    assert (r["legs"], r["depth"], r["unplaced"]) == (2, 1, ["b"])
    assert wj.main(["workflow-journal", str(journal), "--critical-path"]) == wj.EXIT_OK
    out = capsys.readouterr().out
    assert "agent legs: 2" in out and "critical-path depth: 1" in out and "unplaced" in out


def _tool_rec(msg_id, out, tools):
    return {"type": "assistant", "message": {
        "id": msg_id,
        "content": [{"type": "tool_use", "id": t_id, "name": name} for t_id, name in tools],
        "usage": {"output_tokens": out}}}


def _census_run(root, run, events, transcripts):
    d = root / "proj" / "sess" / "subagents" / "workflows" / run
    d.mkdir(parents=True)
    (d / "journal.jsonl").write_text(
        "garbage\n" + "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    for agent_id, records in transcripts.items():
        (d / f"agent-{agent_id}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_census_folds_by_normalised_label_across_journals(tmp_path):
    _census_run(tmp_path, "w1", [
        {"agentId": "a1", "type": "started", "label": "stage:x1"},
        {"agentId": "a2", "type": "started", "label": "stage:y2"},
    ], {
        "a1": [_tool_rec("m1", 10, [("t1", "Bash")]),
               _tool_rec("m1", 10, [("t1", "Bash")])],
        "a2": [_tool_rec("m2", 30, [("t2", "Bash"), ("t3", "Read")])],
    })
    _census_run(tmp_path, "w2", [
        {"agentId": "a3", "type": "started", "label": "plan"},
    ], {"a3": [_tool_rec("m3", 5, [])]})

    labels = wj.census(tmp_path)
    assert labels["stage"]["runs"] == 2
    assert labels["stage"]["fresh"] == 40
    assert labels["stage"]["tool_calls"] == 3
    assert dict(labels["stage"]["tools"]) == {"Bash": 2, "Read": 1}
    assert labels["plan"]["runs"] == 1 and labels["plan"]["tool_calls"] == 0


def test_cli_census_prints_one_line_per_label(tmp_path, capsys):
    _census_run(tmp_path, "w1", [{"agentId": "a1", "type": "started", "label": "stage:x"}],
                {"a1": [_tool_rec("m1", 8, [("t1", "Bash")])]})
    assert wj.main(["workflow-journal", "--census", "--root", str(tmp_path), "--since", "30"]) == wj.EXIT_OK
    out = capsys.readouterr().out
    assert "stage  runs=1  avg_tokens=8  avg_tool_calls=1.0  tools: Bash=1" in out


def test_census_since_skips_old_journals(tmp_path):
    import os
    _census_run(tmp_path, "w1", [{"agentId": "a1", "type": "started", "label": "old"}],
                {"a1": [_tool_rec("m1", 1, [])]})
    journal = next(tmp_path.glob("*/*/subagents/workflows/*/journal.jsonl"))
    os.utime(journal, (1, 1))
    assert wj.census(tmp_path, since_days=30) == {}


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
