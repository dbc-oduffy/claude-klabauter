"""run_artifact_ownership: own paths per run, slice-sizing owner, foreign-marker detection."""

import json

from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract
from coordinator_core.ops.dispatch_emit import run_artifact_ownership as own

A, B = "ask-20261023T000001-aaaaaa", "ask-20261023T000002-bbbbbb"
SIZ_A = "state/sizings/2026-10-23-p-1-a.yaml"
SIZ_B = "state/sizings/2026-10-23-p-2-b.yaml"


def _parent(root, run_id):
    record = contract.ParentRecord(
        parent_id="ask-parent-1",
        runs=(
            contract.SiblingRun(run_id=A, label="a", after=(), slice_sizing=SIZ_A),
            contract.SiblingRun(run_id=B, label="b", after=(), slice_sizing=SIZ_B),
        ),
    )
    path = root / contract.run_parent_copy_rel(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.to_json()), encoding="utf-8")


def test_each_run_sees_only_its_own_slice(tmp_path):
    _parent(tmp_path, A)
    _parent(tmp_path, B)
    assert own.own_artifact_paths(tmp_path, A).sizings == {SIZ_A}
    assert own.own_artifact_paths(tmp_path, B).sizings == {SIZ_B}


def test_run_json_sizing_joins_slice(tmp_path):
    _parent(tmp_path, A)
    (tmp_path / f"state/warp/{A}/run.json").write_text(
        json.dumps({"sizing_rel": "state/sizings/other.yaml"}), encoding="utf-8"
    )
    assert own.own_artifact_paths(tmp_path, A).sizings == {SIZ_A, "state/sizings/other.yaml"}


def test_unsafe_run_id_is_empty(tmp_path):
    got = own.own_artifact_paths(tmp_path, "../x")
    assert got == own.OwnArtifacts()
    assert not got.covers("state/warp/../x/run.json")


def test_covers_three_classes_and_refuses_product(tmp_path):
    _parent(tmp_path, A)
    got = own.own_artifact_paths(tmp_path, A)
    assert got.covers(SIZ_A)
    assert got.covers(f"docs/delivery/{A}.md")
    assert got.covers(f"state/warp/{A}/manifest.json")
    assert not got.covers(SIZ_B)
    assert not got.covers("coordinator_core/ops/x.py")
    assert not got.covers(f"state/warp/{B}/run.json")


def _sizing(root, rel, marker):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    body = f"em_analysis:\n  ask_parent: {marker}\n" if marker else "em_analysis: {}\n"
    path.write_text(body, encoding="utf-8")


def test_owner_and_foreign(tmp_path):
    _sizing(tmp_path, SIZ_B, f"ask-parent-1 {B}")
    _sizing(tmp_path, SIZ_A, None)
    assert own.sizing_owner(tmp_path, SIZ_B) == B
    assert own.foreign_sizing(tmp_path, A, SIZ_B) == B
    assert own.foreign_sizing(tmp_path, B, SIZ_B) is None
    assert own.sizing_owner(tmp_path, SIZ_A) is None
    assert own.foreign_sizing(tmp_path, A, SIZ_A) is None
    assert own.sizing_owner(tmp_path, "state/sizings/missing.yaml") is None
