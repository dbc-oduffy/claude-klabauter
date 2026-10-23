"""dispatch.emit over a sizing path sizes a `status: draft` sizing in-run, intake-minted or not.

Run: python -m pytest coordinator_core/ops/dispatch_emit/tests/test_ask_op_draft_sizing_sized_in_run.py -q
"""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops import sizing_intake_contract as contract
from coordinator_core.ops.dispatch_emit import ask_compose, op
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

REL = "state/sizings/2026-10-14-intake.yaml"
_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    return tmp_path


def _put(repo, *, status, intake_mark):
    doc = contract.intake_record(name="intake", turn={"text": "ship it", "turn": 4}, today="2026-10-14")
    doc["status"] = status
    doc["estimate"]["provisional"] = True
    if not intake_mark:
        doc["em_analysis"].pop(contract.INTAKE_MARK_KEY, None)
    (repo / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    return doc


def _emitted_scripts(repo):
    return [p.read_text(encoding="utf-8") for p in sorted((repo / RUN_DIR_ROOT).glob("*.workflow.mjs"))]


def test_op_sizes_a_non_intake_draft_sizing_in_run(repo):
    doc = _put(repo, status="draft", intake_mark=False)
    assert contract.is_intake(doc) is False and contract.needs_size_stage(doc) is True
    out = op._dispatch_emit({"ask": None, "sizing_path": REL}, repo_root=repo)
    assert out["run_id"]
    (script,) = _emitted_scripts(repo)
    assert "phase('size')" in script


def test_op_gates_a_routed_sizing_instead_of_sizing_it_in_run(repo, monkeypatch):
    doc = _put(repo, status="routed", intake_mark=False)
    assert contract.needs_size_stage(doc) is False
    gated = []

    def _gate(*args, **kwargs):
        gated.append(args)
        raise ValueError("gate reached")

    monkeypatch.setattr(op, "_gate_sizing_at_emit", _gate)
    with pytest.raises(ValueError, match="gate reached"):
        op._dispatch_emit({"ask": None, "sizing_path": REL}, repo_root=repo)
    assert len(gated) == 1
    assert _emitted_scripts(repo) == []
