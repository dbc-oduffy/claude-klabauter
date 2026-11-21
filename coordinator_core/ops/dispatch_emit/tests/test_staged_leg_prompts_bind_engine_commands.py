"""Ratchet: no leg prompt the engine writes for a Claude Workflow agent carries a bare `python -m coordinator_core`.

A staged `prompts/<name>.md` is read by an agent whose `python` need not be the engine interpreter and whose
cwd need not import `coordinator_core`; `ask_staged_prompts.stage` is the one choke point that binds them.
"""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_staged_prompts
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.workflow_fire.worker_preamble import _PORTABLE_MODULE

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _staged_legs() -> dict[str, str]:
    sink: dict[str, str] = {}
    compose_ask_script(
        repo_root="REPO",
        prompt="add a thing",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda text: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        staged_out=sink,
        in_run_commit=True,
        script_path="scratch/warp/a.workflow.mjs",
        **REVIEW_KW,
    )
    return sink


def test_no_staged_leg_prompt_carries_a_bare_engine_module_command() -> None:
    sink = _staged_legs()
    assert sink, "compose staged nothing; the ratchet would pass vacuously"
    bare = {name: m[0] for name, text in sink.items() if (m := _PORTABLE_MODULE.search(text))}
    assert not bare, f"staged prompts with a bare engine command: {bare}"


def test_stage_binds_a_portable_command_and_keeps_other_text() -> None:
    sink: dict[str, str] = {}
    ask_staged_prompts.stage(
        "leg", "Run `python -m coordinator_core.ops.test_leg_run --out x` now.", sink, record_dir="/r"
    )
    body = sink["leg"]
    assert not _PORTABLE_MODULE.search(body)
    assert "coordinator_core.ops.test_leg_run --out x` now." in body
