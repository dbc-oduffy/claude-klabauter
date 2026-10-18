"""ask_compose: the gate's design brief prefixes the plan-author, spine-review and plan-blitz prompts."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.design_conformance import DESIGN_BRIEF_KEY
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_PREFIX = ask_compose._DESIGN_PREFIX_JS


def _prefix_value(gate: dict) -> str:
    proc = subprocess.run(
        ["node", "-e", f"const _gate = {json.dumps(gate)}; process.stdout.write({_PREFIX});"],
        capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return proc.stdout


@pytest.mark.spawns_process
@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
def test_prefix_carries_the_brief_and_is_empty_without_the_key():
    assert _prefix_value({DESIGN_BRIEF_KEY: "Board: https://x/y"}) == "Board: https://x/y\n\n"
    assert _prefix_value({}) == ""
    assert _prefix_value({DESIGN_BRIEF_KEY: None}) == ""
    assert _prefix_value({DESIGN_BRIEF_KEY: ""}) == ""


def _call_text(script: str, label: str) -> str:
    end = script.index(f"label: '{label}'")
    return script[script.rindex("agent(", 0, end) : end]


def test_plan_author_and_spine_review_prompts_start_with_the_prefix():
    script = _compose()
    for label in ("plan", "plan-review"):
        call = _call_text(script, label)
        assert call.startswith(f"agent(({_PREFIX} + "), label


def test_plan_blitz_agents_are_wrapped_with_the_prefix_ahead_of_the_ledger():
    script = _compose()
    assert f"_outerAgent({_PREFIX} + _LEDGER_PREFIX +" in script


def test_gate_reply_schema_and_prompt_carry_the_key():
    assert DESIGN_BRIEF_KEY in ask_compose._GATE_SCHEMA["properties"]
    assert "design_brief" in _compose()
