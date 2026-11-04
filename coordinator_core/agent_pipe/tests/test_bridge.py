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

THREAD = "01a1a3e1-30c3-71c1-9eab-aae86f8bfb81"
LABEL = "[from a (claude) via bridge]"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))


def _mail(session: str) -> list[dict]:
    return [json.loads(x) for x in mailbox.drain(session)]


def test_claude_leg_writes_labelled_line_with_provenance():
    roster.bind("a", "claude", "sess-a")
    roster.bind("b", "claude", "sess-b")
    res = bridge.route(bridge.make_message("a", "b", "hello"))
    assert res.status == "delivered"
    [line] = _mail("sess-b")
    assert line["content"] == f"{LABEL}\nhello"
    assert line["provenance"] == {"from_identity": "a", "from_kind": "claude", "origin": "bridge", "ts": line["ts"]}
    assert line["to_session"] == "sess-b"


def test_codex_leg_delivers_labelled_text(monkeypatch):
    seen = {}

    def fake_deliver(argv, thread_id, text, timeout=0):
        seen.update(argv=argv, thread=thread_id, text=text)
        yield "ok"

    monkeypatch.setattr(codex_app_server, "deliver", fake_deliver)
    roster.bind("a", "claude", "sess-a")
    roster.bind("cx", "codex", THREAD, argv=["x", "proxy"])
    res = bridge.route(bridge.make_message("a", "cx", "ping"))
    res.wait(5)
    assert res.status == "handed-off"
    assert seen == {"argv": ["x", "proxy"], "thread": THREAD, "text": f"{LABEL}\nping"}


def test_codex_leg_against_fake_app_server_completes_without_failure():
    roster.bind("a", "claude", "sess-a")
    roster.bind("cx", "codex", THREAD, argv=[sys.executable, SCRIPT, "owner"])
    res = bridge.route(bridge.make_message("a", "cx", "ping"))
    res.wait(20)
    assert res.status == "handed-off"
    assert not bridge.log_path().exists()


def test_codex_leg_does_not_hold_the_caller_for_the_remote_turn(monkeypatch):
    gate = threading.Event()

    def slow(argv, thread_id, text, timeout=0):
        gate.wait(10)
        yield "late"

    monkeypatch.setattr(codex_app_server, "deliver", slow)
    roster.bind("cx", "codex", THREAD, argv=["x"])
    t0 = time.monotonic()
    res = bridge.route(bridge.make_message("a", "cx", "ping"))
    assert res.status == "handed-off" and time.monotonic() - t0 < 1.0
    gate.set()
    res.wait(5)


def test_codex_binding_without_argv_is_unroutable_and_logged():
    roster.bind("cx", "codex", THREAD)
    res = bridge.route(bridge.make_message("a", "cx", "ping"))
    assert res.status == "unroutable"
    assert json.loads(bridge.log_path().read_text().splitlines()[-1])["event"] == "unroutable"


def test_agy_leg_resumes_conversation_with_labelled_text(monkeypatch, tmp_path):
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


def _last_log() -> dict:
    return json.loads(bridge.log_path().read_text().splitlines()[-1])


def test_agy_quota_error_line_is_logged_with_cause(monkeypatch):
    line = json.dumps({"event": "error", "error": {"message": "quota exceeded"}})

    class FakeAgy:
        def resume_argv(self, conversation, text):
            return [sys.executable, "-c", f"print({line!r})"]

    monkeypatch.setattr(bridge, "AgyAdapter", FakeAgy)
    roster.bind("a", "claude", "sess-a")
    roster.bind("ag", "agy", "conv-9")
    bridge.route(bridge.make_message("a", "ag", "yo")).wait(20)
    row = _last_log()
    assert row["event"] == "leg-failed" and row["cause"] == "quota_exhausted"


def test_codex_leg_failures_log_cause(monkeypatch):
    errors = iter([
        codex_app_server.AppServerError(-1, "turn failed: usage limit reached"),
        codex_app_server.AppServerError(-1, "timed out waiting for the app-server"),
        codex_app_server.AppServerUnreachable(-1, "timed out waiting for the app-server"),
    ])

    def fake_deliver(argv, thread_id, text, timeout=0):
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


def test_classify_exit_ignores_agent_text_and_unknown_failures():
    from coordinator_core.agent_pipe.contract import classify_exit
    assert classify_exit('{"type":"item.completed","item":{"text":"rate limit"}}') is None
    assert classify_exit("boom") is None
    assert classify_exit("") is None


def test_paused_pair_is_refused_and_logged_default_allows():
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


def test_pause_wildcard_and_mute():
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


def test_loop_guard_refuses_reply_bodies():
    roster.bind("b", "claude", "sess-b")
    res = bridge.route(bridge.make_message("a", "b", "[reply to a] thanks"))
    assert res.status == "refused" and res.reason == "loop-guard"
    assert not mailbox_path("sess-b").exists()


def test_unbound_target_is_unroutable():
    assert bridge.route(bridge.make_message("a", "ghost", "x")).status == "unroutable"


def test_line_queued_before_rebind_reaches_new_binding():
    roster.bind("a", "claude", "sess-a")
    roster.bind("b", "claude", "sess-old", token="t1")
    bridge.enqueue(bridge.make_message("a", "b", "queued"))
    roster.bind("b", "claude", "sess-new", token="t2")
    [res] = bridge.drain_once()
    assert res.status == "delivered"
    assert not mailbox_path("sess-old").exists()
    [line] = _mail("sess-new")
    assert line["content"].endswith("queued")


def test_serve_drains_inbox_and_stops():
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


def test_malformed_inbox_line_is_skipped():
    bridge.inbox_path().parent.mkdir(parents=True, exist_ok=True)
    bridge.inbox_path().write_text("not json\n", encoding="utf-8")
    assert bridge.drain_once() == []
    assert json.loads(bridge.log_path().read_text().splitlines()[-1])["event"] == "malformed"


def test_no_token_check_on_any_delivery_path():
    roster.bind("a", "claude", "sess-a", token="tok-a")
    roster.bind("b", "claude", "sess-b", token="tok-b")
    roster.bind("b", "claude", "sess-b", token="rotated")
    assert bridge.route(bridge.make_message("a", "b", "x")).status == "delivered"
    assert "token" not in inspect.getsource(bridge).replace("messaging token", "").replace("a stored token is data", "").split('"""', 2)[2]
