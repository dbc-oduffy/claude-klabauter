"""Raw M ask with --brief/--context: every stage is briefed and nothing PM-facing precedes execute."""

from __future__ import annotations

import re
import subprocess

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_compose, cli
from coordinator_core.ops.dispatch_emit.ask_brief import EM_BRIEF_VAR
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.win_portability import no_console_creationflags

_BLITZ = """export const meta = { phases: [{ title: 'Size' }, { title: 'Plan' }] };
const parsed = (typeof args === 'string') ? JSON.parse(args) : args;
const a = await agent('draft the plan', { label: 'plan-draft' });
const b = await agent('review the plan', { label: 'plan-review' });
return { ready: [a, b] };
"""


@pytest.fixture
def emitted(tmp_path, monkeypatch, capsys):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "ctx.md").write_text("context", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    real = ask_compose.compose_ask_script
    got: list[str] = []

    def spy(**kw):
        got.append(real(**kw))
        return got[-1]

    monkeypatch.setattr(ask_compose, "compose_ask_script", spy)
    rc = cli.main(
        ["--ask", "add a medium thing", "--brief", "be careful", "--context", "ctx.md",
         "--repo-root", str(tmp_path)]
    )
    capsys.readouterr()
    assert rc == 0 and len(got) == 1
    return got[0]


def _agent_prompts(script: str) -> list[str]:
    prompts = []
    for m in re.finditer(r"(?<![\w.])agent\(", script):
        end = script.index("label:", m.end())
        prompts.append(script[m.end():end])
    return prompts


def test_every_agent_including_plan_blitz_is_reached_by_the_brief(emitted):
    assert "draft the plan" in emitted and "review the plan" in emitted
    assert f"const {EM_BRIEF_VAR} =" in emitted
    # Plan-blitz agents are shadowed to _askAgent(_EM_BRIEF + p); every other prompt names the var.
    shadow = f"const agent = (p, o) => _askAgent({EM_BRIEF_VAR} + p, o);"
    assert emitted.count(shadow) == 1
    fn_start = emitted.index("async function planBlitz")
    fn_end = emitted.index("draft the plan") + 1
    assert fn_start < emitted.index(shadow) < fn_end
    outside = emitted[:fn_start] + emitted[emitted.index("review the plan"):]
    bare = [p for p in _agent_prompts(outside) if EM_BRIEF_VAR not in p and "plan-" not in p]
    assert bare == []


def test_halts_before_execute_are_the_named_set_and_none_precedes_size(emitted):
    execute_at = emitted.index("phase('execute')")
    size_at = emitted.index("phase('size')")
    before = emitted[:execute_at]
    assert re.findall(r"_halted = \{ halted: ['\"]?([\w-]+)", emitted[:size_at]) == ["usage_limit"]
    literal = set(re.findall(r"_halted = \{ halted: ['\"]?([\w-]+)", before))
    assert literal <= {"refusal", "no-op", "usage_limit", "no_ready", "no-ready"}, literal
    dynamic = re.findall(r"_halted = \{ halted: \(_gate\.halt && _gate\.halt\.kind\)[^}]*", before)
    assert len(dynamic) == 1
    touch = re.findall(r"_gate\.halt\.kind === 'touchpoint'", before)
    assert touch, "touchpoint halt must exist and be gated on the APM branch"
    assert "_apm" in before[before.index("_gate.halt.kind === 'touchpoint'"):][:600]


@pytest.mark.cadence
@pytest.mark.spawns_process
@pytest.mark.parametrize("mode", ["ceo", "pm", "hands-on"])
def test_ceo_mode_m_plan_gate_returns_no_touchpoint(tmp_path, monkeypatch, mode):
    subprocess.run(["git", "init", "-q", str(tmp_path)], capture_output=True, **no_console_creationflags())
    rel = "state/sizings/2026-10-10-m.yaml"
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    doc = {
        "schema": "sizing-object", "name": "m", "intent": "m",
        "estimate": {"tshirt": "M", "provisional": True}, "route": "plan",
        "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-fixture-abc123", "interaction_mode": mode,
        "exit_criterion": {"statement": "done", "accepted": None},
    }
    (tmp_path / rel).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    v = gate(tmp_path, rel)
    assert not (v.halt and v.halt["kind"] == "touchpoint")
