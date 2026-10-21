"""agent_usage.scan_run: per-agent four-field split, model tag, USD, rollups."""

import json

from coordinator_core.usage.agent_usage import scan_run

OPUS = "claude-opus-5-5-20260101"
HAIKU = "claude-haiku-5-5-20260101"


def _u(inp, out, read, write, speed=None):
    u = {"input_tokens": inp, "output_tokens": out,
         "cache_read_input_tokens": read, "cache_creation_input_tokens": write}
    if speed:
        u["speed"] = speed
    return u


def _agent(run_dir, agent_id, rows):
    """rows: (message id, requestId, model, usage) in file order."""
    lines = [{"type": "user", "message": {"role": "user"}}]
    for mid, rid, model, usage in rows:
        msg = {"id": mid, "usage": usage}
        if model:
            msg["model"] = model
        lines.append({"type": "assistant", "requestId": rid, "message": msg})
    (run_dir / f"agent-{agent_id}.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def _journal(run_dir, agents):
    lines = [{"type": "launched"}] + [
        {"type": "started", "agentId": a, "key": f"k{a}", "label": label} for a, label in agents]
    (run_dir / "journal.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def test_opus_44k_costs_more_than_three_haiku_185k(tmp_path):
    _agent(tmp_path, "o1", [("m1", "r1", OPUS, _u(10_000, 4_600, 30_000, 0))])
    for i in range(3):
        _agent(tmp_path, f"h{i}", [("m1", "r1", HAIKU, _u(2_000, 2_000, 57_933, 0))])
    _journal(tmp_path, [("o1", "opus"), ("h0", "h0"), ("h1", "h1"), ("h2", "h2")])

    res = scan_run(str(tmp_path))
    by_id = {a["agent_id"]: a for a in res["agents"]}
    assert by_id["o1"]["total_tokens"] == 44_600
    assert by_id["o1"]["model"] == OPUS
    haiku_tokens = sum(by_id[f"h{i}"]["total_tokens"] for i in range(3))
    assert haiku_tokens > 4 * by_id["o1"]["total_tokens"] - 1_000
    haiku_usd = sum(by_id[f"h{i}"]["usd"] for i in range(3))
    assert by_id["o1"]["usd"] > haiku_usd
    assert res["run_total"]["total_tokens"] == 44_600 + haiku_tokens
    assert abs(res["run_total"]["usd"] - (by_id["o1"]["usd"] + haiku_usd)) < 1e-9
    assert res["by_model"][OPUS]["agents"] == 1 and res["by_model"][HAIKU]["agents"] == 3
    assert by_id["o1"]["input_tokens"] == 10_000
    assert by_id["o1"]["cache_read_input_tokens"] == 30_000


def test_split_streamed_message_counts_once_last_row_wins(tmp_path):
    _agent(tmp_path, "a", [
        ("m1", "r1", OPUS, _u(100, 5, 0, 0)),
        ("m1", "r1", OPUS, _u(100, 50, 0, 0)),
        ("m2", "r2", OPUS, _u(10, 1, 0, 0)),
    ])
    _journal(tmp_path, [("a", "a")])
    row = scan_run(str(tmp_path))["agents"][0]
    assert row["input_tokens"] == 110 and row["output_tokens"] == 51


def test_unknown_model_is_unpriced_not_zero(tmp_path):
    _agent(tmp_path, "a", [("m1", "r1", "claude-mystery-9", _u(1_000, 0, 0, 0))])
    _agent(tmp_path, "b", [("m1", "r1", OPUS, _u(1_000_000, 0, 0, 0))])
    _journal(tmp_path, [("a", "a"), ("b", "b")])
    res = scan_run(str(tmp_path))
    a, b = res["agents"]
    assert a["unpriced"] is True and a["usd"] is None and a["total_tokens"] == 1_000
    assert b["unpriced"] is False and abs(b["usd"] - 4.0) < 1e-9
    assert res["run_total"]["unpriced_tokens"] == 1_000
    assert abs(res["run_total"]["usd"] - 4.0) < 1e-9
    assert res["run_total"]["total_tokens"] == 1_001_000


def test_fast_speed_doubles_and_meta_model_fallback(tmp_path):
    _agent(tmp_path, "a", [("m1", "r1", OPUS, _u(1_000_000, 0, 0, 0, speed="fast"))])
    _agent(tmp_path, "b", [("m1", "r1", None, _u(5, 5, 0, 0))])
    (tmp_path / "agent-b.meta.json").write_text(json.dumps({"model": "sonnet"}), encoding="utf-8")
    _journal(tmp_path, [("a", "a"), ("b", "b")])
    a, b = scan_run(str(tmp_path))["agents"]
    assert a["speed"] == "fast" and abs(a["usd"] - 8.0) < 1e-9
    assert b["model"] == "sonnet" and b["unpriced"] is True


def test_missing_transcript_and_journal_do_not_crash(tmp_path):
    _journal(tmp_path, [("ghost", "g")])
    res = scan_run(str(tmp_path))
    assert res["agents"][0]["total_tokens"] == 0 and res["agents"][0]["model"] is None
    empty = scan_run(str(tmp_path / "nowhere"))
    assert empty["agents"] == [] and empty["run_total"]["total_tokens"] == 0


def _tool_msg(mid, rid, blocks, usage):
    return {"type": "assistant", "requestId": rid,
            "message": {"id": mid, "model": OPUS, "usage": usage, "content": blocks}}


def _tu(tid, name, path=None):
    return {"type": "tool_use", "id": tid, "name": name,
            "input": {"file_path": path} if path else {"command": "x"}}


def test_tool_calls_peak_context_and_same_file_reads(tmp_path):
    lines = [
        _tool_msg("m1", "r1", [_tu("t1", "Read", "/a/File.py")], _u(10, 1, 100, 5)),
        _tool_msg("m1", "r1", [_tu("t1", "Read", "/a/File.py"), _tu("t2", "Bash")],
                  _u(10, 9, 100, 5)),
        _tool_msg("m2", "r2", [_tu("t3", "Read", "\\a\\file.py"), _tu("t4", "Read", "/a/b.py")],
                  _u(20, 1, 300, 40)),
        _tool_msg("m3", "r3", [{"type": "text", "text": "hi"}], _u(1, 1, 0, 0)),
    ]
    (tmp_path / "agent-a.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    _journal(tmp_path, [("a", "a")])
    row = scan_run(str(tmp_path))["agents"][0]
    assert row["tool_calls"] == 4
    assert row["peak_context"] == 360
    assert row["max_same_file_reads"] == 2


def test_missing_transcript_zeroes_new_fields(tmp_path):
    _journal(tmp_path, [("ghost", "g")])
    row = scan_run(str(tmp_path))["agents"][0]
    assert (row["tool_calls"], row["peak_context"], row["max_same_file_reads"]) == (0, 0, 0)


def test_messages_and_session_cost_advance_agree(tmp_path):
    from coordinator_core.usage import session_cost
    from coordinator_core.usage.agent_usage import _messages
    from coordinator_core.usage.price_table import estimate_usd

    _agent(tmp_path, "a", [
        ("m1", "r1", OPUS, _u(100, 5, 0, 0)),
        ("m1", "r1", OPUS, _u(100, 50, 10, 0)),
        ("m2", "r2", HAIKU, _u(2_000, 300, 500, 0)),
        (None, None, OPUS, _u(7, 7, 0, 0)),
    ])
    path = tmp_path / "agent-a.jsonl"
    expected = sum(estimate_usd(m["model"], m["usage"], m["usage"].get("speed"))
                   for m in _messages(str(path)))
    got = session_cost.advance(str(path), tmp_path / "cache.json", now=1e9, byte_budget=10**9)
    assert got["complete"] and abs(got["usd"] - expected) < 1e-9
