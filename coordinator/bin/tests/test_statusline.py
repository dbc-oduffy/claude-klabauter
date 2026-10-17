"""Smoke tests for this tree's copy of `statusline.py`, run as a real process.

The behavioural suite lives beside the source in coordinator-content-repo
(`coordinator/tests/test_statusline_*.py`); this copy is gated byte-identical to it at publish.
These pin what a consumer install depends on: the inner statusline is read from the durable
settings home, never from inside the plugin root, and the visible line carries effort and the
context gauge.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SCRIPT = Path(__file__).resolve().parents[1] / "statusline.py"


def _run(stdin: bytes, settings_home: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["COORDINATOR_SETTINGS_HOME"] = str(settings_home)
    env["NO_COLOR"] = "1"
    env["CLAUDE_CONFIG_DIR"] = str(settings_home / "no-claude-config")
    env.pop("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", None)
    env.pop("COORDINATOR_STATUSLINE_GAUGE", None)
    return subprocess.run([sys.executable, str(_SCRIPT), *args], input=stdin,
                          capture_output=True, env=env, timeout=30,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _payload(session_id: str = "sl-smoke") -> bytes:
    return json.dumps({
        "session_id": session_id,
        "model": {"id": "claude-opus-5-5", "display_name": "Opus 5"},
        "effort": {"level": "high"},
        "context_window": {"used_percentage": 10, "context_window_size": 200000},
    }).encode("utf-8")


def test_own_line_renders_model_effort_and_gauge(tmp_path):
    result = _run(_payload(), tmp_path)
    assert result.returncode == 0
    line = result.stdout.decode("utf-8")
    assert "Opus 5" in line and "high" in line and "%" in line


def test_sidecar_lands_under_the_settings_home(tmp_path):
    _run(_payload("sl-sidecar"), tmp_path)
    record = json.loads((tmp_path / "state" / "context-window" / "sl-sidecar.json").read_text())
    assert record["context_window"]["used_percentage"] == 10


def test_inner_command_is_read_from_the_settings_home(tmp_path):
    (tmp_path / "statusline-inner.json").write_text(json.dumps(
        {"type": "command", "command": f'"{sys.executable}" -c "print(\'inner-ran\')"'}))
    result = _run(_payload(), tmp_path)
    assert result.stdout.decode("utf-8").strip() == "inner-ran"


def test_selftest_reports_the_sidecar_path(tmp_path):
    result = _run(b"", tmp_path, "--selftest")
    assert result.returncode == 0
    assert str(tmp_path / "state" / "context-window") in result.stdout.decode("utf-8")


def _row(mid: str, tokens: int = 1_000_000) -> str:
    return json.dumps({"requestId": f"r-{mid}", "message": {
        "id": mid, "model": "claude-opus-5-5",
        "usage": {"input_tokens": tokens, "output_tokens": 0}}}) + "\n"


def _fixture_session(root: Path, session_id: str = "sess1") -> tuple[Path, float]:
    from coordinator_core.usage.price_table import estimate_usd
    main = root / f"{session_id}.jsonl"
    rows = {main: _row("m1"),
            root / session_id / "subagents" / "agent-a.jsonl": _row("m2"),
            root / session_id / "subagents" / "workflows" / "wf_x" / "agent-b.jsonl": _row("m3")}
    for path, text in rows.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    one = estimate_usd("claude-opus-5-5", {"input_tokens": 1_000_000, "output_tokens": 0}, None)
    assert one
    return main, one * 3


def _cost_payload(transcript: Path, session_id: str = "sess1") -> bytes:
    body = json.loads(_payload(session_id))
    body["transcript_path"] = str(transcript)
    return json.dumps(body).encode("utf-8")


def test_cost_segment_is_last_and_equals_the_fixture_total(tmp_path):
    main, total = _fixture_session(tmp_path / "proj")
    line = _run(_cost_payload(main), tmp_path / "home").stdout.decode("utf-8").strip()
    assert line.split(" · ")[-1] == f"~${total:.2f}"


def test_cost_segment_absent_without_transcript_path(tmp_path):
    line = _run(_payload(), tmp_path).stdout.decode("utf-8").strip()
    assert "~$" not in line and line.endswith("%")


def test_cost_segment_absent_not_zero_when_session_is_unreadable(tmp_path):
    line = _run(_cost_payload(tmp_path / "gone" / "sess1.jsonl"), tmp_path).stdout.decode("utf-8")
    assert "~$" not in line and "Opus 5" in line


def test_cost_segment_marks_backlog_with_plus():
    statusline = _load_statusline()
    cost = statusline._engine_module("coordinator_core.usage.session_cost")
    assert cost.render({"usd": 1.234, "complete": False, "unpriced_tokens": 0, "files": 1}) == "~$1.23+"


def test_a_raising_session_cost_still_yields_the_rest_of_the_line(tmp_path, monkeypatch):
    statusline = _load_statusline()
    monkeypatch.setattr(statusline, "_engine_module", lambda dotted: (_ for _ in ()).throw(ImportError(dotted)))
    payload = {"transcript_path": str(tmp_path / "s.jsonl")}
    assert statusline._session_cost_segment(payload, "s") is None


def _fast_path_fixture(tmp_path, monkeypatch, *, grow):
    import json
    import time
    statusline = _load_statusline()
    transcript = tmp_path / "sess1.jsonl"
    transcript.write_bytes(b"x\n")
    cache = tmp_path / "cost.json"
    cache.write_text(json.dumps({
        "complete": True, "usd_total": 1.5, "fresh_until": time.time() + 60, "main_size": 2,
        "files": {"sess1.jsonl": {"offset": 2, "size": 2}}}), encoding="utf-8")
    monkeypatch.setattr(statusline, "_cost_path", lambda sid: cache)
    imported = []

    def engine(dotted):
        imported.append(dotted)
        raise ImportError(dotted)

    monkeypatch.setattr(statusline, "_engine_module", engine)
    if grow:
        transcript.write_text("x\nmore\n", encoding="utf-8")
    return statusline, {"transcript_path": str(transcript)}, imported


def test_an_unchanged_transcript_renders_from_the_fast_path(tmp_path, monkeypatch):
    statusline, payload, imported = _fast_path_fixture(tmp_path, monkeypatch, grow=False)
    assert "~$1.50" in statusline._session_cost_segment(payload, "s")
    assert imported == []


def test_a_grown_transcript_falls_through_to_the_engine(tmp_path, monkeypatch):
    statusline, payload, imported = _fast_path_fixture(tmp_path, monkeypatch, grow=True)
    statusline._session_cost_segment(payload, "s")
    assert imported == ["coordinator_core.usage.session_cost"]


def test_the_cost_cache_lands_under_the_settings_home(tmp_path):
    main, _ = _fixture_session(tmp_path / "proj")
    _run(_cost_payload(main), tmp_path / "home")
    assert (tmp_path / "home" / "state" / "session-cost" / "sess1.json").is_file()


def _load_statusline():
    import importlib.util
    spec = importlib.util.spec_from_file_location("statusline_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("command", [
    "mystatus | head -1", "a && b", "a; b", "a > out.txt", "echo `id`",
])
def test_a_command_needing_a_shell_is_not_delegated(command):
    statusline = _load_statusline()
    with pytest.raises(OSError):
        statusline._argv_for(command)


def test_shell_characters_inside_quotes_stay_an_argument():
    statusline = _load_statusline()
    argv = statusline._argv_for(f'"{sys.executable}" -c "print(\'a|b\')"')
    assert argv[1:] == ["-c", "print('a|b')"]
    assert os.path.samefile(argv[0], sys.executable)


def test_an_unresolvable_executable_is_not_delegated():
    statusline = _load_statusline()
    with pytest.raises(OSError):
        statusline._argv_for("no-such-statusline-binary-xyz --flag")


def test_an_embedded_quoted_value_stays_one_argument():
    statusline = _load_statusline()
    path = r"C:\Users\o'neil\x.py" if os.name == "nt" else "o'neil/x.py"
    argv = statusline._argv_for(f'"{sys.executable}" --arg="a b" "{path}"')
    assert argv[1:] == ["--arg=a b", path]


@pytest.mark.skipif(os.name != "nt", reason="batch files are a Windows CreateProcess shape")
def test_a_batch_file_executable_is_not_delegated(tmp_path):
    shim = tmp_path / "status.cmd"
    shim.write_text("@echo off\r\necho hi\r\n")
    statusline = _load_statusline()
    with pytest.raises(OSError):
        statusline._argv_for(f'"{shim}"')
