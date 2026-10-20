"""A raw ask's script stages, refires and names next_action against the sizing path the gate bound."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_size_then_plan_e2e import (
    PLAN_PATH,
    _run_script,
    _scout,
    _wrap_stage,
)

RUN_ID = "ask-20261020T170155-faf09b"
SUFFIX = "faf09b"
BOUND = "state/sizings/2026-10-20-delegated-atlassian-read-access.yaml"


def _compose(**over):
    kw = dict(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id=RUN_ID, session_id=None,
        wrap_stage=_wrap_stage, plan_blitz_text="stub", **REVIEW_KW,
    )
    kw.update(over)
    return compose_ask_script(**kw)


def test_scout_write_gate_and_stage_paths_all_carry_the_run_suffix():
    script = _compose()
    suffixed = f"-<name>-{SUFFIX}.yaml"
    assert "-<name>.yaml" not in script
    assert f"--write \\'REPO/state/sizings/" in script
    assert f'\\\'{{"sizing_path":"state/sizings/' in script
    assert f'sizing_path "state/sizings/' in script
    assert script.count(suffixed) >= 4
    assert f"_slugOf(r) + '-{SUFFIX}.yaml';" in script


def test_scout_is_told_to_return_sizing_path_and_the_schema_admits_it():
    from coordinator_core.ops.dispatch_emit.ask_contract import ASK_SCOUT_SCHEMA

    assert "arm, halt, baton, sizing_path" in _compose()
    assert ASK_SCOUT_SCHEMA["properties"]["sizing_path"] == {"type": ["string", "null"]}


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_stage_refire_and_next_action_name_the_gate_bound_path_not_the_computed_one():
    scout = {
        **_scout("M", []),
        "exit_code": 0, "stdout": json.dumps({"route": "plan"}), "stderr": "",
        "arm": "S", "halt": None, "baton": None, "sizing_path": BOUND, "tshirt": "M", "route": "plan",
    }
    out = _run_script(_compose(), {"size": scout, "blitz": {"plan_rel": PLAN_PATH}})
    result = out["result"]
    text = json.dumps(result)
    assert BOUND in text
    assert f"-{SUFFIX}.yaml" not in text, text
    assert result["sizing"] == BOUND
