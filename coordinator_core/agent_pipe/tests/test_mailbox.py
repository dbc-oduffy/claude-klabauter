"""Tests for agent_pipe contract path guards and the append/drain mailbox."""
from __future__ import annotations

import json
import threading

import pytest

from coordinator_core.agent_pipe import mailbox
from coordinator_core.agent_pipe.contract import PipeMessage, mailbox_path, run_dir, session_lock_path
from coordinator_core.workflow_watch.mailbox_line import wait_for_line


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))


def _msg(i: int, session: str = "sess-1", content: str = "hello") -> PipeMessage:
    return PipeMessage(f"m{i}", "antigravity", "conv-1", session, content, "2026-11-02T00:00:00Z")


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "a\\b", "../x", "a\0b"])
def test_ids_that_escape_are_rejected(bad):
    with pytest.raises(ValueError):
        run_dir(bad)
    with pytest.raises(ValueError):
        mailbox_path(bad)


def test_paths_live_under_settings_home(tmp_path):
    assert run_dir("r1").is_relative_to(tmp_path / "agent-pipe")
    assert mailbox_path("s1").is_relative_to(tmp_path / "agent-pipe")


def test_session_lock_path_under_home_and_rejects_escape(tmp_path):
    p = session_lock_path("codex", "conv-1")
    assert p == tmp_path / "agent-pipe" / "sessions" / "codex" / "conv-1.lock"
    for bad in ("../x", "a/b", ".."):
        with pytest.raises(ValueError):
            session_lock_path("codex", bad)
        with pytest.raises(ValueError):
            session_lock_path(bad, "conv-1")


def test_json_line_without_origin_is_the_six_key_line():
    line = _msg(1).to_json_line()
    assert list(json.loads(line)) == ["id", "sender_agent", "sender_conversation", "to_session", "content", "ts"]
    assert line == (
        '{"id":"m1","sender_agent":"antigravity","sender_conversation":"conv-1",'
        '"to_session":"sess-1","content":"hello","ts":"2026-11-02T00:00:00Z"}'
    )


def test_json_line_with_origin_carries_it():
    m = PipeMessage("m1", "antigravity", "c", "s", "x", "t", origin="agent-initiated")
    assert json.loads(m.to_json_line())["origin"] == "agent-initiated"


def test_json_line_is_single_line_and_round_trips():
    m = _msg(1, content="a\nb")
    line = m.to_json_line()
    assert "\n" not in line
    assert json.loads(line)["content"] == "a\nb"


def test_append_then_wait_for_line_returns_it():
    mailbox.append(_msg(1))
    got = wait_for_line(str(mailbox_path("sess-1")), from_start=True, poll_seconds=0.05)
    assert json.loads(got)["id"] == "m1"


def test_wait_for_line_armed_before_append_wakes_on_it():
    path = mailbox_path("sess-1")
    path.parent.mkdir(parents=True)
    path.write_text("")
    threading.Timer(0.2, lambda: mailbox.append(_msg(2))).start()
    assert json.loads(wait_for_line(str(path), poll_seconds=0.05))["id"] == "m2"


def test_concurrent_appenders_never_interleave():
    n = 8
    per = 25
    big = "x" * 3000

    def work(t: int) -> None:
        for k in range(per):
            mailbox.append(_msg(t * 1000 + k, content=big))

    threads = [threading.Thread(target=work, args=(t,)) for t in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = mailbox.drain("sess-1")
    assert len(lines) == n * per
    assert len({json.loads(line)["id"] for line in lines}) == n * per


def test_drain_returns_lines_and_next_append_starts_fresh_file():
    mailbox.append(_msg(1))
    mailbox.append(_msg(2))
    lines = mailbox.drain("sess-1")
    assert [json.loads(x)["id"] for x in lines] == ["m1", "m2"]
    assert not mailbox_path("sess-1").exists()
    assert mailbox.drain("sess-1") == []
    mailbox.append(_msg(3))
    assert [json.loads(x)["id"] for x in mailbox.drain("sess-1")] == ["m3"]
