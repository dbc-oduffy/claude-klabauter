"""
coordinator_core.roadmap.tests.test_number_stubs_sprint_axis — C5a coverage:
topo_number's sprint-axis caller work in run_default_mode (AC21) plus the
sprint-descriptor/stub_id disjointness invariant primitive (AC7).

Spec backlink: docs/plans/2026-08-21-engine-half-of-the-roadmap-sprint-spine-split.md
§ C5a.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.roadmap.number_stubs import (
    SprintDescriptorStubCollisionError,
    assert_sprint_descriptor_stub_disjoint,
    main,
    parse_edges_file,
)

# ---------------------------------------------------------------------------
# parse_edges_file — @N sprint tag / fromSprint,toSprint extraction (AC21)
# ---------------------------------------------------------------------------


def test_parse_edges_file_line_form_sprint_tag_on_edge_sides():
    content = "WIDGET@2 <- BASE@1\nGADGET@2 <- WIDGET@2\n"
    parsed = parse_edges_file(content)
    assert parsed["edges"] == [
        {"from": "WIDGET", "to": "BASE"},
        {"from": "GADGET", "to": "WIDGET"},
    ]
    assert parsed["sprints"] == {"WIDGET": 2, "BASE": 1, "GADGET": 2}


def test_parse_edges_file_line_form_sprint_tag_on_isolated_node():
    content = "LONER@3\n"
    parsed = parse_edges_file(content)
    assert parsed["isolatedNodes"] == ["LONER"]
    assert parsed["sprints"] == {"LONER": 3}


def test_parse_edges_file_line_form_no_tags_has_empty_sprints():
    content = "WIDGET <- BASE\n"
    parsed = parse_edges_file(content)
    assert parsed["sprints"] == {}


def test_parse_edges_file_json_form_from_to_sprint_fields():
    content = json.dumps(
        [{"from": "BETA", "to": "ALPHA", "fromSprint": 2, "toSprint": 1}]
    )
    parsed = parse_edges_file(content)
    assert parsed["edges"] == [{"from": "BETA", "to": "ALPHA"}]
    assert parsed["sprints"] == {"BETA": 2, "ALPHA": 1}


def test_parse_edges_file_json_form_no_sprint_fields_has_empty_sprints():
    content = json.dumps([{"from": "BETA", "to": "ALPHA"}])
    parsed = parse_edges_file(content)
    assert parsed["sprints"] == {}


# ---------------------------------------------------------------------------
# run_default_mode (via main) — author-assigned sprints stamped onto
# sprintWave, byte-parity-preserving default, and fail-loud on a
# non-dependency-monotone author assignment (AC21).
# ---------------------------------------------------------------------------


def _rows(out: str):
    rows = {}
    for ln in out.splitlines():
        if not ln or ln.startswith("#"):
            continue
        parts = ln.split()
        rows[parts[0]] = {"num": int(parts[1]), "sprint": int(parts[2]), "wave": int(parts[3])}
    return rows


def test_default_mode_unannotated_edges_file_defaults_every_label_to_sprint_1(
    tmp_path, capsys
):
    edges_file = tmp_path / "edges.txt"
    edges_file.write_text("WIDGET <- BASE\nGADGET <- WIDGET\n", encoding="utf-8")

    rc = main([str(edges_file)])
    out = capsys.readouterr().out
    assert rc == 0
    rows = _rows(out)
    assert all(r["sprint"] == 1 for r in rows.values())
    assert rows["BASE"]["wave"] < rows["WIDGET"]["wave"] < rows["GADGET"]["wave"]


def test_default_mode_stamps_author_assigned_sprint_values(tmp_path, capsys):
    edges_file = tmp_path / "edges.txt"
    edges_file.write_text("WIDGET@2 <- BASE@1\nGADGET@2 <- WIDGET@2\n", encoding="utf-8")

    rc = main([str(edges_file)])
    out = capsys.readouterr().out
    assert rc == 0
    rows = _rows(out)
    assert rows["BASE"]["sprint"] == 1
    assert rows["WIDGET"]["sprint"] == 2
    assert rows["GADGET"]["sprint"] == 2
    # Within sprint 2, wave still increases dependency-before-dependent.
    assert rows["WIDGET"]["wave"] < rows["GADGET"]["wave"]


def test_default_mode_sprint_axis_order_is_dependency_monotone_across_sprints(
    tmp_path, capsys
):
    edges_file = tmp_path / "edges.txt"
    # BASE ships in sprint 1; WIDGET (which depends on BASE) in sprint 2.
    edges_file.write_text("WIDGET@2 <- BASE@1\n", encoding="utf-8")

    rc = main([str(edges_file)])
    out = capsys.readouterr().out
    assert rc == 0
    rows = _rows(out)
    base_slot = (rows["BASE"]["sprint"], rows["BASE"]["wave"])
    widget_slot = (rows["WIDGET"]["sprint"], rows["WIDGET"]["wave"])
    assert base_slot < widget_slot


def test_default_mode_dependency_assigned_later_sprint_than_dependent_fails_loud(
    tmp_path, capsys
):
    edges_file = tmp_path / "edges.txt"
    # BASE (the dependency, must ship first) tagged sprint 2, but its
    # dependent WIDGET tagged sprint 1 -- inverted, not dependency-monotone.
    edges_file.write_text("WIDGET@1 <- BASE@2\n", encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        main([str(edges_file)])
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "not dependency-monotone" in err
    assert "same-or-inverted-slot" in err


def test_default_mode_json_form_stamps_from_to_sprint_fields(tmp_path, capsys):
    edges_file = tmp_path / "edges.json"
    edges_file.write_text(
        json.dumps([{"from": "BETA", "to": "ALPHA", "fromSprint": 2, "toSprint": 1}]),
        encoding="utf-8",
    )

    rc = main([str(edges_file)])
    out = capsys.readouterr().out
    assert rc == 0
    rows = _rows(out)
    assert rows["ALPHA"]["sprint"] == 1
    assert rows["BETA"]["sprint"] == 2


# ---------------------------------------------------------------------------
# assert_sprint_descriptor_stub_disjoint — AC7 fail-loud collision invariant
# ---------------------------------------------------------------------------


def test_disjoint_ids_pass_silently():
    assert_sprint_descriptor_stub_disjoint(
        sprint_descriptor_ids=["sprint-1", "sprint-2"],
        stub_ids=["stub-a-1", "stub-b-2"],
    )


def test_colliding_id_raises_fail_loud():
    with pytest.raises(SprintDescriptorStubCollisionError) as excinfo:
        assert_sprint_descriptor_stub_disjoint(
            sprint_descriptor_ids=["sprint-1", "collide-id"],
            stub_ids=["stub-a-1", "collide-id"],
            roadmap_id="rm-c5a",
        )
    assert excinfo.value.colliding_ids == ["collide-id"]
    assert excinfo.value.roadmap_id == "rm-c5a"
    assert "collide-id" in str(excinfo.value)
    assert "rm-c5a" in str(excinfo.value)


def test_multiple_colliding_ids_all_reported():
    with pytest.raises(SprintDescriptorStubCollisionError) as excinfo:
        assert_sprint_descriptor_stub_disjoint(
            sprint_descriptor_ids=["dup-1", "dup-2", "sprint-only"],
            stub_ids=["dup-1", "dup-2", "stub-only"],
        )
    assert excinfo.value.colliding_ids == ["dup-1", "dup-2"]


def test_empty_id_sets_pass_silently():
    assert_sprint_descriptor_stub_disjoint(sprint_descriptor_ids=[], stub_ids=[])
