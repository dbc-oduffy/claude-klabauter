"""A named reviewer emitted with a non-Anthropic provider configured runs on that provider's
Opus-equivalent model and returns the normal reviewer verdict shape.

Emits through `compose_script` at default arguments, reads the leg's command back out of the emitted
script, and executes that command's entry point (`reviewer_run.main`) against a fake codex.
"""
from __future__ import annotations

import io
import json
import re
import sys

import pytest

from coordinator_core.agent_pipe import reviewer_run
from coordinator_core.agent_pipe.reviewer_provider import OPUS_EQUIVALENT_MODELS, PROVIDER_ENV
from coordinator_core.ops.dispatch_emit.emit import compose_script
from coordinator_core.ops.dispatch_emit.predispatch import AgentSpec
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

STAFF_ENG = "coordinator:staff-eng"
BRIEF = "Review this plan: BRIEF-MARKER-E2E"

FAKE_CODEX = '''
import json, sys
args = sys.argv[1:]
model = args[args.index("-m") + 1]
prompt = args[-1]
def say(text):
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}))
say("reading the plan")
say("Done.\\n```json\\n" + json.dumps({"verdict": "READY_WITH_CONCERNS", "findings": [
    {"severity": "minor", "location": "plan", "finding": "ran on " + model, "saw_brief": "BRIEF-MARKER-E2E" in prompt,
     "saw_definition": "STAFF-ENG-DEFINITION-BODY" in prompt}]}) + "\\n```")
'''

_LEG_COMMAND = re.compile(r"agent_type: '([^']+)', provider: '([^']+)', model: '([^']+)'")


def _emit_plan_review_wave() -> str:
    spec = AgentSpec(
        key="docs/plans/p.md",
        label="plan-review",
        phase="Review",
        agent_type=STAFF_ENG,
        model="opus",
        prompt=BRIEF,
        schema="falsifier_integrity_result",
    )
    wave = [[WaveRow(id="A", title="t", surface="s", writes=["a.py"], reads=[], depends_on=[])]]
    return compose_script(
        wave,
        name="wf",
        description="plan review",
        plan_path="docs/plans/p.md",
        predispatch=True,
        review_specs=[spec],
        **REVIEW_KW,
    )


@pytest.fixture
def codex_home(monkeypatch, tmp_path):
    agents = tmp_path / ".claude" / "plugins" / "coordinator" / "agents"
    agents.mkdir(parents=True)
    (agents / "staff-eng.md").write_text(
        "---\nname: staff-eng\nmodel: opus\n---\nSTAFF-ENG-DEFINITION-BODY\n", encoding="utf-8"
    )
    fake = tmp_path / "fake_codex.py"
    fake.write_text(FAKE_CODEX, encoding="utf-8")
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr(
        "coordinator_core.agent_pipe.codex.resolve_codex", lambda: [sys.executable, str(fake)]
    )
    return tmp_path


def test_configured_staff_eng_leg_runs_reviewer_run_on_the_codex_opus_equivalent(monkeypatch):
    monkeypatch.setenv(PROVIDER_ENV, f"{STAFF_ENG}=codex")
    script = _emit_plan_review_wave()
    match = _LEG_COMMAND.search(script)
    assert match, "the staff-eng leg does not submit a named review"
    assert "coordinator-invoke codex_runner.start" in script
    assert "coordinator-invoke codex_runner.result" in script
    assert "python -m coordinator_core.agent_pipe.reviewer_run" not in script
    assert match.groups() == (STAFF_ENG, "codex", OPUS_EQUIVALENT_MODELS["codex"])
    assert "label: 'plan-review:start'" in script
    assert f"agentType: '{STAFF_ENG}'" not in script


def test_unconfigured_staff_eng_leg_stays_on_claude(monkeypatch):
    monkeypatch.delenv(PROVIDER_ENV, raising=False)
    script = _emit_plan_review_wave()
    assert "reviewer_run" not in script
    assert f"agentType: '{STAFF_ENG}'" in script


def test_emitted_leg_executes_against_fake_codex_and_returns_the_verdict_shape(
    monkeypatch, codex_home, capsys
):
    monkeypatch.setenv(PROVIDER_ENV, f"{STAFF_ENG}=codex")
    agent_type, provider, model = _LEG_COMMAND.search(_emit_plan_review_wave()).groups()
    monkeypatch.setattr(sys, "stdin", io.StringIO(BRIEF))

    assert reviewer_run.main([agent_type, provider, model]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "READY_WITH_CONCERNS"
    assert out["provider"] == "codex" and out["model"] == OPUS_EQUIVALENT_MODELS["codex"]
    (finding,) = out["findings"]
    assert finding["finding"] == f"ran on {OPUS_EQUIVALENT_MODELS['codex']}"
    assert finding["saw_brief"] is True and finding["saw_definition"] is True
    assert "error" not in out


def test_emitted_leg_with_a_failing_codex_returns_failed_not_a_pass(monkeypatch, codex_home, capsys):
    monkeypatch.setenv(PROVIDER_ENV, f"{STAFF_ENG}=codex")
    agent_type, provider, model = _LEG_COMMAND.search(_emit_plan_review_wave()).groups()
    (codex_home / "fake_codex.py").write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", io.StringIO(BRIEF))

    assert reviewer_run.main([agent_type, provider, model]) == 1
    assert json.loads(capsys.readouterr().out)["verdict"] == reviewer_run.FAILED


def test_windows_reviewer_start_uses_powershell_json_stdin():
    from pathlib import Path
    from coordinator_core.agent_pipe.reviewer_provider import ProviderChoice
    from coordinator_core.ops.dispatch_emit.emit import _external_reviewer_thunk_js
    spec = AgentSpec(key="p", label="r", phase="Review", agent_type=STAFF_ENG,
                     model="opus", prompt="Review Unicode: café", schema="falsifier_integrity_result")
    script = _external_reviewer_thunk_js(spec, ProviderChoice("codex", "gpt-6-astra"), None,
                                        Path("C:/work/repo"))
    assert "@\\'" in script and "\\'@ | coordinator-invoke codex_runner.start" in script
    assert "<<" not in script and "--repo" not in script
    assert "padStart(4, '0')" in script
    assert "coordinator-invoke codex_runner.result" in script
