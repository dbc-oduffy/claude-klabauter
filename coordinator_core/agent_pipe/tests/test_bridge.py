"""Tests for agent_pipe.bridge: labelled delivery on each leg, routing control, rebind-at-dispatch, no token gate."""
from __future__ import annotations

import inspect
import json
import sys
import threading
import time

import pytest

from coordinator_core.agent_pipe import bridge, codex_app_server, mailbox, roster
from coordinator_core.agent_pipe.contract import mailbox_path
from coordinator_core.agent_pipe.tests.fixtures.fake_app_server import SCRIPT
from pathlib import Path

THREAD = "01a1a3e1-30c3-71c1-9eab-aae86f8bfb81"
LABEL = "[from a (claude) via bridge]"


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.setenv("AGY_BRAIN_DIR", str(tmp_path / "ag" / "brain"))


def _mail(session: str) -> list[dict]:
    return [json.loads(x) for x in mailbox.drain(session)]


def test_claude_leg_writes_labelled_line_with_provenance() -> None:
    roster.bind("a", "claude", "sess-a")
    roster.bind("b", "claude", "sess-b")
    res = bridge.route(bridge.make_message("a", "b", "hello"))
    assert res.status == "delivered"
    [line] = _mail("sess-b")
    assert line["content"] == f"{LABEL}\nhello"
    assert line["provenance"] == {"from_identity": "a", "from_kind": "claude", "origin": "bridge", "ts": line["ts"]}
    assert line["to_session"] == "sess-b"


