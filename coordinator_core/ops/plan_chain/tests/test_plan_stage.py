"""plan_stage reads plan digests by field and binds the plan script with a session-naming receipt."""
from __future__ import annotations

import json

import pytest

from coordinator_core.ops.plan_chain import plan_stage
from coordinator_core.ops.plan_chain.contract import ChainManifest, Halt, WorkflowResult


def _wr(digest):
    return WorkflowResult(digest=digest, raw_result="", child_session_id="sid")


def _ready(plan_path):
    return {"kind": "plan", "outcome": "ready", "next_action": {"params": {"plan_path": plan_path}}}


def test_ready_returns_existing_plan_path(tmp_path):
    (tmp_path / "p.md").write_text("x", encoding="utf-8")
    assert plan_stage.read_plan_result(_wr(_ready("p.md")), repo_root=tmp_path) == "p.md"


@pytest.mark.parametrize(
    "digest, halt_at",
    [
        (None, "plan"),
        ({"kind": "close", "outcome": "ready"}, "plan"),
        ({"kind": "plan", "outcome": "pulled"}, "ready-gate"),
        ({"kind": "plan", "outcome": "surfaced"}, "ready-gate"),
        ({"kind": "plan", "outcome": "ready", "next_action": {"params": None}}, "plan"),
        (_ready("missing.md"), "plan"),
    ],
)
def test_halts(tmp_path, digest, halt_at):
    out = plan_stage.read_plan_result(_wr(digest), repo_root=tmp_path)
    assert isinstance(out, Halt) and out.halted_at == halt_at


def test_not_ready_reason_names_outcome(tmp_path):
    out = plan_stage.read_plan_result(_wr({"kind": "plan", "outcome": "replan"}), repo_root=tmp_path)
    assert "replan" in out.reason


def _manifest(tmp_path, source):
    return ChainManifest(
        sizing_object="s.yaml", baton="b", deliverable_id=None, interaction_mode="pm",
        repo_root=str(tmp_path), trail_dir="trail", wave_args={"k": 1}, script_source=str(source),
    )


def test_bind_plan_script_writes_script_and_receipt(tmp_path):
    src = tmp_path / "wf.mjs"
    src.write_text(
        "export const meta = {\n  name: 'x',\n}\nreturn args\n", encoding="utf-8"
    )
    out = plan_stage.bind_plan_script(_manifest(tmp_path, src), child_session_id="sess-1", chain_id="c1")
    assert out == tmp_path / "trail" / "chain-c1.plan.mjs"
    assert "const args = " in out.read_text(encoding="utf-8")
    receipt = json.loads((out.parent / (out.name + ".emitted.json")).read_text(encoding="utf-8"))
    assert receipt["session_id"] == "sess-1"


def test_bind_plan_script_refuses_empty_session_and_missing_source(tmp_path):
    with pytest.raises(ValueError):
        plan_stage.bind_plan_script(_manifest(tmp_path, tmp_path / "x"), child_session_id="")
    with pytest.raises(ValueError):
        plan_stage.bind_plan_script(_manifest(tmp_path, tmp_path / "nope.mjs"), child_session_id="s")
    assert not list((tmp_path / "trail").glob("*")) if (tmp_path / "trail").exists() else True
