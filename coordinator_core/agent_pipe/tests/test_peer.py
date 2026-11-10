"""Peer relay: participants address each other by roster name; no Claude session is in the path."""
from __future__ import annotations

import json
import sys

import pytest

from coordinator_core.agent_pipe import peer
from coordinator_core.agent_pipe.contract import mailbox_path
from coordinator_core.agent_pipe.tests.fixtures.fake_peer import SCRIPT, FakePeerAdapter, conversation_of
from coordinator_core.session import core

pytestmark = pytest.mark.complete

LOG = "peer-log"
READY = "ready"  # a resume peer's first run must emit a message so its conversation id is known


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FAKE_PEER_ALWAYS", raising=False)
    return tmp_path / "home"


def stdin_peer(name, opening="", prompt=""):
    argv = [sys.executable, SCRIPT, "stdin", name, opening]
    return peer.Participant(name, FakePeerAdapter(name, "stdin"), prompt, peer.STDIN, argv)


def resume_peer(name, opening=READY):
    return peer.Participant(name, FakePeerAdapter(name, "resume"), opening, peer.RESUME)


def lines(session):
    path = mailbox_path(session)
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def contents(session=LOG):
    return [(m["sender_agent"], m["content"]) for m in lines(session) if m["content"] != READY]


def assert_no_pid_alive(result):
    assert result.pids
    assert not [pid for pid in result.pids if core.pid_alive(pid)]


def test_stdin_peer_to_resume_peer_and_back(home):
    a = stdin_peer("agy1", opening="@codex1 hello")
    b = resume_peer("codex1")
    result = peer.relay([a, b], idle=0.4)
    assert contents() == [
        ("agy1", "@codex1 hello"),
        ("codex1", "@agy1 echo:hello"),
        ("agy1", "got:echo:hello"),
    ]
    assert result.hops == 2
    assert result.stopped_by == "quiet"
    assert result.conversations == {"agy1": conversation_of("agy1"), "codex1": conversation_of("codex1")}
    assert_no_pid_alive(result)


def test_resume_peer_to_resume_peer_exits_without_idle_wait(home):
    result = peer.relay([resume_peer("c1", "@c2 ping"), resume_peer("c2")], idle=60)
    assert contents() == [("c1", "@c2 ping"), ("c2", "@c1 echo:ping"), ("c1", "got:echo:ping")]
    assert result.stopped_by == "exited"
    assert_no_pid_alive(result)


def test_stdin_peer_to_stdin_peer(home):
    result = peer.relay([stdin_peer("a1", "@a2 yo"), stdin_peer("a2")], idle=0.4)
    assert contents() == [("a1", "@a2 yo"), ("a2", "@a1 echo:yo"), ("a1", "got:echo:yo")]
    assert_no_pid_alive(result)


def test_participant_prompt_is_the_first_stdin_turn(home):
    result = peer.relay([stdin_peer("a1", prompt="[from boss] work")], idle=0.3)
    assert contents() == [("a1", "@boss echo:work")]
    assert_no_pid_alive(result)


def test_message_to_claude_observer_lands_in_its_mailbox_and_log(home):
    result = peer.relay([resume_peer("codex1", "@claude1 done")], observers={"claude1": "sess9"})
    assert contents("sess9") == [("codex1", "@claude1 done")]
    assert contents() == [("codex1", "@claude1 done")]
    assert result.hops == 0
    assert_no_pid_alive(result)


def test_unaddressed_and_unknown_targets_are_logged_not_routed(home):
    result = peer.relay([resume_peer("c1", "@nobody hi"), resume_peer("c2", "plain")])
    assert sorted(contents()) == [("c1", "@nobody hi"), ("c2", "plain")]
    assert result.hops == 0
    assert_no_pid_alive(result)


def test_self_address_is_not_routed(home):
    result = peer.relay([resume_peer("c1", "@c1 me")])
    assert contents() == [("c1", "@c1 me")]
    assert result.hops == 0


def test_hop_limit_stops_a_ping_pong_and_reaps_children(home, monkeypatch):
    monkeypatch.setenv("FAKE_PEER_ALWAYS", "1")
    result = peer.relay([resume_peer("c1", "@c2 x"), resume_peer("c2")], max_hops=3)
    assert result.stopped_by == "hop_limit"
    assert result.hops == 3
    assert_no_pid_alive(result)


def test_deadline_kills_a_child_that_never_exits(home):
    result = peer.relay([stdin_peer("a1")], idle=60, deadline=0.5)
    assert result.stopped_by == "deadline"
    assert_no_pid_alive(result)


def test_duplicate_names_and_observer_clash_are_refused(home):
    with pytest.raises(ValueError):
        peer.relay([resume_peer("c1"), resume_peer("c1")])
    with pytest.raises(ValueError):
        peer.relay([resume_peer("c1")], observers={"c1": "s"})


def test_missing_binary_is_reported_as_exit_127_not_raised(home):
    p = peer.Participant("a1", FakePeerAdapter("a1", "stdin"), "", peer.STDIN, ["/nonexistent/agy-binary"])
    result = peer.relay([p, resume_peer("c1")])
    assert result.exit_codes["a1"] == 127


def test_real_adapters_build_native_argv_without_bypass_flags(home):
    agy = peer.agy_participant("agy1", conversation="cid")
    assert agy.mode == peer.STDIN
    assert agy.argv[1:] == ["--conversation", "cid", "--input-format", "stream-json", "--output-format", "stream-json"]
    codex = peer.codex_participant("codex1")
    assert codex.mode == peer.RESUME
    for argv in (agy.argv, codex.adapter.resume_argv("tid", "hi")):
        assert not [a for a in argv if "dangerously" in a or "bypass" in a]


def test_grok_participant_is_resume_mode_with_read_only_allowlist_argv_and_no_bypass(home, monkeypatch):
    monkeypatch.setattr("coordinator_core.codex_runner.grok.resolve_native_cli", lambda name: "/bin/grok")
    g = peer.grok_participant("grok1", conversation="sid-1")
    assert g.mode == peer.RESUME and g.conversation == "sid-1"
    argv = g.adapter.resume_argv("sid-1", "hi")
    assert argv[:3] == ["/bin/grok", "-p", "hi"] and argv[argv.index("-s") + 1] == "sid-1"
    assert "--permission-mode" not in argv
    assert argv[argv.index("--tools") + 1] == "Read,Grep"
    denied = [argv[i + 1] for i, a in enumerate(argv) if a == "--deny"]
    assert {"Edit", "Write", "Bash"} <= set(denied)
    assert not [a for a in argv if "always-approve" in a or "bypass" in a or "yolo" in a]
    fresh = g.adapter.spawn_argv("hello")
    assert fresh[fresh.index("-s") + 1] == g.adapter.session


def test_grok_adapter_reads_text_events_and_result_only_as_fallback(home):
    a = peer.GrokAdapter()
    assert a.parse_line('{"type":"text","text":"x"}') is None  # no session yet
    a.session = "s"
    assert a.parse_line("not json") is None
    assert a.parse_line('{"type":"system","text":"boot"}') is None
    assert a.parse_line('{"type":"assistant","text":" hi "}') == ("s", "hi")
    assert a.parse_line('{"type":"result","result":"hi"}') is None
    b = peer.GrokAdapter()
    b.session = "s"
    assert b.parse_line('{"type":"result","result":"only"}') == ("s", "only")


def test_json_turn_is_the_agy_user_event_schema():
    assert json.loads(peer.json_turn("hi\nthere")) == {"event": "user", "message": {"content": "hi\nthere"}}
