"""A plan-less XS sizing composed into an ask stages through dispatch.ask_stage from the composed stage params alone."""

from __future__ import annotations

import json
import re

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_compose, ask_stage, reply_sidecar
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

SIZING_REL = "state/sizings/2026-10-22-xs-plan-less.yaml"
WRITES = ["src/x.py"]
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_STAGE_MARK = "dispatch.ask_stage \\'<params>\\'"

_RUNTIME_VALUES = {
    "_runId": "xs-plan-less-1",
    "_writes": WRITES,
    "_crossRepoApproved": False,
    "_SESSION_ID": "",
    "_sizingRel": SIZING_REL,
    "_gated": [],
}


@pytest.fixture
def repo(tmp_path):
    for d in (".git", "state/sizings", "src"):
        (tmp_path / d).mkdir(parents=True)
    doc = {
        "intent": "Fix x",
        "exit_criterion": {"statement": "x works"},
        "deliverable_id": "dlv-xs-plan-less-abc123",
    }
    (tmp_path / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    return tmp_path


def _object_literal(text: str, start: int) -> str:
    """The balanced `{...}` literal beginning at the first `{` at or after `start`."""
    begin = text.index("{", start)
    depth = 0
    for i in range(begin, len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[begin : i + 1]
    raise AssertionError("unbalanced object literal in the stage act")


def _composed_stage_params(monkeypatch) -> dict:
    """The params object the gate agent is told to run for ask_stage, resolved against fixed runtime values.

    Only keys written inside the composed JSON.stringify literal count: a key the prompt asks
    the agent to add in prose is the agent's choice, not a composed argument.
    """
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "xs")
    script = compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel=SIZING_REL,
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda text: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        writes=WRITES,
        **REVIEW_KW,
    )
    act = script[script.index(_STAGE_MARK) + len(_STAGE_MARK) :]
    literal = _object_literal(act, act.index("JSON.stringify("))
    params: dict = {}
    for key, value in re.findall(r"(?<![\w.])(\w+)\s*:\s*(_\w+|\"[^\"]*\")", literal):
        params[key] = _RUNTIME_VALUES[value] if value.startswith("_") else json.loads(value)
    return params


def test_plan_less_xs_stage_params_reach_a_manifest_handle(repo, monkeypatch):
    params = _composed_stage_params(monkeypatch)
    assert params.get("sizing_path") == SIZING_REL
    assert "plan_path" not in params
    reply = ask_stage._handler(params, repo_root=repo)
    assert "error" not in reply, reply
    handle = reply["manifest_handle"]
    manifest = reply_sidecar.read_reply(repo, handle)
    (row,) = manifest["rows"]
    assert row["id"] == "X1"
    assert set(WRITES) <= set(row["writes"])


def test_stage_without_sizing_path_still_refuses(repo):
    reply = ask_stage._handler({"run_id": "xs-plan-less-2", "writes": WRITES}, repo_root=repo)
    assert reply == {"error": "name exactly one of plan_rel and sizing_rel"}
