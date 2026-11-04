"""Tests for agent_pipe.post: agy-originated mailbox lines carry provenance."""
from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest

from coordinator_core.agent_pipe import mailbox, post
from coordinator_core.agent_pipe.contract import mailbox_path


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))


def test_post_line_carries_provenance(capsys):
    assert post.main(["--to", "sess-1", "--from-conversation", "conv-9", "hi there"]) == 0
    assert capsys.readouterr().out.strip() == str(mailbox_path("sess-1"))
    (line,) = mailbox.drain("sess-1")
    obj = json.loads(line)
    assert obj["sender_agent"] == "antigravity"
    assert obj["sender_conversation"] == "conv-9"
    assert obj["origin"] == "agent-initiated"
    assert obj["to_session"] == "sess-1"
    assert obj["content"] == "hi there"


def test_dash_reads_stdin(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("multi\nline"))
    assert post.main(["--to", "s", "--from-conversation", "c", "-"]) == 0
    assert json.loads(mailbox.drain("s")[0])["content"] == "multi\nline"


@pytest.mark.parametrize("bad", ["../x", "a/b", ".."])
def test_invalid_to_is_refused_with_exit_2(bad, tmp_path):
    assert post.main(["--to", bad, "--from-conversation", "c", "x"]) == 2
    assert not (tmp_path / "agent-pipe" / "mailboxes").exists()


def test_name_addressing_queues_labelled_bridge_message(capsys):
    from coordinator_core.agent_pipe import bridge, roster

    roster.bind("a", "codex", "t-1", argv=["x"])
    roster.bind("b", "claude", "sess-b")
    assert post.main(["--from", "a", "--to", "b", "hello"]) == 0
    assert capsys.readouterr().out.strip() == str(bridge.inbox_path())
    bridge.drain_once()
    (line,) = mailbox.drain("sess-b")
    obj = json.loads(line)
    assert obj["content"] == "[from a (codex) via bridge]\nhello"
    assert obj["provenance"]["from_identity"] == "a"


def test_name_addressing_queues_before_rebind_delivers_to_new_id():
    from coordinator_core.agent_pipe import bridge, roster

    roster.bind("b", "claude", "old")
    assert post.main(["--from", "a", "--to", "b", "hi"]) == 0
    roster.bind("b", "claude", "new")
    bridge.drain_once()
    assert mailbox.drain("old") == []
    assert len(mailbox.drain("new")) == 1


def test_invalid_name_is_refused_with_exit_2():
    assert post.main(["--from", "../x", "--to", "b", "hi"]) == 2


def test_neither_sender_form_is_a_usage_error():
    with pytest.raises(SystemExit) as exc:
        post.main(["--to", "b", "hi"])
    assert exc.value.code == 2


def test_cli_roster_and_bridge_send(capsys):
    from coordinator_core.agent_pipe import __main__ as cli, bridge

    assert cli.main(["roster", "bind", "b", "--kind", "claude", "--id", "s1"]) == 0
    assert cli.main(["roster", "bind", "b", "--kind", "claude", "--id", "s2"]) == 0
    capsys.readouterr()
    assert cli.main(["roster", "resolve", "b"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "s2"
    assert cli.main(["roster", "resolve", "nobody"]) == 1
    assert cli.main(["bridge", "send", "--from", "a", "--to", "b", "hi"]) == 0
    assert cli.main(["bridge", "serve", "--max-cycles", "1"]) == 0
    assert len(mailbox.drain("s2")) == 1
    assert bridge.inbox_path().exists() is False


def test_cli_route_pause_refuses_delivery():
    from coordinator_core.agent_pipe import __main__ as cli

    cli.main(["roster", "bind", "b", "--kind", "claude", "--id", "s2"])
    assert cli.main(["bridge", "route", "--pause", "a", "b"]) == 0
    assert cli.main(["bridge", "send", "--from", "a", "--to", "b", "--now", "hi"]) == 1
    assert mailbox.drain("s2") == []
    assert cli.main(["bridge", "route", "--resume", "a", "b"]) == 0
    assert cli.main(["bridge", "send", "--from", "a", "--to", "b", "--now", "hi"]) == 0
    assert len(mailbox.drain("s2")) == 1


def test_cli_roster_bind_live(monkeypatch, capsys):
    from coordinator_core.agent_pipe import __main__ as cli, live, roster

    monkeypatch.setattr(live, "find_live", lambda *a, **k: {"sessions": [{"agent": "antigravity", "pid": 1, "cwd": "/", "conversation": "conv-7"}]})
    assert cli.main(["roster", "bind", "g", "--live"]) == 0
    assert roster.resolve("g")["kind"] == "agy" and roster.resolve("g")["id"] == "conv-7"
    monkeypatch.setattr(live, "find_live", lambda *a, **k: {"sessions": []})
    assert cli.main(["roster", "bind", "g", "--live"]) == 1


def test_post_imports_neither_ipc_nor_ops():
    code = (
        "import sys; import coordinator_core.agent_pipe.post;"
        "bad=[m for m in sys.modules if m=='coordinator_core.ipc' or m=='coordinator_core.ops' or m.startswith('coordinator_core.ops.')];"
        "sys.exit(1 if bad else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
