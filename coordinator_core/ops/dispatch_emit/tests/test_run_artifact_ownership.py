"""run_artifact_ownership: own paths per run and what they cover."""

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


def test_run_id_prefix_does_not_cover_longer_sibling_id(tmp_path):
    got = own.own_artifact_paths(tmp_path, "ask-a")
    assert got.covers("state/warp/ask-a/x")
    assert not got.covers("state/warp/ask-ab/x")
    assert not got.covers("docs/delivery/ask-ab.md")


def test_drop_expectations_removes_own_keeps_product_and_keepset(tmp_path):
    _parent(tmp_path, A)
    got = own.own_artifact_paths(tmp_path, A)
    note = f"docs/delivery/{A}.md"
    expected = {SIZ_A: "1", SIZ_B: "2", note: "3", "coordinator_core/x.py": "4"}
    reread = got.drop_expectations(expected, keep={note})
    assert reread == [SIZ_A]
    assert expected == {SIZ_B: "2", note: "3", "coordinator_core/x.py": "4"}
    assert got.drop_expectations(expected) == [note]


def test_legacy_parent_fallback_reads_contract_path(tmp_path):
    record = contract.ParentRecord(
        parent_id=A, runs=(contract.SiblingRun(run_id=A, label="a", after=(), slice_sizing=SIZ_A),)
    )
    path = tmp_path / contract.parent_record_rel(A)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record.to_json()), encoding="utf-8")
    assert own.own_artifact_paths(tmp_path, A).sizings == {SIZ_A}


def test_corrupt_parent_copy_is_distinguishable_from_missing(tmp_path):
    assert own.own_artifact_paths(tmp_path, A).corrupt_parents == ()
    path = tmp_path / contract.run_parent_copy_rel(A)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert own.own_artifact_paths(tmp_path, A).corrupt_parents == (contract.run_parent_copy_rel(A),)


def test_covers_three_classes_and_refuses_product(tmp_path):
    _parent(tmp_path, A)
    got = own.own_artifact_paths(tmp_path, A)
    assert got.covers(SIZ_A)
    assert got.covers(f"docs/delivery/{A}.md")
    assert got.covers(f"state/warp/{A}/manifest.json")
    assert not got.covers(SIZ_B)
    assert not got.covers("coordinator_core/ops/x.py")
    assert not got.covers(f"state/warp/{B}/run.json")


def _sizing_with_plan(root, stem, plan):
    path = root / f"state/sizings/{stem}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'plan: "{plan}"\n', encoding="utf-8")
    (root / f"state/warp/{A}").mkdir(parents=True, exist_ok=True)
    (root / f"state/warp/{A}/run.json").write_text(
        json.dumps({"sizing_rel": f"state/sizings/{stem}.yaml"}), encoding="utf-8"
    )


def test_minted_plan_doc_is_own(tmp_path):
    plan = "docs/plans/2026-11/2026-11-05-18d8d5-1-x.md"
    _sizing_with_plan(tmp_path, "2026-11-05-18d8d5-1-x", plan)
    got = own.own_artifact_paths(tmp_path, A)
    assert got.covers(plan)
    assert not got.covers("docs/plans/2026-11/2026-11-05-18d8d5-2-y.md")


def test_pointed_at_plan_with_other_stem_stays_foreign(tmp_path):
    _sizing_with_plan(tmp_path, "2026-11-05-18d8d5-1-x", "docs/plans/2026-10-01-older.md")
    assert not own.own_artifact_paths(tmp_path, A).covers("docs/plans/2026-10-01-older.md")


def test_delivery_note_follows_flat_or_shard(tmp_path):
    assert own.own_artifact_paths(tmp_path, A).delivery_note == f"docs/delivery/2026-10/{A}.md"
    (tmp_path / "docs/delivery").mkdir(parents=True)
    (tmp_path / f"docs/delivery/{A}.md").write_text("x", encoding="utf-8")
    assert own.own_artifact_paths(tmp_path, A).delivery_note == f"docs/delivery/{A}.md"
