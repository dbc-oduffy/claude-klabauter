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


def test_split_model_usd_and_run_total_rendered(tmp_path):
    def agent(agent_id, model, usage):
        (tmp_path / f"agent-{agent_id}.jsonl").write_text(json.dumps(
            {"type": "assistant", "timestamp": "2026-10-16T10:00:00Z", "requestId": "r",
             "message": {"id": "m", "model": model, "usage": usage}}) + "\n", encoding="utf-8")

    agent("o1", "claude-opus-5-5", _usage(1000, 0, 0, 1000))
    agent("h1", "claude-haiku-5-5", _usage(1000, 0, 0, 1000))
    agent("x1", "claude-mystery-9", _usage(10, 0, 0, 10))
    report = run_report.build_report(_journal(tmp_path, [("o1", "a"), ("h1", "b"), ("x1", "c")]))
    opus, haiku, mystery = report["rows"]
    assert opus["model"] == "claude-opus-5-5" and opus["usd"] == 0.024
    assert haiku["usd"] == 0.0006
    assert opus["input_tokens"] == 1000 and opus["cache_read_input_tokens"] == 0
    assert mystery["usd"] is None and mystery["unpriced"] is True
    assert report["run_total"]["total_tokens"] == 4020
    assert abs(report["run_total"]["usd"] - 0.0246) < 1e-9
    text = run_report.render(report)
    assert "run total: 4020 tok, $0.0246" in text
    assert "claude-opus-5-5: 1 agent(s)" in text
    for col in ("cache_r", "cache_w", "est_$"):
        assert col in text


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


def _exec_agent(run_dir, agent_id, *, ctxs, reads=(), calls_per_turn=1):
    (run_dir / f"agent-{agent_id}.meta.json").write_text(
        json.dumps({"agentType": "coordinator:executor", "model": "sonnet"}), encoding="utf-8")
    lines = []
    for i, ctx in enumerate(ctxs):
        blocks = [{"type": "tool_use", "id": f"{agent_id}-{i}-{j}", "name": "Read",
                   "input": {"file_path": reads[i % len(reads)] if reads else "x.py"}}
                  for j in range(calls_per_turn)]
        lines.append({"type": "assistant", "timestamp": "2026-10-16T10:00:00Z", "requestId": f"r{i}",
                      "message": {"id": f"m{i}", "usage": _usage(ctx, 0, 0, 1), "content": blocks}})
    (run_dir / f"agent-{agent_id}.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def _phase_journal(run_dir, agents):
    lines = [{"type": "launched"}]
    for agent_id, label, phase in agents:
        event = {"type": "started", "agentId": agent_id, "label": label}
        if phase is not None:
            event["phase"] = phase
        lines.append(event)
    path = run_dir / "journal.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return str(path)


def _fixture_run(tmp_path):
    _write_agent(tmp_path, "p1", agent_type="t", model="m", start_min=0, end_min=1,
                 usages=[_usage(100, 0, 0, 0)])
    _write_agent(tmp_path, "p2", agent_type="t", model="m", start_min=0, end_min=1,
                 usages=[_usage(100, 0, 0, 0)])
    _exec_agent(tmp_path, "e1", ctxs=[1000, 5000, 3000], reads=["A.py", "a.py", "b.py"])
    return _phase_journal(tmp_path, [("p1", "plan", "Plan"), ("p2", "x", "plan"),
                                     ("e1", "work:C1", "Execute")])


def test_phase_split_groups_case_insensitively_and_unknown(tmp_path):
    _write_agent(tmp_path, "p1", agent_type="t", model="m", start_min=0, end_min=1,
                 usages=[_usage(300, 0, 0, 0)])
    _write_agent(tmp_path, "p2", agent_type="t", model="m", start_min=0, end_min=1,
                 usages=[_usage(100, 0, 0, 0)])
    _write_agent(tmp_path, "p3", agent_type="t", model="m", start_min=0, end_min=1,
                 usages=[_usage(600, 0, 0, 0)])
    journal = _phase_journal(tmp_path, [("p1", "a", "Plan"), ("p2", "b", "plan"), ("p3", "c", None)])
    split = run_report.build_report(journal)["phase_split"]
    assert split["plan"]["total_tokens"] == 400 and split["plan"]["share"] == 0.4
    assert split["unknown"]["total_tokens"] == 600 and split["unknown"]["share"] == 0.6


def test_executors_carry_full_read_counts(tmp_path):
    report = run_report.build_report(_fixture_run(tmp_path))
    assert [e["label"] for e in report["executors"]] == ["work:C1"]
    e = report["executors"][0]
    assert e["tool_calls"] == 3 and e["peak_context"] == 5000
    assert e["max_same_file_reads"] == 2 and e["partial"] is False


def test_write_record_ratchet_ok_and_breach(tmp_path):
    journal = _fixture_run(tmp_path)
    ratchet = tmp_path / "ratchet.json"
    ratchet.write_text(json.dumps({"executor_peak_context": 4000, "executor_tool_calls": 10,
                                   "executor_max_same_file_reads": 10}), encoding="utf-8")
    out = run_report.write_record(journal, str(ratchet))
    assert out == str(tmp_path / "run-cost.json")
    record = json.loads((tmp_path / "run-cost.json").read_text(encoding="utf-8"))
    assert {"phase_split", "executors", "run_total", "ratchet"} <= set(record)
    assert record["ratchet"]["executor_peak_context"] == {
        "ceiling": 4000, "observed": 5000, "verdict": "breach"}
    assert record["ratchet"]["executor_tool_calls"]["verdict"] == "ok"
    assert not list(tmp_path.glob("*.tmp-*"))


def test_tighten_lowers_and_never_raises(tmp_path):
    ratchet = tmp_path / "ratchet.json"
    ratchet.write_text(json.dumps({"executor_peak_context": 4000, "executor_tool_calls": 10,
                                   "executor_max_same_file_reads": 1}), encoding="utf-8")
    block = {"executor_peak_context": {"ceiling": 4000, "observed": 5000, "verdict": "breach"},
             "executor_tool_calls": {"ceiling": 10, "observed": 3, "verdict": "ok"},
             "executor_max_same_file_reads": {"ceiling": 1, "observed": 0, "verdict": "ok"}}
    after = run_report.tighten(block, str(ratchet))
    assert after == {"executor_peak_context": 4000, "executor_tool_calls": 3,
                     "executor_max_same_file_reads": 1}
    assert json.loads(ratchet.read_text(encoding="utf-8")) == after


def test_tracked_ratchet_at_or_below_measured_baseline():
    from coordinator_core.ops.dispatch_emit.executor_economy import MEASURED_BASELINE, RATCHET_METRICS

    ceilings = run_report.load_ratchet()
    assert set(ceilings) == set(RATCHET_METRICS)
    assert all(ceilings[m] <= MEASURED_BASELINE[m] for m in RATCHET_METRICS)


def test_main_record_writes_run_cost(tmp_path, capsys):
    journal = _fixture_run(tmp_path)
    assert run_report.main(["--journal", journal, "--record"]) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / "run-cost.json")
    assert (tmp_path / "run-cost.json").is_file()
