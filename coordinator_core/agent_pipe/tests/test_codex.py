"""Tests for agent_pipe.codex against the captured codex exec --json stream."""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.agent_pipe import codex as codex_mod
from coordinator_core.agent_pipe.codex import CodexAdapter

FIXTURE = Path(__file__).parent / "fixtures" / "codex_exec.jsonl"
FAKE = ["/fake/node", "/fake/codex.js"]
BANNED = ("dangerously", "yolo", "bypass", "danger-full-access")


@pytest.fixture(autouse=True)
def _fake_resolve(monkeypatch):
    monkeypatch.setattr(codex_mod, "resolve_codex", lambda: list(FAKE))


def test_parse_line_yields_each_agent_message_with_thread_id():
    a = CodexAdapter()
    got = [r for r in map(a.parse_line, FIXTURE.read_text(encoding="utf-8").splitlines()) if r]
    assert got == [("01a19cda-16da-79e2-8b09-9765d2070f06", "ALPHA"), ("01a19cda-16da-79e2-8b09-9765d2070f06", "BETA")]


@pytest.mark.parametrize("line", [
    "", "not json", "[1]",
    '{"type":"turn.completed","usage":{}}',
    '{"type":"item.completed","item":{"type":"command_execution","text":"x"}}',
    '{"type":"item.completed","item":{"type":"agent_message","text":""}}',
    '{"type":"item.started","item":{"type":"agent_message","text":"x"}}',
])
def test_parse_line_ignores_non_messages(line):
    assert CodexAdapter().parse_line(line) is None


def test_parse_line_records_quota_cause_from_error_event():
    a = CodexAdapter()
    assert a.parse_line('{"type":"error","message":"You exceeded your current quota"}') is None
    assert a.cause == "quota_exhausted"
    b = CodexAdapter()
    b.parse_line('{"type":"turn.failed","error":{"message":"rate limit reached (429)"}}')
    assert b.cause == "quota_exhausted"


def test_agent_message_mentioning_quota_is_not_a_cause():
    a = CodexAdapter()
    a.parse_line('{"type":"item.completed","item":{"type":"agent_message","text":"hit the rate limit"}}')
    assert a.cause is None


def test_spawn_argv_starts_with_resolver_output_and_has_no_bypass():
    argv = CodexAdapter().spawn_argv("hi")
    assert argv[:2] == FAKE
    assert argv[2:5] == ["exec", "--json", "--skip-git-repo-check"]
    assert argv[-1] == "hi"
    assert not any(b in " ".join(argv) for b in BANNED)


def test_resume_argv_shape_and_no_bypass():
    argv = CodexAdapter().resume_argv("tid-1", "reply")
    assert argv[:2] == FAKE
    assert argv[2:4] == ["exec", "resume"]
    assert "--json" in argv and argv[-2:] == ["tid-1", "reply"]
    assert not any(b in " ".join(argv) for b in BANNED)


def test_proxy_argv_is_resolver_output_plus_app_server_proxy():
    assert CodexAdapter().proxy_argv() == [*FAKE, "app-server", "proxy"]


def test_spawn_with_conversation_resumes():
    assert CodexAdapter().spawn_argv("t", "tid-1") == CodexAdapter().resume_argv("tid-1", "t")
