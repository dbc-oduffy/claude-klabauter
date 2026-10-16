"""run_report: per-leg cost, wall seconds from transcripts, and the over-cap section."""

import json
import time

from coordinator_core.workflow_watch import run_report


def _write_agent(run_dir, agent_id, *, agent_type, model, start_min, end_min, usages):
    meta = {"agentType": agent_type, "model": model}
    (run_dir / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta), encoding="utf-8")

    def stamp(minutes):
        return f"2026-10-16T10:{int(minutes) % 60:02d}:00Z" if minutes < 60 else \
            f"2026-10-16T11:{int(minutes) - 60:02d}:00Z"

    lines = [{"type": "user", "timestamp": stamp(start_min), "message": {"role": "user"}}]
    for i, usage in enumerate(usages):
        lines.append({"type": "assistant", "timestamp": stamp(end_min),
                      "message": {"id": f"m{i}", "usage": usage}})
    (run_dir / f"agent-{agent_id}.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def _usage(inp, create, read, out):
    return {"input_tokens": inp, "cache_creation_input_tokens": create,
            "cache_read_input_tokens": read, "output_tokens": out}


def _journal(run_dir, agents):
    lines = [{"type": "launched"}]
    for agent_id, label in agents:
        lines.append({"type": "started", "agentId": agent_id, "key": f"k{agent_id}", "label": label})
    path = run_dir / "journal.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return str(path)


def test_report_rows_and_over_cap_names_only_the_31_minute_row(tmp_path):
    _write_agent(tmp_path, "s1", agent_type="general-purpose", model="haiku", start_min=0,
                 end_min=1, usages=[_usage(2, 12000, 35000, 100), _usage(1, 500, 47000, 2400)])
    _write_agent(tmp_path, "w1", agent_type="coordinator:executor", model="sonnet", start_min=0,
                 end_min=31, usages=[_usage(2, 100, 20000, 50)])
    _write_agent(tmp_path, "w2", agent_type="coordinator:executor", model="sonnet", start_min=0,
                 end_min=5, usages=[_usage(2, 100, 20000, 50)])
    journal = _journal(tmp_path, [("s1", "stage"), ("w1", "work:A"), ("w2", "work:B")])

    report = run_report.build_report(journal)
    stage = report["rows"][0]
    assert stage["turn1_context"] == 47002
    assert stage["peak_context"] == 47501
    assert stage["output_tokens"] == 2500
    assert stage["turns"] == 2
    assert stage["agent_type"] == "general-purpose" and stage["model"] == "haiku"
    assert [r["label"] for r in report["over_cap"]] == ["work:A"]
    assert report["over_cap"][0]["wall_seconds"] == 31 * 60
    text = run_report.render(report)
    assert "work:A" in text.split("over 1800s")[1]
    assert "work:B" not in text.split("over 1800s")[1]


def test_missing_transcript_and_meta_is_unknown_not_a_crash(tmp_path):
    journal = _journal(tmp_path, [("ghost", "work:C")])
    report = run_report.build_report(journal)
    row = report["rows"][0]
    assert row["turn1_context"] is None and row["wall_seconds"] is None
    assert row["agent_type"] == "unknown"
    assert report["over_cap"] == []
    text = run_report.render(report)
    assert "unknown" in text and text.rstrip().endswith("none")


def test_main_json_and_missing_journal(tmp_path, capsys):
    _write_agent(tmp_path, "s1", agent_type="t", model="haiku", start_min=0, end_min=1,
                 usages=[_usage(1, 2, 3, 4)])
    journal = _journal(tmp_path, [("s1", "stage")])
    assert run_report.main(["--journal", journal, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["rows"][0]["turn1_context"] == 6
    assert run_report.main(["--journal", str(tmp_path / "nope.jsonl")]) == 2


def test_large_transcript_reads_head_and_tail_only_and_marks_partial(tmp_path):
    _write_agent(tmp_path, "w1", agent_type="t", model="m", start_min=0, end_min=1,
                 usages=[_usage(1, 1, 1, 1)])
    path = tmp_path / "agent-w1.jsonl"
    filler = json.dumps({"type": "user", "timestamp": "2026-10-16T10:10:00Z", "pad": "x" * 2000})
    with open(path, "a", encoding="utf-8") as handle:
        for _ in range(300):
            handle.write(filler + "\n")
        handle.write(json.dumps({"type": "assistant", "timestamp": "2026-10-16T11:00:00Z",
                                 "message": {"id": "last", "usage": _usage(1, 1, 1, 9)}}) + "\n")
    row = run_report.build_report(_journal(tmp_path, [("w1", "work:Z")]))["rows"][0]
    assert row["partial"] is True
    assert row["wall_seconds"] == 3600.0
    assert row["turn1_context"] == 3


def test_forty_agent_report_is_fast_and_spawns_nothing(tmp_path, monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    agents = []
    for i in range(40):
        _write_agent(tmp_path, f"a{i}", agent_type="t", model="m", start_min=0, end_min=2,
                     usages=[_usage(1, 2, 3, 4)] * 3)
        agents.append((f"a{i}", f"work:{i}"))
    journal = _journal(tmp_path, agents)
    began = time.process_time()
    report = run_report.build_report(journal)
    run_report.render(report)
    assert time.process_time() - began < 0.5
    assert len(report["rows"]) == 40
