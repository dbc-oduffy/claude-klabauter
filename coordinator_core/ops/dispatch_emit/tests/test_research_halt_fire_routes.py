"""A research halt's fire, called through the submit hook, emits on the research route (`from_sizing`)."""

from __future__ import annotations

import json
import re

import yaml

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit.ask_gate import gate

REL = "state/sizings/2026-10-16-research-fire.yaml"


def _put(root):
    doc = {
        "schema": "sizing-object",
        "name": "research fixture",
        "intent": "survey",
        "estimate": {"tshirt": "S", "provisional": True},
        "route": "spec-dispatch",
        "detents": [],
        "fork": None,
        "xl_exit": None,
        "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-fixture-abc123",
        "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": None},
        "research": {"value_class": "corpus", "appetite": "medium", "questions": ["which?"]},
    }
    (root / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def test_research_halt_fire_emits_from_sizing(tmp_path, monkeypatch):
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    _put(tmp_path)

    halt = gate(tmp_path, REL).halt
    assert halt["kind"] == "research" and len(halt["fire"]) == 1
    m = re.fullmatch(r"Workflow\((\{.*\})\)", halt["fire"][0])
    call = json.loads(re.sub(r"(\{|, )(name|args):", r'\1"\2":', m.group(1)))
    assert call["args"] == {"sizing": REL}

    seen: list[dict] = []

    def emit(params, root):
        seen.append(params)
        return {"path": str(tmp_path / "run.js"), "run_id": "r1", "fire_args": {}}

    payload = {
        "tool_name": "Workflow",
        "cwd": str(tmp_path),
        "session_id": "s1",
        "tool_input": {"name": call["name"], "args": call["args"]},
    }
    env = mod.evaluate(payload, emit=emit)
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert seen[0]["from_sizing"] == REL
    assert "ask" not in seen[0] and "sizing_path" not in seen[0]
