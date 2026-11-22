"""Emitted ask_gate / ask_stage legs pin the run's repo, so a gate fired from another cwd sees the run token."""

from __future__ import annotations

import re

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import _repo_flag, compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_roadmap import ROADMAP_STAGE_FN, compose_roadmap_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_RUN_ID = "warp-test-20261027T000000-abc123"
_LEG = re.compile(r"coordinator-invoke\"? dispatch\.ask_(?:gate|stage) \\+'")
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _legs_without_repo(script: str, repo_root: str) -> list[str]:
    """Each ask_gate/ask_stage command in the script, up to its closing backtick, that lacks the pin."""
    flag = _repo_flag(repo_root)
    return [
        script[m.start(): m.start() + 80]
        for m in _LEG.finditer(script)
        if flag not in script[m.start(): script.find("`", m.end())]
    ]


def test_every_composed_ask_leg_carries_repo() -> None:
    script = compose_ask_script(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        run_base_sha="a" * 40, **REVIEW_KW,
    )
    assert len(_LEG.findall(script)) >= 3
    assert _legs_without_repo(script, "REPO") == []


def test_the_gate_relay_declares_its_operation_and_keeps_the_pin_behind_the_host_branch() -> None:
    script = compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel="state/sizings/x.yaml", run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        run_base_sha="a" * 40, **REVIEW_KW,
    )
    gate = next(ln for ln in script.splitlines() if "typeof engineOp === 'function' && _runId ? _hostGate(" in ln)
    assert re.search(r"""op_id: (["'])dispatch\.ask_gate\1""", gate)
    assert _legs_without_repo(script, "REPO") == []


def test_roadmap_gate_leg_carries_repo() -> None:
    script = compose_roadmap_script(
        repo_root="/repo", sizing_rel="state/sizings/x.yaml", run_id="run-1", interaction_mode=None,
        roadmap_text="stub", plan_blitz_fn="  async function planBlitz() { return {}; }",
        plan_blitz_phases=[], plan_blitz_args=None,
        wrap_roadmap=lambda _t: (f"  async function {ROADMAP_STAGE_FN}() {{ return {{}}; }}", []),
    )
    assert _LEG.search(script)
    assert _legs_without_repo(script, "/repo") == []


def test_repo_flag_quotes_a_path_with_spaces() -> None:
    assert _repo_flag("my repo") == " --repo 'my repo'"
