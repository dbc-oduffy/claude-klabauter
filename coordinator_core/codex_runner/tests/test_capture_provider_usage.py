import json
import subprocess

import pytest

from coordinator_core.codex_runner.capture_provider import spawn_collector


@pytest.mark.parametrize("transport", ["tungstenite::protocol: Received message ", "codex_api::sse::responses: SSE event: "])
def test_capture_preserves_terminal_usage_and_identity(transport):
    identity = {"fire_id": "fixture", "stage": "work:X1", "pid": 123}
    usage = {"input_tokens": 12400, "output_tokens": 1000,
             "input_tokens_details": {"cached_tokens": 2000, "cache_write_tokens": 400,
                                      "cache_write_tokens_details": {"ttl_12h_tokens": 100}}}
    counters = {"input_tokens": 12400, "output_tokens": 1000, "cached_input_tokens": 2000,
                "cache_write_input_tokens": 400, "ttl_12h_tokens": 100}
    lines = []
    expected = []
    for status, observed, retained in [
        ("completed", usage, counters), ("failed", None, None),
        ("incomplete", {"input_tokens": True, "output_tokens": -1, "input_tokens_details": ["text"]}, {}),
        ("failed", {"input_tokens": 0, "input_tokens_details": {"cache_write_tokens_details": "text"}}, {"input_tokens": 0}),
    ]:
        kind = "response." + status
        body = {"id": str(len(lines)), "model": "fixture-model"}
        response = dict(body, output="private content", usage=observed)
        line = "2026-11-24T12:00:00Z TRACE " + transport + json.dumps({"type": kind, "response": response})
        lines.append(line)
        if retained is not None:
            body["pricing_usage"] = retained
        expected.append(dict(identity, type=kind, response=body))
    lines.append(lines[0].replace(transport, "outbound: "))
    with spawn_collector(identity, subprocess.PIPE, subprocess.PIPE) as collector:
        stdout, _ = collector.communicate(("\n".join(lines) + "\n").encode(), timeout=3)
    assert collector.returncode == 0
    assert [json.loads(line) for line in stdout.splitlines()] == expected


def test_failed_worker_capture_reaches_restarted_status(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from coordinator_core.codex_runner import workflow
    from coordinator_core.codex_status.__main__ import snapshot

    root = "00000000-0000-4000-8000-000000000001"
    child = "00000000-0000-4000-8000-000000000002"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    private = tmp_path / "private"
    private.mkdir()
    monkeypatch.setenv("COORDINATOR_PROVIDER_EVIDENCE_DIR", str(private))
    stage = tmp_path / "agents" / "X1"
    stage.mkdir(parents=True)
    (stage / "stdout.jsonl").write_text(json.dumps({"type": "thread.started", "thread_id": child}) + "\n")
    identity = dict(broker_root_session_id=root, broker_request_id=1, broker_fire_id="fire")
    proc = SimpleNamespace(pid=123, returncode=1)
    usage = {"input_tokens": 1000, "output_tokens": 0, "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}}
    with (stage / "stderr.txt").open("wb") as stderr:
        with workflow._provider_stream({}, "codex", stage, stderr, "fire", 1, 0, None) as (_, writer, start, trace):
            start(proc.pid)
            for uid, counters in [("resp_one", usage), ("resp_one", usage), ("resp_two", None)]:
                body = {"id": uid, "model": "gpt-6.2-astra", "usage": counters}
                msg = json.dumps({"type": "response.failed", "response": body})
                writer.write(("2026-11-24T12:00:00Z TRACE tungstenite::protocol: Received message " + msg + "\n").encode())
    other = workflow._save_process(stage, SimpleNamespace(pid=124, returncode=1), "codex", {}, None,
                                   provider_trace=trace, **identity)
    assert other["canonical_usage"]["status"] == "incomplete"
    assert not (tmp_path / "settings" / "state" / "codex-usage" / (root + ".jsonl")).exists()
    workflow._record_execution(root, "fire", 1, "started")
    for _ in range(2):
        receipt = workflow._save_process(stage, proc, "codex", {}, "another-model", provider_trace=trace, **identity)
        workflow._record_execution(root, "fire", 1, "completed", receipt=receipt)
    result = snapshot({"session_id": root})
    assert result["usd"] == pytest.approx(.008)
    assert result["priced_events"] == 1
    assert result["execution_inventory"]["missing_usage"] == ["fire:1"]
    assert not result["coverage"]["workflows"]
    cache = tmp_path / "settings" / "state" / "codex-status" / (root + ".json")
    cache.unlink()
    assert snapshot({"session_id": root})["usd"] == result["usd"]
