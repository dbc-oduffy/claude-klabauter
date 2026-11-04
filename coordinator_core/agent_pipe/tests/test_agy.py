"""Tests for the Antigravity adapter against the captured real stream."""
from __future__ import annotations

from pathlib import Path

from coordinator_core.agent_pipe.agy import AgyAdapter

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
