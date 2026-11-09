"""Tests for the Antigravity adapter against the captured real stream."""
from __future__ import annotations

from pathlib import Path

import sys

from coordinator_core.agent_pipe import agy as agy_mod
from coordinator_core.agent_pipe.agy import AgyAcpDriver, AgyAdapter
from coordinator_core.agent_pipe.session_driver import SessionHandle

FIXTURE = Path(__file__).parent / "fixtures" / "agy_stream.ndjson"
CONV = "894074b8-1367-4122-8a8f-ef1f745ba338"


def _lines() -> list[str]:
    return FIXTURE.read_text(encoding="utf-8").splitlines()


def test_parse_line_yields_only_agent_messages():
    parsed = [AgyAdapter().parse_line(line) for line in _lines()]
    assert [p for p in parsed if p is not None] == [(CONV, "ALPHA\n\nBETA")]


def test_parse_line_ignores_garbage():
    a = AgyAdapter()
    assert a.parse_line("") is None
    assert a.parse_line("not json") is None
    assert a.parse_line("[1]") is None
    assert a.parse_line('{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"x"}}') is None


def test_parse_line_records_quota_cause_from_error_event():
    a = AgyAdapter()
    assert a.parse_line('{"event":"error","error":{"message":"RESOURCE_EXHAUSTED: quota exceeded"}}') is None
    assert a.cause == "quota_exhausted"


def test_ordinary_stream_leaves_no_cause():
    a = AgyAdapter()
    for line in _lines():
        a.parse_line(line)
    assert a.cause is None


def test_argv_shapes_and_no_bypass(monkeypatch):
    monkeypatch.setattr("coordinator_core.agent_pipe.agy.shutil.which", lambda name: "/bin/agy")
    a = AgyAdapter()
    spawn = a.spawn_argv("hi")
    assert spawn == ["/bin/agy", "-p", "hi", "--output-format", "stream-json"]
    resume = a.resume_argv("c1", "yo")
    assert resume == ["/bin/agy", "--conversation", "c1", "-p", "yo", "--output-format", "stream-json"]
    assert a.spawn_argv("yo", conversation="c1") == resume
    for argv in (spawn, resume):
        assert not any("dangerously" in x or x == "--settings" for x in argv)


def test_agy_missing_from_path_falls_back_to_bare_name(monkeypatch):
    monkeypatch.setattr("coordinator_core.agent_pipe.agy.shutil.which", lambda name: None)
    assert AgyAdapter().spawn_argv("p")[0] == "agy"


FAKE_ACP = [sys.executable, str(Path(__file__).parent / "fixtures" / "fake_acp_agent.py")]


def test_agy_acp_driver_argv_is_agy_acp_with_no_bypass(monkeypatch):
    monkeypatch.setattr(agy_mod.shutil, "which", lambda name: None)
    assert AgyAcpDriver()._argv == ["agy", "acp"]
    monkeypatch.setattr(agy_mod.shutil, "which", lambda name: "/opt/agy")
    assert AgyAcpDriver()._argv == ["/opt/agy", "acp"]
    assert AgyAcpDriver(argv=FAKE_ACP)._argv == FAKE_ACP


def test_agy_acp_driver_starts_prompts_and_resumes_over_acp(monkeypatch):
    monkeypatch.delenv("FAKE_ACP_LOG", raising=False)
    with AgyAcpDriver(argv=FAKE_ACP) as d:
        h = d.start("/w")
        assert isinstance(h, SessionHandle) and h.cwd == "/w"
        ups = list(d.prompt(h, "hi"))
        assert ups[-1].kind == "done"
        assert d.resume(h.session_id).session_id == h.session_id


def test_agy_adapter_headless_argv_unchanged_by_acp_driver(monkeypatch):
    monkeypatch.setattr(agy_mod.shutil, "which", lambda name: None)
    assert AgyAdapter().spawn_argv("p") == ["agy", "-p", "p", "--output-format", "stream-json"]
