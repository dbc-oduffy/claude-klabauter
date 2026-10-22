"""
coordinator_core/roadmap/tests/test_blitz_land_joins_xs_sizing_to_baton.py

Subject: `blitz_land.land_wave`'s `xsRuns` leg (`join_xs_sizing_to_baton`).

Claim: landing a wave result whose xsRuns entry carries a sizing leaves the baton with
handoff_id, deliverable_id and sizing_object, so `emit-dispatch-workflow --sizing --baton`
has nothing to refuse. Existing values are kept; a differing deliverable_id is reported.

Zero spawns; every case builds its corpus in `tmp_path`.
"""

from __future__ import annotations

import re
from pathlib import Path

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.roadmap import blitz_land as bl

SIZING = "state/sizings/2026-10-21-xs-thing.yaml"
DLV = "dlv-xs-thing-abc123"


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir(exist_ok=True)
    return tmp_path


def _baton(root: Path, stub_id: str, **fields) -> str:
    lines = [
        "kind: roadmap-baton", f"title: {stub_id}", f"stub_id: {stub_id}",
        "status: open", "deployment_state: ready_to_fire", "baton_role: work",
    ]
    lines += [f"{k}: {v}" for k, v in fields.items()]
    p = root / "state" / "handoffs" / f"{stub_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + "\n".join(lines) + "\n---\n\nbody\n", encoding="utf-8")
    return f"state/handoffs/{stub_id}.md"


def _sizing(root: Path, deliverable_id: str = DLV) -> str:
    p = root / SIZING
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"deliverable_id: {deliverable_id}\nschema: sizing-object\nroute: dispatch\n", encoding="utf-8")
    return SIZING


def _fm(root: Path, baton: str, key: str):
    split = split_frontmatter((root / baton).read_text(encoding="utf-8"))
    return read_fm_field_unquoted(split.fm_text, key)


def _land(root: Path, baton: str, stub_id: str):
    return bl.land_wave(
        root,
        {
            "waveIndex": 0,
            "xsRuns": [
                {"batonId": stub_id, "record": baton, "sizingObject": SIZING, "deliverableId": DLV}
            ],
        },
    )


def test_an_xs_run_baton_is_joined_to_its_sizing(tmp_path):
    root = _repo(tmp_path)
    baton = _baton(root, "b-1")
    _sizing(root)

    out = _land(root, baton, "b-1")

    assert not out["refused"]
    assert out["xs_joined"][0]["stamped"] == ["handoff_id", "deliverable_id", "sizing_object"]
    hid = _fm(root, baton, "handoff_id")
    assert re.fullmatch(r"hnd-[a-z0-9-]+-[0-9a-f]{6}", hid)
    assert _fm(root, baton, "deliverable_id") == DLV
    assert _fm(root, baton, "sizing_object") == SIZING


def test_null_fields_are_filled_and_a_reland_is_idempotent(tmp_path):
    root = _repo(tmp_path)
    baton = _baton(root, "b-2", handoff_id="null", deliverable_id="null", sizing_object="null")
    _sizing(root)

    _land(root, baton, "b-2")
    first = (root / baton).read_text(encoding="utf-8")
    assert _fm(root, baton, "deliverable_id") == DLV
    again = _land(root, baton, "b-2")

    assert (root / baton).read_text(encoding="utf-8") == first
    assert again["xs_joined"][0]["stamped"] == []
    assert not again["refused"]


def test_existing_values_are_kept_and_a_deliverable_mismatch_is_reported(tmp_path):
    root = _repo(tmp_path)
    baton = _baton(
        root, "b-3", handoff_id="hnd-mine-aaaaaa", deliverable_id="dlv-other-ffffff",
        sizing_object=SIZING,
    )
    _sizing(root)

    out = _land(root, baton, "b-3")

    row = out["xs_joined"][0]
    assert row["stamped"] == []
    assert any("deliverable_id" in c for c in row["conflicts"])
    assert _fm(root, baton, "handoff_id") == "hnd-mine-aaaaaa"
    assert _fm(root, baton, "deliverable_id") == "dlv-other-ffffff"


def test_a_missing_sizing_is_refused_by_name_and_leaves_the_baton_untouched(tmp_path):
    root = _repo(tmp_path)
    baton = _baton(root, "b-4")
    before = (root / baton).read_text(encoding="utf-8")

    out = _land(root, baton, "b-4")

    assert out["xs_joined"] == []
    assert "sizing does not exist" in out["refused"][0]["reason"]
    assert (root / baton).read_text(encoding="utf-8") == before


