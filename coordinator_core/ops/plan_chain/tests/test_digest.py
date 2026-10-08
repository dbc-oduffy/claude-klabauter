"""Final-digest assembly: every halt id plus the completed chain validates and carries the right shape."""
import json

import pytest

from coordinator_core.ops.plan_chain import digest as D
from coordinator_core.ops.plan_chain.contract import (
    HALTS, STAGE_RELATIVE, STAGES, ChainManifest, ChainState, halt,
)


def _manifest():
    return ChainManifest("s.yaml", "b.md", "D1", "pm", ".", "trail", {}, "x")


@pytest.mark.parametrize("halt_id", sorted(HALTS))
def test_halt_digest_validates(halt_id):
    h = halt(halt_id, "why " * 200, running_stage="execute")
    state = ChainState(stages_run=list(STAGES[: STAGES.index(h.halted_at)]), halt=h, plan_path="p.md")
    d = D.assemble_final_digest(state, _manifest(), plan_digest=None, execute_digest=None)
    D.validate_final_digest(d)
    assert d["chain"]["halted_at"] == h.halted_at
    assert d["chain"]["commit"] is None
    early = h.halted_at in ("plan", "ready-gate")
    assert d["kind"] == ("plan" if early else "close")
    assert d["outcome"] == ("surfaced" if early else "indeterminate")
    assert len(d["chain"]["halt_reason"]) <= 300


def test_plan_halt_keeps_plan_outcome():
    state = ChainState(halt=halt("ready-gate-not-ready", "r"))
    d = D.assemble_final_digest(state, _manifest(), plan_digest={"outcome": "replan"}, execute_digest=None)
    D.validate_final_digest(d)
    assert d["outcome"] == "replan"


def test_completed_chain():
    state = ChainState(stages_run=list(STAGES), commit={"sha": "abc", "receipt_path": "r.json"}, plan_path="p.md")
    ex = {
        "criterion": {"status": "met", "observation": "ok", "sidecar": None},
        "deviations": [{"chunk": "C1", "kind": "partial", "anchor": "a"}],
        "review": {"slices": 2, "fixes_applied": 3},
    }
    d = D.assemble_final_digest(state, _manifest(), plan_digest=None, execute_digest=ex)
    D.validate_final_digest(d)
    assert (d["kind"], d["outcome"]) == ("close", "complete")
    assert d["chain"]["halted_at"] is None
    assert d["chain"]["stages_run"] == list(STAGES)
    assert d["chain"]["commit"] == {"sha": "abc", "receipt_path": "r.json"}
    assert d["criterion"]["status"] == "met"
    assert d["counts"]["reviewers"] == 2 and d["counts"]["findings_applied"] == 3


def test_invalid_digest_raises_and_is_not_written(tmp_path):
    state = ChainState(stages_run=list(STAGES), commit={"sha": "a", "receipt_path": "r"})
    d = D.assemble_final_digest(state, _manifest(), plan_digest=None, execute_digest=None)
    d["kind"] = "bogus"
    with pytest.raises(ValueError):
        D.write_final_digest(d, tmp_path, "c1")
    assert not list(tmp_path.iterdir())


def test_write_final_digest(tmp_path):
    state = ChainState(stages_run=list(STAGES), commit={"sha": "a", "receipt_path": "r"})
    d = D.assemble_final_digest(state, _manifest(), plan_digest=None, execute_digest=None)
    p = D.write_final_digest(d, tmp_path / "trail", "c1")
    assert p.name == "chain-c1.final-digest.json"
    assert json.loads(p.read_text(encoding="utf-8")) == d
