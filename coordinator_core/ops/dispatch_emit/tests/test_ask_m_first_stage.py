"""Raw M ask through the ask arm: the brief reaches every agent, plan-blitz included, and no PM detent precedes execute."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, ask_plan_blitz, cli
from coordinator_core.ops.dispatch_emit.ask_brief import EM_BRIEF_VAR
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan

def _resolve_plugin_root():
    if os.environ.get("CLAUDE_PLUGIN_ROOT"):
        return os.environ["CLAUDE_PLUGIN_ROOT"]
    from coordinator_core.warm.caller_context import resolve_caller_context

    return resolve_caller_context().plugin_root


_PLUGIN_ROOT = _resolve_plugin_root()  # resolved at import, before conftest clears the env var
_ALLOWED_PRE_EXECUTE_HALTS = {"usage_limit", "refusal"}


@pytest.fixture
def blitz_text():
    try:
        return ask_plan_blitz.load_plan_blitz_text(_PLUGIN_ROOT)
    except ask_plan_blitz.AskPlanBlitzRefused:
        pytest.skip("plugin root unresolved")


def _emit_m_ask(repo, capsys, blitz_text, monkeypatch):
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: blitz_text)
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "ctx.md").write_text("context", encoding="utf-8")
    rc = cli.main(
        ["--ask", "add a small thing to the engine", "--brief", "be careful", "--context", "docs/ctx.md",
         "--repo-root", str(repo)]
    )
    assert rc == 0
    return Path(json.loads(capsys.readouterr().out)["path"]).read_text(encoding="utf-8")


def _agent_prompts(script: str) -> list[str]:
    out = []
    for m in re.finditer(r"(?<![\w.])agent\(", script):
        end = script.index("label:", m.end()) if "label:" in script[m.end():] else m.end() + 200
        out.append(script[m.end():end])
    return out


def _balanced_function(script: str, name: str) -> str:
    start = script.index(f"async function {name}(")
    depth, i = 0, script.index("{", start)
    for j in range(i, len(script)):
        depth += script[j] == "{"
        depth -= script[j] == "}"
        if depth == 0:
            return script[start:j + 1]
    raise AssertionError("unbalanced")


def test_every_agent_in_the_emitted_script_is_reached_by_the_brief(
    repo, capsys, blitz_text, monkeypatch  # noqa: F811
):
    script = _emit_m_ask(repo, capsys, blitz_text, monkeypatch)
    blitz = _balanced_function(script, "planBlitz")
    assert blitz.split("\n", 2)[1].strip() == f"const agent = (p, o) => _askAgent({EM_BRIEF_VAR} + p, o);"
    outside = script.replace(blitz, "")
    prompts = _agent_prompts(outside)
    assert len(prompts) >= 9
    assert [p for p in prompts if EM_BRIEF_VAR not in p] == []
    assert _agent_prompts(blitz.split("\n", 2)[2])  # plan-blitz agents exist and ride the shadow


def test_halts_before_execute_are_only_the_named_kinds_and_none_precedes_size(
    repo, capsys, blitz_text, monkeypatch  # noqa: F811
):
    script = _emit_m_ask(repo, capsys, blitz_text, monkeypatch)
    size_at = script.index("phase('size')")
    execute_at = script.index("phase('execute')")
    assignments = [m for m in re.finditer(r"_halted = \{", script) if m.start() < execute_at]
    helper = script.index("function _haltOnUsageLimit(")
    helper_end = script.index("\n  }", helper)
    assert assignments
    assert all(m.start() > size_at or helper < m.start() < helper_end for m in assignments)
    literal = {
        m.group(1)
        for m in re.finditer(r"halted: '([\w-]+)'", script[size_at:execute_at])
    }
    assert literal <= _ALLOWED_PRE_EXECUTE_HALTS
    assert "(_gate.halt && _gate.halt.kind)" in script  # room / touchpoint ride the gate verdict
    assert "plan-blitz reported no ready plan" in script[size_at:execute_at]


def _git_init(repo):
    (repo / ".git").mkdir(exist_ok=True)


def test_ceo_mode_m_plan_route_gates_with_no_touchpoint(repo):  # noqa: F811
    _git_init(repo)
    _put(repo, tshirt="M", route="plan", mode="ceo")
    v = gate(repo, REL)
    assert v.halt is None or v.halt["kind"] != "touchpoint"
    assert v.arm == "m_plus" or v.halt is not None  # an engine-mint refusal is not a touchpoint


def test_resumed_m_sizing_passes_its_unfired_plan_as_the_baton_plan_path(
    repo, capsys, blitz_text, monkeypatch  # noqa: F811
):
    _git_init(repo)
    _put(repo, tshirt="M", route="plan", mode="ceo", plan=PLAN_REL)
    _plan(repo)
    v = gate(repo, REL)
    assert v.halt is None, v.halt
    assert v.resume_plan == PLAN_REL
    script = _emit_m_ask(repo, capsys, blitz_text, monkeypatch)
    assert "planPath: _gate.resume_plan ?? null" in script