def _spec_plan(root: Path, **fields) -> str:
    lines = ["title: the-spec", "status: draft", "capabilities: []"] + [f"{k}: {v}" for k, v in fields.items()]
    p = root / "docs" / "plans" / "the-spec.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + "\n".join(lines) + "\n---\n\nbody\n", encoding="utf-8")
    return "docs/plans/the-spec.md"


def _land_spec(root: Path, stub_id: str, plan: str, **entry):
    return bl.land_wave(
        root,
        {
            "waveIndex": 0,
            "ready": [{"batonId": stub_id, "planPath": plan, "route": "spec-dispatch", **entry}],
        },
    )


def test_a_spec_dispatch_baton_is_joined_from_the_entry_sizing(tmp_path):
    root = _repo(tmp_path)
    baton = _baton(root, "s-1")
    _sizing(root)
    plan = _spec_plan(root)

    out = _land_spec(root, "s-1", plan, sizingObject=SIZING, deliverableId=DLV)

    assert not out["refused"]
    assert out["execution_ready"][0]["join"]["stamped"] == [
        "handoff_id", "deliverable_id", "sizing_object",
    ]
    assert re.fullmatch(r"hnd-[a-z0-9-]+-[0-9a-f]{6}", _fm(root, baton, "handoff_id"))
    assert _fm(root, baton, "deliverable_id") == DLV
    assert _fm(root, baton, "sizing_object") == SIZING
    assert _fm(root, baton, "handoff_phase") == "execution"


def test_a_spec_dispatch_sizing_is_found_on_the_plan_when_the_entry_names_none(tmp_path):
    root = _repo(tmp_path)
    baton = _baton(root, "s-2")
    _sizing(root)
    plan = _spec_plan(root, sizing_object=SIZING)

    out = _land_spec(root, "s-2", plan)

    assert not out["refused"]
    assert _fm(root, baton, "sizing_object") == SIZING
    assert _fm(root, baton, "deliverable_id") == DLV


def test_a_spec_dispatch_keeps_existing_values_and_reports_a_conflict(tmp_path):
    root = _repo(tmp_path)
    baton = _baton(root, "s-3", handoff_id="hnd-mine-aaaaaa", deliverable_id="dlv-other-ffffff")
    _sizing(root)
    plan = _spec_plan(root)

    out = _land_spec(root, "s-3", plan, sizingObject=SIZING)

    join = out["execution_ready"][0]["join"]
    assert any("deliverable_id" in c for c in join["conflicts"])
    assert _fm(root, baton, "handoff_id") == "hnd-mine-aaaaaa"
    assert _fm(root, baton, "deliverable_id") == "dlv-other-ffffff"


def test_a_spec_dispatch_naming_no_sizing_skips_the_join_and_lands_as_before(tmp_path):
    root = _repo(tmp_path)
    baton = _baton(root, "s-4")
    plan = _spec_plan(root)

    out = _land_spec(root, "s-4", plan)

    assert not out["refused"]
    assert "join" not in out["execution_ready"][0]
    assert _fm(root, baton, "sizing_object") in (None, "", "null")


def test_a_spec_dispatch_naming_a_missing_sizing_file_is_refused_and_not_execution_ready(tmp_path):
    root = _repo(tmp_path)
    baton = _baton(root, "s-5")
    before = (root / baton).read_text(encoding="utf-8")
    plan = _spec_plan(root)

    out = _land_spec(root, "s-5", plan, sizingObject="state/sizings/absent.yaml")

    assert out["execution_ready"] == []
    assert "does not exist on disk" in out["refused"][0]["reason"]
    assert (root / baton).read_text(encoding="utf-8") == before


def test_a_spec_dispatch_plan_citing_a_missing_sizing_is_refused(tmp_path):
    root = _repo(tmp_path)
    baton = _baton(root, "s-6")
    before = (root / baton).read_text(encoding="utf-8")
    plan = _spec_plan(root, sizing_object="state/sizings/absent.yaml")

    out = _land_spec(root, "s-6", plan)

    assert out["execution_ready"] == []
    assert "does not exist on disk" in out["refused"][0]["reason"]
    assert (root / baton).read_text(encoding="utf-8") == before


def test_a_sizing_path_escaping_the_worktree_is_refused(tmp_path):
    root = _repo(tmp_path)
    baton = _baton(root, "b-5")

    out = bl.land_wave(
        root,
        {"waveIndex": 0, "xsRuns": [{"batonId": "b-5", "record": baton, "sizingObject": "../x.yaml"}]},
    )

    assert out["xs_joined"] == []
    assert "outside the worktree" in out["refused"][0]["reason"]
