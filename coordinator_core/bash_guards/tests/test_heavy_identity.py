"""Fixture-payload tests for the heavy-admission identity leg, one per caller class."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards._heavy_identity import identity_verdict, load_allowlist

ALLOW = load_allowlist()
HEX = "0123456789abcdef"


def _verdict(payload, **kw):
    kw.setdefault("allowlist", ALLOW)
    return identity_verdict(payload, **kw)


def test_seed_allowlist_contents():
    assert ALLOW == {
        "coordinator:staff-eng",
        "coordinator:staff-data-sci",
        "coordinator:staff-ux",
        "coordinator:senior-front-end",
        "coordinator:eng-director",
        "game-dev:staff-game-dev",
        "coordinator:test-runner",
        "coordinator:exit-criterion-falsifier",
        "example-game-repo-control:ue-infra-engineer",
    }


@pytest.mark.parametrize("payload", [
    {},
    {"agent_id": ""},
    {"agent_id": "   ", "agent_type": ""},
    {"agent_type": "coordinator:some-main-agent"},
    {"transcript_path": "projects\\p\\sess.jsonl"},
])
def test_main_thread_allowed(payload):
    assert _verdict(payload).allowed


@pytest.mark.parametrize("agent_type", sorted(ALLOW))
def test_listed_persona_allowed(agent_type):
    assert _verdict({"agent_id": "a" + HEX, "agent_type": agent_type}).allowed


@pytest.mark.parametrize("agent_type", [
    "general-purpose", "Explore", "coordinator:executor", "workflow-subagent",
    "alice-bob", "", "unknown", None, 7, ["coordinator:staff-eng"],
])
def test_other_agent_denied(agent_type):
    p = {"agent_id": "a" + HEX, "agent_type": agent_type}
    assert not _verdict(p).allowed


def test_absent_agent_type_with_agent_id_denied():
    assert not _verdict({"agent_id": "a" + HEX}).allowed


def test_teammate_denied():
    p = {"agent_id": "aalice-" + HEX, "agent_type": "alice"}
    assert not _verdict(p).allowed


def test_non_string_agent_id_denied_even_for_listed_type():
    assert not _verdict({"agent_id": 5, "agent_type": "coordinator:staff-eng"}).allowed


@pytest.mark.parametrize("path", [
    "projects\\p\\sess\\subagents\\agent-a1.jsonl",
    "projects/p/sess/subagents/agent-a1.jsonl",
])
def test_subagent_transcript_without_agent_id_denied(path):
    assert not _verdict({"transcript_path": path}).allowed


def test_listed_persona_denied_while_workflow_run_live():
    p = {"agent_id": "a" + HEX, "agent_type": "coordinator:staff-eng"}
    v = _verdict(p, workflow_runs=["run-1"])
    assert not v.allowed and "workflow" in v.reason


def test_main_thread_unaffected_by_workflow_run():
    assert _verdict({}, workflow_runs=["run-1"]).allowed


def test_unreadable_allowlist_admits_only_main_thread(tmp_path):
    empty = load_allowlist(tmp_path / "missing.txt")
    assert empty == frozenset()
    assert identity_verdict({}, allowlist=empty).allowed
    p = {"agent_id": "a" + HEX, "agent_type": "coordinator:staff-eng"}
    assert not identity_verdict(p, allowlist=empty).allowed


def test_allowlist_parsing_exact_lines_and_comments(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("# c\n\nfoo:bar  # trailing\n  baz  \n", encoding="utf-8")
    assert load_allowlist(f) == {"foo:bar", "baz"}
    p = {"agent_id": "a" + HEX, "agent_type": "foo:ba"}
    assert not identity_verdict(p, allowlist=load_allowlist(f)).allowed