def test_codex_leg_delivers_labelled_text(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    def fake_deliver(argv, thread_id, text, timeout: int = 0):
        seen.update(argv=argv, thread=thread_id, text=text)
        yield "ok"

    monkeypatch.setattr(codex_app_server, "deliver", fake_deliver)
    roster.bind("a", "claude", "sess-a")
    roster.bind("cx", "codex", THREAD, argv=["x", "proxy"])
    res = bridge.route(bridge.make_message("a", "cx", "ping"))
    res.wait(5)
    assert res.status == "handed-off"
    assert seen == {"argv": ["x", "proxy"], "thread": THREAD, "text": f"{LABEL}\nping"}


def test_codex_leg_against_fake_app_server_completes_without_failure() -> None:
    roster.bind("a", "claude", "sess-a")
    roster.bind("cx", "codex", THREAD, argv=[sys.executable, SCRIPT, "owner"])
    res = bridge.route(bridge.make_message("a", "cx", "ping"))
    res.wait(20)
    assert res.status == "handed-off"
    assert not bridge.log_path().exists()


def test_codex_leg_does_not_hold_the_caller_for_the_remote_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()

    def slow(argv, thread_id, text, timeout: int = 0):
        gate.wait(10)
        yield "late"

    monkeypatch.setattr(codex_app_server, "deliver", slow)
    roster.bind("cx", "codex", THREAD, argv=["x"])
    t0 = time.monotonic()
    res = bridge.route(bridge.make_message("a", "cx", "ping"))
    assert res.status == "handed-off" and time.monotonic() - t0 < 1.0
    gate.set()
    res.wait(5)


def test_codex_binding_without_argv_is_unroutable_and_logged() -> None:
    roster.bind("cx", "codex", THREAD)
    res = bridge.route(bridge.make_message("a", "cx", "ping"))
    assert res.status == "unroutable"
    assert json.loads(bridge.log_path().read_text().splitlines()[-1])["event"] == "unroutable"


def test_agy_leg_resumes_conversation_with_labelled_text(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = tmp_path / "argv.json"
    script = f"import sys,json;open({str(out)!r},'w').write(json.dumps(sys.argv[1:]))"

    class FakeAgy:
        def resume_argv(self, conversation, text):
            return [sys.executable, "-c", script, conversation, text]

    monkeypatch.setattr(bridge, "AgyAdapter", FakeAgy)
    roster.bind("a", "claude", "sess-a")
    roster.bind("ag", "agy", "conv-9")
    res = bridge.route(bridge.make_message("a", "ag", "yo"))
    res.wait(20)
    assert res.status == "handed-off"
    assert json.loads(out.read_text()) == ["conv-9", f"{LABEL}\nyo"]


def _spool(tmp_path: Path, conv: str) -> list[dict]:
    path = tmp_path / "unimatrix" / "agy-legs" / f"{conv}.queue.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def test_agy_delivery_with_presence_lock_held_spools_and_spawns_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from coordinator_core.agent_pipe import agy as agy_mod

    def no_spawn(*a, **kw):
        raise AssertionError("agy spawned over a held presence lock")

    monkeypatch.setattr(bridge.subprocess, "Popen", no_spawn)
    roster.bind("a", "claude", "sess-a")
    roster.bind("ag", "agy", "conv-9")
    lock = tmp_path / "ag" / "presence" / "conv-9.lock"
    lock.parent.mkdir(parents=True)
    with open(lock, "a+b") as fh:
        assert agy_mod._try_lock(fh)
        res = bridge.route(bridge.make_message("a", "ag", "yo"))
        agy_mod._unlock(fh)
    assert (res.status, res.reason) == ("queued", "spooled")
    [line] = _spool(tmp_path, "conv-9")
    assert line["text"] == f"{LABEL}\nyo" and line["from"] == "a"


def test_concurrent_agy_deliveries_never_run_two_resumes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    started = tmp_path / "started"
    script = f"import time;open({str(started)!r},'a').write('x');time.sleep(1.0)"

    class FakeAgy:
        def resume_argv(self, conversation, text):
            return [sys.executable, "-c", script]

    monkeypatch.setattr(bridge, "AgyAdapter", FakeAgy)
    roster.bind("a", "claude", "sess-a")
    roster.bind("ag", "agy", "conv-9")
    first = bridge.route(bridge.make_message("a", "ag", "one"))
    second = bridge.route(bridge.make_message("a", "ag", "two"))
    third = bridge.route(bridge.make_message("a", "ag", "three"))
    assert first.status == "handed-off"
    assert (second.status, third.status) == ("queued", "queued")
    assert [x["text"].split("\n")[1] for x in _spool(tmp_path, "conv-9")] == ["two", "three"]
    first.wait(20)
    assert started.read_text() == "xx"
    assert _spool(tmp_path, "conv-9") == []
    again = bridge.route(bridge.make_message("a", "ag", "four"))
    assert again.status == "handed-off"
    again.wait(20)
    assert started.read_text() == "xxx"


def test_spooled_text_is_resumed_once_when_the_running_resume_exits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = tmp_path / "texts.jsonl"
    script = (f"import sys,json,time;open({str(out)!r},'a').write(json.dumps(sys.argv[2])+'\\n');"
              "time.sleep(0.5)")

    class FakeAgy:
        def resume_argv(self, conversation, text):
            return [sys.executable, "-c", script, conversation, text]

    monkeypatch.setattr(bridge, "AgyAdapter", FakeAgy)
    roster.bind("a", "claude", "sess-a")
    roster.bind("ag", "agy", "conv-9")
    first = bridge.route(bridge.make_message("a", "ag", "one"))
    assert bridge.route(bridge.make_message("a", "ag", "two")).status == "queued"
    first.wait(20)
    rows = [json.loads(x) for x in out.read_text().splitlines()]
    assert len(rows) == 2 and rows[0].endswith("one") and rows[1].endswith("two")


def test_agy_spool_refusal_is_unroutable(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*a, **kw):
        raise bridge.SpoolRefused("spool full")

    monkeypatch.setattr(bridge, "claim_resume", refuse)
    roster.bind("ag", "agy", "conv-9")
    res = bridge.route(bridge.make_message("a", "ag", "yo"))
    assert res.status == "unroutable" and "spool full" in res.reason


def _last_log() -> dict:
    return json.loads(bridge.log_path().read_text().splitlines()[-1])


def test_agy_quota_error_line_is_logged_with_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    line = json.dumps({"event": "error", "error": {"message": "quota exceeded"}})

    class FakeAgy:
        def resume_argv(self, conversation, text) -> list[str]:
            return [sys.executable, "-c", f"print({line!r})"]

    monkeypatch.setattr(bridge, "AgyAdapter", FakeAgy)
    roster.bind("a", "claude", "sess-a")
    roster.bind("ag", "agy", "conv-9")
    bridge.route(bridge.make_message("a", "ag", "yo")).wait(20)
    row = _last_log()
    assert row["event"] == "leg-failed" and row["cause"] == "quota_exhausted"


def test_codex_leg_failures_log_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    errors = iter([
        codex_app_server.AppServerError(-1, "turn failed: usage limit reached"),
        codex_app_server.AppServerError(-1, "timed out waiting for the app-server"),
        codex_app_server.AppServerUnreachable(-1, "timed out waiting for the app-server"),
    ])

    def fake_deliver(argv, thread_id, text, timeout: int = 0):
        raise next(errors)
        yield

    monkeypatch.setattr(codex_app_server, "deliver", fake_deliver)
    roster.bind("a", "claude", "sess-a")
    roster.bind("cx", "codex", THREAD, argv=["x", "proxy"])
    causes = []
    for _ in range(3):
        bridge.route(bridge.make_message("a", "cx", "ping")).wait(5)
        causes.append(_last_log()["cause"])
    assert causes == ["quota_exhausted", "turn_timeout", "handshake_timeout"]


def test_classify_exit_ignores_agent_text_and_unknown_failures() -> None:
    from coordinator_core.agent_pipe.contract import classify_exit
    assert classify_exit('{"type":"item.completed","item":{"text":"rate limit"}}') is None
    assert classify_exit("boom") is None
    assert classify_exit("") is None


def test_paused_pair_is_refused_and_logged_default_allows() -> None:
    roster.bind("a", "claude", "sess-a")
    roster.bind("b", "claude", "sess-b")
    assert bridge.route(bridge.make_message("a", "b", "one")).status == "delivered"
    bridge.set_pause("a", "b")
    res = bridge.route(bridge.make_message("a", "b", "two"))
    assert res.status == "refused" and res.reason == "paused:a>b"
    assert [m["content"].split("\n")[1] for m in _mail("sess-b")] == ["one"]
    assert json.loads(bridge.log_path().read_text().splitlines()[-1])["event"] == "refused"
    bridge.set_pause("a", "b", paused=False)
    assert bridge.route(bridge.make_message("a", "b", "three")).status == "delivered"


def test_pause_wildcard_and_mute() -> None:
    roster.bind("a", "claude", "sess-a")
    roster.bind("b", "claude", "sess-b")
    bridge.set_pause("*", "b")
    assert bridge.route(bridge.make_message("a", "b", "x")).status == "refused"
    bridge.set_pause("*", "b", paused=False)
    bridge.set_mute("a")
    assert bridge.route(bridge.make_message("a", "b", "x")).reason == "muted:a"
    assert bridge.route(bridge.make_message("b", "a", "x")).reason == "muted:a"
    bridge.set_mute("a", muted=False)
    assert bridge.route(bridge.make_message("a", "b", "x")).status == "delivered"


def test_loop_guard_refuses_reply_bodies() -> None:
    roster.bind("b", "claude", "sess-b")
    res = bridge.route(bridge.make_message("a", "b", "[reply to a] thanks"))
    assert res.status == "refused" and res.reason == "loop-guard"
    assert not mailbox_path("sess-b").exists()


def test_unbound_target_is_unroutable() -> None:
    assert bridge.route(bridge.make_message("a", "ghost", "x")).status == "unroutable"


def test_line_queued_before_rebind_reaches_new_binding() -> None:
    roster.bind("a", "claude", "sess-a")
    roster.bind("b", "claude", "sess-old", token="t1")
    bridge.enqueue(bridge.make_message("a", "b", "queued"))
    roster.bind("b", "claude", "sess-new", token="t2")
    [res] = bridge.drain_once()
    assert res.status == "delivered"
    assert not mailbox_path("sess-old").exists()
    [line] = _mail("sess-new")
    assert line["content"].endswith("queued")


def test_serve_drains_inbox_and_stops() -> None:
    roster.bind("a", "claude", "sess-a")
    roster.bind("b", "claude", "sess-b")
    stop = threading.Event()
    th = threading.Thread(target=bridge.serve, kwargs={"stop": stop, "poll_s": 0.01})
    th.start()
    bridge.enqueue(bridge.make_message("a", "b", "via serve"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not mailbox_path("sess-b").exists():
        time.sleep(0.02)
    stop.set()
    th.join(5)
    assert not th.is_alive()
    assert _mail("sess-b")[0]["content"].endswith("via serve")


def test_malformed_inbox_line_is_skipped() -> None:
    bridge.inbox_path().parent.mkdir(parents=True, exist_ok=True)
    bridge.inbox_path().write_text("not json\n", encoding="utf-8")
    assert bridge.drain_once() == []
    assert json.loads(bridge.log_path().read_text().splitlines()[-1])["event"] == "malformed"


FAKE_ACP = [sys.executable, str(__import__("pathlib").Path(__file__).parent / "fixtures" / "fake_acp_agent.py")]


def _grok_wire(tmp_path, monkeypatch) -> "list[dict]":
    wire = tmp_path / "acp-wire.jsonl"
    monkeypatch.setenv("FAKE_ACP_LOG", str(wire))
    return wire


def _wire_rows(wire) -> "list[dict]":
    return [json.loads(x) for x in wire.read_text(encoding="utf-8").splitlines()]


def test_grok_binding_is_delivered_not_refused_via_resume_then_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire = _grok_wire(tmp_path, monkeypatch)
    roster.bind("a", "claude", "sess-a")
    roster.bind("gk", "grok", "gsess-1", argv=FAKE_ACP)
    res = bridge.route(bridge.make_message("a", "gk", "ping"))
    res.wait(20)
    assert res.status == "handed-off"
    rows = _wire_rows(wire)
    methods = [r["method"] for r in rows if "method" in r]
    assert methods.index("session/resume") < methods.index("session/prompt")
    assert "session/new" not in methods
    resume = next(r for r in rows if r.get("method") == "session/resume")
    assert resume["params"]["sessionId"] == "gsess-1"
    prompt = next(r for r in rows if r.get("method") == "session/prompt")
    assert prompt["params"] == {"sessionId": "gsess-1", "prompt": [{"type": "text", "text": f"{LABEL}\nping"}]}


def test_grok_leg_rejects_permission_requests_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wire = _grok_wire(tmp_path, monkeypatch)
    roster.bind("a", "claude", "sess-a")
    roster.bind("gk", "grok", "gsess-1", argv=FAKE_ACP)
    bridge.route(bridge.make_message("a", "gk", "permission")).wait(20)
    answer = next(r for r in _wire_rows(wire) if r.get("id") == "perm-1" and "result" in r)
    assert answer["result"]["outcome"] == {"outcome": "selected", "optionId": "reject-once"}


def test_grok_quota_error_is_logged_with_cause(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _grok_wire(tmp_path, monkeypatch)
    roster.bind("a", "claude", "sess-a")
    roster.bind("gk", "grok", "gsess-1", argv=FAKE_ACP)
    bridge.route(bridge.make_message("a", "gk", "quota")).wait(20)
    row = _last_log()
    assert row["event"] == "leg-failed" and row["cause"] == "quota_exhausted"


def test_grok_binding_with_missing_binary_logs_failure_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    roster.bind("a", "claude", "sess-a")
    roster.bind("gk", "grok", "gsess-1", argv=["/nonexistent/grok-binary"])
    res = bridge.route(bridge.make_message("a", "gk", "x"))
    res.wait(20)
    assert res.status == "handed-off" and _last_log()["event"] == "leg-failed"


def test_grok_kind_has_a_leg() -> None:
    assert "grok" in bridge._LEGS


def test_no_token_check_on_any_delivery_path() -> None:
    roster.bind("a", "claude", "sess-a", token="tok-a")
    roster.bind("b", "claude", "sess-b", token="tok-b")
    roster.bind("b", "claude", "sess-b", token="rotated")
    assert bridge.route(bridge.make_message("a", "b", "x")).status == "delivered"
    assert "token" not in inspect.getsource(bridge).replace("messaging token", "").replace("a stored token is data", "").split('"""', 2)[2]
