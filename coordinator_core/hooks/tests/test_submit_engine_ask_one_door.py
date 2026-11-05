"""`submit_engine_ask.evaluate` at default arguments: one fire reaches the roadmap run, and the only sizing-time halt is the EM-M-or-below vs engine-XL/XXL misalignment."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from coordinator_core import warp_fire
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit import ask_compose, ask_roadmap
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_MISALIGNED, HALT_ROOM
from coordinator_core.ops.dispatch_emit.ask_gate import gate

REL = "state/sizings/2026-11-01-one-door.yaml"
_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"
_RB = "export const meta = { name: 'rb', phases: [{ title: 'Synthesize', detail: 'x' }] };\nreturn { gate_report_path: 'g.json' };\n"
_BRIEF = "BRIEF-MARK prefer small"
_ROADMAP_FN = f"async function {ask_roadmap.ROADMAP_STAGE_FN}("
_OLD_HALTS = ("which this script does not embed", "the roadmap arm takes no EM brief")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID",
                 "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PLUGIN_ROOT", "DELIVERABLE_ID"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "repo"
    (root / "state" / "sizings").mkdir(parents=True)
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_AGENT_TYPE_HOST", "coordinator")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    monkeypatch.setattr(ask_roadmap, "load_roadmap_blitz_text", lambda plugin_root=None: _RB)
    return root


def _put_sizing(root: Path, *, tshirt: str = "XXL", route: str = "roadmap", mode: str = "pm", xl_exit=None) -> None:
    doc = {
        "schema": "sizing-object", "name": "one door", "intent": "exercise the one door",
        "estimate": {"tshirt": tshirt, "provisional": True}, "route": route, "detents": [],
        "fork": None, "xl_exit": xl_exit, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-one-door-abc123", "interaction_mode": mode,
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-11-01", "mode": mode}},
        "pm_verbatims": [{"turn": 1, "text": "make it so"}],
    }
    (root / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def _payload(root: Path, args) -> dict:
    return {
        "tool_name": "Workflow",
        "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": args},
        "session_id": "sess-one-door",
        "transcript_path": str(root / "transcript.jsonl"),
        "cwd": str(root),
    }


def _fire(root: Path, args) -> str:
    """The script the hook's answer runs; a raw ask answers with a deferred emit, so its `dispatch.emit` runs here."""
    hso = mod.evaluate(_payload(root, args))["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow", hso
    script = Path(hso["updatedInput"]["scriptPath"]).read_text(encoding="utf-8")
    literal = re.search(r"^const PARAMS = (.*);$", script, re.M)
    if literal is None:
        return script
    emitted = mod._default_emit(json.loads(json.loads(literal.group(1))), root)
    assert emitted.get("path"), emitted
    return Path(emitted["path"]).read_text(encoding="utf-8")


def _assert_reaches_roadmap(script: str) -> None:
    assert _ROADMAP_FN in script
    assert "_gate.arm === 'roadmap'" in script
    for text in _OLD_HALTS:
        assert text not in script


def test_ask_with_brief_reaches_the_roadmap_arm(repo):
    _assert_reaches_roadmap(_fire(repo, {"ask": "add the widget", "source": "pm", "brief": _BRIEF}))


def test_ask_without_brief_reaches_the_roadmap_arm(repo):
    _assert_reaches_roadmap(_fire(repo, {"ask": "add the widget", "source": "pm"}))


def test_presized_roadmap_sizing_with_brief_embeds_the_stage_and_does_not_halt(repo):
    _put_sizing(repo)
    script = _fire(repo, {"sizing": REL, "brief": _BRIEF})
    _assert_reaches_roadmap(script)
    assert "_viaRoadmap = true;" in script
    assert gate(repo, REL).halt is None


def test_presized_roadmap_sizing_without_brief_embeds_the_stage_and_does_not_halt(repo):
    _put_sizing(repo)
    script = _fire(repo, {"sizing": REL})
    assert not any(text in script for text in _OLD_HALTS)
    assert gate(repo, REL).halt is None


@pytest.mark.parametrize("engine", ["XL", "XXL"])
def test_gate_halts_on_em_size_m_against_an_engine_xl_or_xxl_naming_both(repo, engine):
    _put_sizing(repo, tshirt=engine)
    halt = gate(repo, REL, em_size="M").halt
    assert halt["kind"] == HALT_MISALIGNED
    assert "M" in halt["reason"] and engine in halt["reason"]


def test_hook_carries_em_size_into_the_composed_script(repo):
    script = _fire(repo, {"ask": "add the widget", "source": "pm", "em_size": "M"})
    assert '"em_size":"M"' in script or 'em_size: "M"' in script


def test_em_size_of_l_or_above_does_not_halt_on_misalignment(repo):
    _put_sizing(repo)
    assert gate(repo, REL, em_size="L").halt is None
    assert gate(repo, REL, em_size="XXL").halt is None


def test_em_size_m_against_a_smaller_engine_size_does_not_halt_on_misalignment(repo):
    _put_sizing(repo, tshirt="M", route="plan")
    assert (gate(repo, REL, em_size="M").halt or {}).get("kind") != HALT_MISALIGNED


def test_no_em_size_against_xxl_halts_nothing_terminal_at_sizing(repo):
    script = _fire(repo, {"ask": "add the widget", "source": "pm"})
    assert ", em_size: " not in script and '"em_size"' not in script
    assert "_gate.halt.apm_xl_exit === true" in script and "phase('xl-exit')" in script
    for mode in ("pm", "ceo"):
        _put_sizing(repo, route="pm-decision", mode=mode)
        halt = gate(repo, REL).halt
        assert halt["kind"] == HALT_ROOM and halt.get("apm_xl_exit") is True, (mode, halt)
    _put_sizing(repo)
    assert gate(repo, REL).halt is None


def test_an_em_size_outside_the_size_set_is_refused_by_the_hook(repo):
    hso = mod.evaluate(_payload(repo, {"ask": "add the widget", "source": "pm", "em_size": "HUGE"}))["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "em_size" in hso["permissionDecisionReason"]
