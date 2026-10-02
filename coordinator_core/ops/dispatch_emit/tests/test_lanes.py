"""Tests for the mise lane partitioner (``dispatch_emit.lanes``)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from coordinator_core.ops.dispatch_emit import lanes
from coordinator_core.ops.dispatch_emit.inventory_mint import parse_chunk_table
from coordinator_core.ops.dispatch_emit.lanes import (
    LaneMapStaleError,
    LaneParams,
    PartNotReadyError,
    RowOverBudgetError,
    partition,
    ready_parts,
    render_part_inventory,
)
from coordinator_core.ops.dispatch_emit.wave_map import WaveCycleError, _paths_overlap

_SCHEMA = json.loads(
    (Path(lanes.__file__).resolve().parents[2] / "contract" / "mise-lane-map.schema.json").read_text()
)
_HEADER = "| id | spec path | summary | footprint | deps | verification | complexity | disposition |"


def _row(rid, writes, deps="-", plan="docs/plans/p.md", disp="pending"):
    fp = ", ".join(f"`{w}`" for w in writes)
    return {
        "id": rid,
        "spec path": plan,
        "summary": f"summary of {rid}",
        "footprint": fp,
        "deps": deps,
        "verification": "t.py",
        "complexity": "S",
        "disposition": disp,
    }


def _master(rows):
    body = [_HEADER, "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        body.append("| " + " | ".join(r[k] for k in r) + " |")
    return "---\nrun_id: m\n---\n# Mise inventory\n## Chunk table\n\n" + "\n".join(body) + "\n"


def _part(rows, **kw):
    args = dict(
        params=LaneParams(lanes=3, hot_files=0),
        row_bytes={r["id"]: 100 for r in rows},
        fixed_bytes=0,
        bound_plans=frozenset(),
        run_id="m",
        source_inventory="state/mise-inventory/m.md",
        start_sha="abc",
    )
    args.update(kw)
    return partition(rows, **args)


def _lane_of(lane_map):
    return {r: lane["id"] for lane in lane_map["lanes"] for p in lane["parts"] for r in p["rows"]}


def _four_independent():
    return [_row(f"R{i}", [f"src/mod{i}/f.py"], plan=f"docs/plans/p{i}.md") for i in range(4)]


def test_hot_file_pulls_rows_and_dependency_neighbours_into_hub():
    rows = [
        _row("A", ["hot.py", "a.py"]),
        _row("B", ["hot.py", "b.py"], plan="docs/plans/b.md"),
        _row("C", ["c.py"], deps="A", plan="docs/plans/c.md"),
        _row("D", ["d.py"], plan="docs/plans/d.md"),
        _row("E", ["e.py"], deps="D", plan="docs/plans/e.md"),
    ]
    lm = _part(rows, params=LaneParams(lanes=3, hot_files=1))
    of = _lane_of(lm)
    assert lm["hot_files"] == ["hot.py"]
    assert of["A"] == of["B"] == of["C"] == "hub"
    assert of["D"] == of["E"] != "hub"


def test_bound_plan_split_by_overlap_stays_in_one_lane():
    rows = [
        _row("A", ["a.py"], plan="docs/plans/x.md"),
        _row("B", ["b.py"], plan="docs/plans/x.md"),
        _row("C", ["c.py"], plan="docs/plans/y.md"),
    ]
    lm = _part(rows, bound_plans=frozenset({"docs/plans/x.md"}))
    of = _lane_of(lm)
    assert of["A"] == of["B"] != of["C"]
    assert lm["falsifier_review_part"] == {"docs/plans/x.md": of["A"]}


def test_unbound_plan_rows_may_spread_across_lanes():
    rows = [_row("A", ["a.py"]), _row("B", ["b.py"])]
    of = _lane_of(_part(rows))
    assert of["A"] != of["B"]


def test_no_file_spans_two_lanes_including_directory_overlap():
    rows = [
        _row("A", ["pkg/a.py"], plan="docs/plans/a.md"),
        _row("B", ["pkg/b.py"], plan="docs/plans/b.md"),
        _row("C", ["x/y.py"], plan="docs/plans/c.md"),
        _row("D", ["pkg/a.py"], plan="docs/plans/d.md"),
        _row("E", ["z.py"], plan="docs/plans/e.md"),
    ]
    rows[1]["footprint"] += " writes_under: `pkg/`"
    lm = _part(rows)
    of = _lane_of(lm)
    assert of["A"] == of["B"] == of["D"]
    by_lane = {}
    for r in rows:
        w = [p.strip("` ") for p in r["footprint"].replace("writes_under:", "").split(",")]
        by_lane.setdefault(of[r["id"]], set()).update(w)
    lanes_ = sorted(by_lane)
    for i, la in enumerate(lanes_):
        for lb in lanes_[i + 1:]:
            assert not any(_paths_overlap(p, q) for p in by_lane[la] for q in by_lane[lb])
    assert sorted(of) == sorted(r["id"] for r in rows)


def test_bytes_over_budget_split_into_sequential_parts_with_after_edges():
    rows = [_row(f"R{i}", ["shared.py"], deps="-" if i == 0 else f"R{i - 1}") for i in range(5)]
    lm = _part(
        rows,
        params=LaneParams(lanes=1, hot_files=0, byte_cap=1000, headroom=1.0),
        row_bytes={r["id"]: 300 for r in rows},
        fixed_bytes=50,
    )
    (lane,) = lm["lanes"]
    ids = [p["id"] for p in lane["parts"]]
    assert ids == [f"{lane['id']}-p{n}" for n in (1, 2)]
    assert [p["rows"] for p in lane["parts"]] == [["R0", "R1", "R2"], ["R3", "R4"]]
    assert lane["parts"][0]["after"] == [] and lane["parts"][1]["after"] == [ids[0]]


def test_lane_that_fits_is_one_part_named_by_lane():
    lm = _part(_four_independent())
    for lane in lm["lanes"]:
        assert [p["id"] for p in lane["parts"]] == [lane["id"]]


def test_over_budget_single_row_raises():
    rows = [_row("A", ["a.py"])]
    with pytest.raises(RowOverBudgetError, match="'A'"):
        _part(rows, row_bytes={"A": 2000}, params=LaneParams(byte_cap=1000))


def test_topological_order_follows_depends_on_then_id():
    rows = [
        _row("Z", ["s.py"]),
        _row("M", ["s.py"], deps="Z"),
        _row("A", ["s.py"], deps="M"),
        _row("B", ["s.py"]),
    ]
    (lane,) = _part(rows, params=LaneParams(lanes=1, hot_files=0))["lanes"]
    assert lane["parts"][0]["rows"] == ["B", "Z", "M", "A"]


def test_dependency_cycle_raises_wave_cycle_error():
    rows = [_row("A", ["a.py"], deps="B"), _row("B", ["b.py"], deps="A")]
    with pytest.raises(WaveCycleError):
        _part(rows)


def test_identical_input_gives_identical_sorted_json():
    rows = _four_independent() + [_row("H", ["hot.py"]), _row("I", ["hot.py"])]
    kw = dict(params=LaneParams(lanes=3, hot_files=1))
    a = json.dumps(_part(rows, **kw), sort_keys=True)
    b = json.dumps(_part(list(reversed(rows)), **kw), sort_keys=True)
    assert a == json.dumps(_part(rows, **kw), sort_keys=True)
    assert json.loads(a)["lanes"] and json.loads(b)["hot_files"] == ["hot.py"]


def test_output_validates_against_lane_map_schema():
    rows = _four_independent() + [_row("H", ["hot.py"]), _row("I", ["hot.py"], deps="H")]
    lm = _part(rows, params=LaneParams(lanes=3, hot_files=1), start_sha=None)
    jsonschema.validate(lm, _SCHEMA)
    assert [lane["id"] for lane in lm["lanes"]][-1] == "hub"


def test_ready_parts_and_staleness():
    rows = [_row(f"R{i}", ["s.py"], deps="-" if i == 0 else f"R{i - 1}") for i in range(4)]
    lm = _part(
        rows,
        params=LaneParams(lanes=1, hot_files=0, byte_cap=250, headroom=1.0),
        row_bytes={r["id"]: 100 for r in rows},
    )
    disp = {r["id"]: "pending" for r in rows}
    first, second = [p["id"] for p in lm["lanes"][0]["parts"]]
    assert ready_parts(lm, disp) == [first]
    disp.update({"R0": "landed", "R1": "already-fixed"})
    assert ready_parts(lm, disp) == [first, second]
    with pytest.raises(LaneMapStaleError, match="m.lanes.json") as exc:
        ready_parts(lm, {**disp, "NEW": "pending"})
    assert "NEW" in str(exc.value)
    with pytest.raises(LaneMapStaleError):
        ready_parts(lm, {k: v for k, v in disp.items() if k != "R3"})


def _two_part_master(dispositions):
    rows = [
        _row("R0", ["s.py"], disp=dispositions[0]),
        _row("R1", ["s.py"], deps="R0", disp=dispositions[1]),
        _row("R2", ["s.py"], deps="R0, R1", disp=dispositions[2]),
    ]
    lm = _part(rows, params=LaneParams(lanes=1, hot_files=0, byte_cap=200, headroom=1.0))
    return rows, lm


def test_render_part_inventory_refuses_live_cross_part_dependency():
    rows, lm = _two_part_master(["pending", "pending", "pending"])
    p2 = lm["lanes"][0]["parts"][1]["id"]
    with pytest.raises(PartNotReadyError, match="R0|R1"):
        render_part_inventory(_master(rows), lm, p2)


def test_render_part_inventory_drops_closed_satisfied_dependency():
    rows, lm = _two_part_master(["landed", "already-fixed", "pending"])
    p2 = lm["lanes"][0]["parts"][1]["id"]
    text = render_part_inventory(_master(rows), lm, p2)
    (row,) = parse_chunk_table(text)
    assert row["id"] == "R2" and row["deps"] == "-" and row["disposition"] == "pending"
    assert text.startswith(f"---\nrun_id: m-{p2}\nstart_sha: abc\n---")
    assert "do not hand-edit" in text


def test_render_part_inventory_routes_out_row_whose_dependency_is_routed_out():
    rows, lm = _two_part_master(["landed", "routed-out (premise gone)", "pending"])
    p2 = lm["lanes"][0]["parts"][1]["id"]
    (row,) = parse_chunk_table(render_part_inventory(_master(rows), lm, p2))
    p1 = lm["lanes"][0]["parts"][0]["id"]
    assert row["disposition"] == f"routed-out (dep R1 routed out in part {p1})"


def test_render_part_inventory_keeps_in_part_dependencies_and_stale_check():
    rows, lm = _two_part_master(["pending", "pending", "pending"])
    p1 = lm["lanes"][0]["parts"][0]["id"]
    parsed = parse_chunk_table(render_part_inventory(_master(rows), lm, p1))
    assert [r["id"] for r in parsed] == ["R0", "R1"] and parsed[1]["deps"] == "R0"
    with pytest.raises(LaneMapStaleError):
        render_part_inventory(_master(rows[:2]), lm, p1)
