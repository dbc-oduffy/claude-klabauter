"""Acceptance: a free-text research ask sizes to the research hand-back and composes no plan or execute phase."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

import coordinator_core.sizing_assemble as sa
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_gate import _handler
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.session import record_homes
from coordinator_core.warp_fire import fire_command

_NAME = "2026-10-16-research-ask.yaml"
_DRAFT = """schema: sizing-object
intent: "Survey how the thing works"
estimate:
  tshirt: XS
  provisional: true
route: dispatch
detents: []
fork: null
xl_exit: null
status: draft
premise:
  provenance: unrecorded
  evidence: PLACEHOLDER - cite the evidence
deliverable_id: null
"""
_RESEARCH_FLAGS = ("--research-class", "deep", "--research-source", "repo", "--research-question", "How does X work?")


def _stub_wrap(text):
    return "  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]


@pytest.fixture(scope="module")
def script() -> str:
    return compose_ask_script(
        repo_root="REPO",
        prompt="Survey how the sizing gate routes asks and write up the findings",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=_stub_wrap,
        plan_blitz_text="stub",
        **REVIEW_KW,
    )


def _line(script: str, needle: str) -> str:
    return next(ln for ln in script.splitlines() if needle in ln)


def test_size_stage_scout_schema_carries_a_research_property(script):
    scout = _line(script, "const _scouted")
    assert re.search(r"""["']?research["']?\s*:""", scout.split("schema:", 1)[1])


def test_scout_prompt_carries_the_research_classification_instruction(script):
    scout = _line(script, "const _scouted")
    prompt = scout.split("schema:", 1)[0]
    assert "research" in prompt.lower()


def test_scout_assemble_template_carries_research_flags(script):
    scout = _line(script, "const _scouted")
    assert "--research-" in scout.split("schema:", 1)[0]


def test_reword_reassemble_carries_research_flags_only_when_scout_returned_research(script):
    cmd = _line(script, "const _reassembleCmd")
    assert "--research-" in cmd
    assert "_asmFields.research" in cmd


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    Path(record_homes.home_dir(str(tmp_path), "sizings")).mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    return tmp_path


def _assemble(repo: Path, *extra: str, tshirt: str = "XS") -> str:
    path = Path(record_homes.record_path(str(repo), "sizings", _NAME))
    path.write_text(_DRAFT, encoding="utf-8")
    rel = Path(record_homes.record_path("", "sizings", _NAME)).as_posix()
    code = sa.main(
        [
            "--tshirt", tshirt, "--interaction-mode", "pm", "--exit-criterion", "findings written",
            "--premise-provenance", "not-applicable", "--premise-evidence", "fixture", "--write", rel, *extra,
        ]
    )
    assert code == 0
    assert bool(yaml.safe_load(path.read_text(encoding="utf-8")).get("research")) == bool(extra)
    return rel


@pytest.mark.parametrize("tshirt", ["XS", "S"])
def test_research_sizing_gets_a_research_hand_back_with_no_arm_and_no_baton(repo, tshirt):
    rel = _assemble(repo, *_RESEARCH_FLAGS, tshirt=tshirt)
    reply = _handler({"sizing_path": rel, "writes": []}, repo_root=repo / ".git")
    assert reply["arm"] is None and reply.get("baton") is None
    halt = reply["halt"]
    assert halt["kind"] == "research" and halt["route"] == "research"
    assert halt["fire"] == [fire_command(rel)]
    assert "sizing" in halt["reason"] and "/coordinator:research" not in halt["reason"]
    assert "engine-ask" not in halt["reason"]


@pytest.mark.parametrize("tshirt", ["XS", "S"])
def test_non_research_sizing_keeps_its_arm(repo, tshirt):
    rel = _assemble(repo, tshirt=tshirt)
    reply = _handler({"sizing_path": rel, "writes": ["coordinator_core/x.py"]}, repo_root=repo / ".git")
    assert reply["halt"] is None and reply["arm"] is not None


def test_no_plan_or_execute_agent_runs_outside_a_not_halted_guard(script):
    """Regression guard: every halt sets `_halted`, so only guarded phases may spawn plan/stage/execute agents."""
    lines = script.splitlines()
    gate_at = next(i for i, ln in enumerate(lines) if "phase('gate')" in ln)
    for label in ("'plan'", "'plan-review'", "'stage'", "'executor:stage'"):
        idxs = [i for i, ln in enumerate(lines) if f"label: {label}" in ln and i > gate_at]
        for i in idxs:
            guard = [ln for ln in lines[gate_at:i] if "if (" in ln and "!_halted" in ln]
            assert guard, label
