"""Tests for workflow_fire.notify: one-shot bridge message to agy/grok/claude peers, legs stubbed."""
from __future__ import annotations

import json
import sys

import pytest

from coordinator_core.agent_pipe import bridge, mailbox, roster
from coordinator_core.ops.workflow_fire.notify import notify_terminal


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))


def _record(**kw) -> dict:
    rec = {"fire_id": "f1", "state": "exited", "exit_code": 0, "log_path": "/tmp/f1.log", "notify_to": "ag"}
    rec.update(kw)
    return rec


def test_agy_peer_gets_one_labelled_message(monkeypatch, tmp_path):
    out = tmp_path / "argv.json"
    script = f"import sys,json;open({str(out)!r},'w').write(json.dumps(sys.argv[1:]))"

    class FakeAgy:
        def resume_argv(self, conversation, text):
            return [sys.executable, "-c", script, conversation, text]

    monkeypatch.setattr(bridge, "AgyAdapter", FakeAgy)
    roster.bind("ag", "agy", "conv-9")
    done = notify_terminal(_record())
    assert done["notify_status"] == "handed-off"
    assert done["notified_at"] and "notify_error" not in done
    for _ in range(200):
        if out.exists() and out.read_text():
            break
        import time
        time.sleep(0.05)
    conv, text = json.loads(out.read_text())
    assert conv == "conv-9"
    assert text.startswith("[from workflow.fire (unknown) via bridge]")
    assert "fire_id=f1" in text and "state=exited" in text and "exit_code=0" in text and "/tmp/f1.log" in text
    assert "driver-done" in text


def test_grok_peer_gets_one_labelled_message(monkeypatch):
    seen = []

    class FakeDriver:
        def __init__(self, argv=None):
            pass

        def resume(self, sid):
            seen.append(("resume", sid))
            return "h"

        def prompt(self, handle, text):
            seen.append(("prompt", text))
            yield "ok"

        def close(self, handle):
            pass

        def shutdown(self):
            pass

    monkeypatch.setattr(bridge, "GrokAcpDriver", FakeDriver)
    roster.bind("gk", "grok", "gs-1")
    rec = _record(notify_to="gk", exit_code=None)
    done = notify_terminal(rec)
    assert done["notify_status"] == "handed-off" and done["notified_at"]
    for _ in range(200):
        if len(seen) == 2:
            break
        import time
        time.sleep(0.05)
    assert seen[0] == ("resume", "gs-1")
    assert "exit_code=unknown" in seen[1][1]
    assert seen[1][1].count("fire_id=f1") >= 1


def test_non_roster_sender_is_not_refused():
    roster.bind("cl", "claude", "sess-c")
    msg = bridge.make_message("workflow.fire", "cl", "x")
    assert bridge.refusal(msg) is None
    done = notify_terminal(_record(notify_to="cl"))
    assert done["notify_status"] == "delivered"
    [line] = [json.loads(x) for x in mailbox.drain("sess-c")]
    assert "fire_id=f1" in line["content"]


def test_unbound_notify_to_is_recorded_not_raised():
    done = notify_terminal(_record(notify_to="ghost"))
    assert done["notify_status"] == "unroutable"
    assert "unroutable" in done["notify_error"]
    assert "notified_at" not in done


def test_refused_sets_error():
    roster.bind("cl", "claude", "sess-c")
    bridge.set_mute("cl")
    done = notify_terminal(_record(notify_to="cl"))
    assert done["notify_status"] == "refused"
    assert done["notify_error"].startswith("refused:")
    assert "notified_at" not in done


def test_idempotent_via_notified_at():
    roster.bind("cl", "claude", "sess-c")
    first = notify_terminal(_record(notify_to="cl"))
    second = notify_terminal(first)
    assert second == first
    assert len(mailbox.drain("sess-c")) == 1


def test_no_notify_to_or_non_terminal_is_noop():
    roster.bind("cl", "claude", "sess-c")
    rec = _record(notify_to=None)
    assert notify_terminal(rec) == rec
    rec = _record(notify_to="cl", state="running")
    assert notify_terminal(rec) == rec
    assert mailbox.drain("sess-c") == []


def test_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bridge down")

    monkeypatch.setattr(bridge, "route", boom)
    roster.bind("cl", "claude", "sess-c")
    done = notify_terminal(_record(notify_to="cl"))
    assert "RuntimeError" in done["notify_error"] and "notified_at" not in done


def test_input_record_not_mutated():
    roster.bind("cl", "claude", "sess-c")
    rec = _record(notify_to="cl")
    notify_terminal(rec)
    assert "notified_at" not in rec
