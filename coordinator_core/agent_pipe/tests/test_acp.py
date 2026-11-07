"""AcpDriver / GrokAcpDriver against the fake ACP agent: start, stream, approval routing, resume, cancel, deadlines."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from coordinator_core.agent_pipe import acp
from coordinator_core.agent_pipe.acp import AcpDriver, AcpError
from coordinator_core.agent_pipe.grok import GROK_ACP_ARGV, GrokAcpDriver
from coordinator_core.agent_pipe.session_driver import ALLOW_ONCE, REJECT_ONCE, Decision

pytestmark = pytest.mark.complete

FAKE = [sys.executable, str(Path(__file__).parent / "fixtures" / "fake_acp_agent.py")]


@pytest.fixture
def log(tmp_path, monkeypatch):
    for k in ("FAKE_ACP_SILENT", "FAKE_ACP_NO_RESUME"):
        monkeypatch.delenv(k, raising=False)
    path = tmp_path / "wire.jsonl"
    monkeypatch.setenv("FAKE_ACP_LOG", str(path))
    return path


def _wire(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def _methods(path: Path) -> list[str]:
    return [m["method"] for m in _wire(path) if "method" in m]


def test_start_handshake_advertises_no_fs_or_terminal_and_new_session(log):
    with GrokAcpDriver(argv=FAKE) as d:
        h = d.start("/work")
    assert h.session_id == "sess-1" and h.cwd == "/work"
    init = next(m for m in _wire(log) if m.get("method") == "initialize")["params"]
    assert init["protocolVersion"] == 1
    assert init["clientCapabilities"] == {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}
    new = next(m for m in _wire(log) if m.get("method") == "session/new")["params"]
    assert new["cwd"] == "/work"


def test_drivers_satisfy_the_session_driver_surface_and_grok_argv():
    for name in ("start", "resume", "prompt", "cancel", "close"):
        assert callable(getattr(GrokAcpDriver, name))
    assert GROK_ACP_ARGV == ["grok", "agent", "stdio"]
    assert GrokAcpDriver()._argv == GROK_ACP_ARGV and "--always-approve" not in GROK_ACP_ARGV


def test_prompt_streams_updates_then_done(log):
    with AcpDriver(FAKE) as d:
        h = d.start("/w")
        ups = list(d.prompt(h, "hi"))
    assert [(u.kind, u.text) for u in ups[:2]] == [("message", "hello "), ("message", "world")]
    assert ups[-1].kind == "done" and ups[-1].raw["stopReason"] == "end_turn"


def test_start_with_prompt_keeps_its_updates(log):
    with AcpDriver(FAKE) as d:
        d.start("/w", "hi")
        assert [u.text for u in d.start_updates if u.kind == "message"] == ["hello ", "world"]


def test_default_approval_rejects_and_never_allows(log):
    with AcpDriver(FAKE) as d:
        h = d.start("/w")
        ups = list(d.prompt(h, "permission please"))
    assert "permission:reject-once" in [u.text for u in ups]
    reply = next(m for m in _wire(log) if m.get("id") == "perm-1" and "result" in m)
    assert reply["result"]["outcome"] == {"outcome": "selected", "optionId": "reject-once"}


def test_callback_receives_request_and_its_allow_decision_goes_on_the_wire(log):
    seen = []

    def approve(req):
        seen.append(req)
        return Decision(ALLOW_ONCE)

    with AcpDriver(FAKE, approve) as d:
        h = d.start("/w")
        ups = list(d.prompt(h, "permission please"))
    assert seen[0].tool == "write file" and seen[0].options == ("allow-once", "reject-once")
    assert "permission:allow-once" in [u.text for u in ups]


def test_raising_or_unknown_decision_rejects(log):
    def boom(req):
        raise RuntimeError("x")

    for cb in (boom, lambda req: Decision("allow-forever-nonsense")):
        with AcpDriver(FAKE, cb) as d:
            ups = list(d.prompt(d.start("/w"), "permission please"))
        assert "permission:reject-once" in [u.text for u in ups]


def test_resume_uses_session_resume(log):
    with AcpDriver(FAKE) as d:
        h = d.resume("sess-9")
    assert h.session_id == "sess-9"
    ms = _methods(log)
    assert "session/resume" in ms and "session/load" not in ms


def test_resume_falls_back_to_load_when_resume_is_missing(log, monkeypatch):
    monkeypatch.setenv("FAKE_ACP_NO_RESUME", "1")
    with AcpDriver(FAKE) as d:
        h = d.resume("sess-9")
        ups = list(d.prompt(h, "hi"))
    ms = _methods(log)
    assert ms.index("session/resume") < ms.index("session/load")
    assert all(u.text != "replayed history" for u in ups)


def test_cancel_ends_inflight_prompt_with_cancelled(log):
    with AcpDriver(FAKE) as d:
        h = d.start("/w")
        it = d.prompt(h, "slow one")
        first = next(it)
        assert first.text == "working"
        d.cancel(h)
        rest = list(it)
    assert rest[-1].kind == "done" and rest[-1].raw["stopReason"] == "cancelled"
    cancel = next(m for m in _wire(log) if m.get("method") == "session/cancel")
    assert "id" not in cancel and cancel["params"]["sessionId"] == "sess-1"


def test_silent_agent_is_a_named_handshake_timeout(log, monkeypatch):
    monkeypatch.setenv("FAKE_ACP_SILENT", "1")
    d = AcpDriver(FAKE, handshake_timeout_s=0.5)
    with pytest.raises(AcpError) as ei:
        d.start("/w")
    assert ei.value.cause == "handshake_timeout"
    assert d._proc is None


def test_prompt_error_carries_classified_cause(log):
    with AcpDriver(FAKE) as d:
        h = d.start("/w")
        with pytest.raises(AcpError) as ei:
            list(d.prompt(h, "quota"))
    assert ei.value.cause == "quota_exhausted" and ei.value.code == 429


def test_stalled_prompt_is_turn_timeout(log):
    with AcpDriver(FAKE, prompt_timeout_s=0.5) as d:
        h = d.start("/w")
        with pytest.raises(AcpError) as ei:
            list(d.prompt(h, "slow one"))
    assert ei.value.cause == "turn_timeout"


def test_close_shuts_down_the_agent_and_a_later_call_reconnects(log):
    d = AcpDriver(FAKE)
    h = d.start("/w")
    d.close(h)
    assert d._proc is None and "session/close" in _methods(log)
    h2 = d.start("/w")
    assert h2.session_id == "sess-1"
    d.shutdown()


def test_missing_binary_is_acp_error():
    with pytest.raises(AcpError):
        GrokAcpDriver(argv=["definitely-not-a-grok-binary-xyz", "agent", "stdio"]).start("/w")


def test_agent_to_client_fs_requests_are_method_not_found(log):
    d = AcpDriver(FAKE)
    d._send = lambda obj: sent.append(obj)
    sent: list[dict] = []
    d._route({"jsonrpc": "2.0", "id": 7, "method": "fs/read_text_file", "params": {}})
    assert sent[0]["error"]["code"] == acp.METHOD_NOT_FOUND and sent[0]["id"] == 7
