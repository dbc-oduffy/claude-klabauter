"""``dispatch.emit --inventory --lanes``: the lane map, sub-inventories, scripts and readiness.

Pins: a 3-lane inventory writes the pinned lane map, three sub-inventories and three
scripts; a hub needing two parts emits p1 and reports p2 waiting; closing p1 in the
master releases p2 on the next ``--lanes``; a falsifier plan whose ``how`` names a .py
instrument gets its can-report-red JSON written beside the record.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import jsonschema
import pytest

from coordinator_core.ops.dispatch_emit import lanes, op
from coordinator_core.ops.dispatch_emit.lanes import PartNotReadyError
from coordinator_core.session.record_homes import home_dir, record_path

from .conftest import REVIEW_KW

pytestmark = pytest.mark.complete

RUN = "20260930T000000-lanes"
_HEADER = (
    "| id | spec path | summary | footprint | deps | verification | complexity | disposition |\n"
    "|---|---|---|---|---|---|---|---|\n"
)


def _inventory(rows: list[tuple[str, str, str, str]]) -> str:
    """rows: (id, footprint, deps, disposition)."""
    body = "".join(
        f"| {rid} | `docs/plans/fixture.md` | does {rid} | `{fp}` | {deps} | scoped pytest | S | {disp} |\n"
        for rid, fp, deps, disp in rows
    )
    return (
        f"---\nrun_id: {RUN}\nstart_sha: {'a' * 40}\n---\n\n# Mise inventory\n\n"
        f"## Chunk table\n\n{_HEADER}{body}"
    )


@pytest.fixture(autouse=True)
def _review_inputs(monkeypatch):
    monkeypatch.setattr(op, "_load_review_inputs", lambda route: tuple(REVIEW_KW.values()))


def _write_master(root: Path, text: str) -> Path:
    inv = Path(record_path(str(root), "mise-inventory", f"{RUN}.md"))
    inv.parent.mkdir(parents=True, exist_ok=True)
    inv.write_text(text, encoding="utf-8", newline="\n")
    return inv


def _lanes(inv: Path, root: Path, **extra) -> dict:
    return op._dispatch_emit({"inventory_path": str(inv), "lanes": True, **extra}, root)


def test_three_lane_inventory_writes_map_sub_inventories_and_scripts(tmp_path):
    inv = _write_master(
        tmp_path,
        _inventory(
            [
                ("R1", "pkg/one.py", "-", "pending"),
                ("R2", "pkg/two.py", "-", "pending"),
                ("R3", "pkg/three.py", "-", "pending"),
            ]
        ),
    )
    reply = _lanes(inv, tmp_path, hot_files=0)

    assert reply["ok"] is True
    assert reply["not_ready"] == []
    lane_map_file = tmp_path / reply["lane_map"]
    lane_map = json.loads(lane_map_file.read_text(encoding="utf-8"))
    schema = json.loads(
        (Path(lanes.__file__).parents[2] / "contract" / "mise-lane-map.schema.json").read_text(encoding="utf-8")
    )
    jsonschema.validate(lane_map, schema)
    assert [lane["id"] for lane in lane_map["lanes"]] == ["a", "b", "c"]
    for lane in lane_map["lanes"]:
        (part,) = lane["parts"]
        assert (tmp_path / part["inventory"]).is_file()
        script = tmp_path / part["script"]
        assert script.is_file() and part["bytes"] == script.stat().st_size
        text = script.read_text(encoding="utf-8")
        assert "label: 'check:" not in text and "] = _runRow(" in text
    assert len(reply["parts"]) == 3
    assert all(Path(p["path"]).name.endswith(".workflow.mjs") for p in reply["parts"])
    assert all(p["receipt"] for p in reply["parts"])


def _pin_two_part_hub(root: Path, master: str) -> None:
    from coordinator_core.ops.dispatch_emit.inventory_mint import parse_chunk_table

    rows = parse_chunk_table(master)
    ids = [r["id"].strip("`") for r in rows]
    lane_map = lanes.partition(
        rows,
        params=lanes.LaneParams(lanes=1, hot_files=1, byte_cap=2000, headroom=1.0),
        row_bytes={i: 1500 for i in ids},
        fixed_bytes=0,
        bound_plans=frozenset(),
        run_id=RUN,
        source_inventory=Path(record_path(".", "mise-inventory", f"{RUN}.md")).as_posix(),
        start_sha="a" * 40,
    )
    pins = root / lanes.lane_map_path(RUN)
    pins.write_text(json.dumps(lane_map, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def test_hub_needing_two_parts_emits_p1_and_reports_p2_waiting_then_releases_it(tmp_path):
    rows = [
        ("H1", "shared/hot.py", "-", "pending"),
        ("H2", "shared/hot.py", "H1", "pending"),
    ]
    master = _inventory(rows)
    inv = _write_master(tmp_path, master)
    _pin_two_part_hub(tmp_path, master)

    first = _lanes(inv, tmp_path)
    assert [p["part"] for p in first["parts"]] == ["hub-p1"]
    assert first["not_ready"] == [{"part": "hub-p2", "waiting_on": ["hub-p1"]}]

    with pytest.raises(PartNotReadyError, match="hub-p1"):
        _lanes(inv, tmp_path, part="hub-p2")

    inv.write_text(
        _inventory([("H1", "shared/hot.py", "-", "landed"), ("H2", "shared/hot.py", "H1", "pending")]),
        encoding="utf-8",
        newline="\n",
    )
    second = _lanes(inv, tmp_path)
    assert [p["part"] for p in second["parts"]] == ["hub-p2"]
    assert second["skipped_closed_parts"] == ["hub-p1"]
    assert second["not_ready"] == []
    lane_map = json.loads((tmp_path / second["lane_map"]).read_text(encoding="utf-8"))
    scripts = {p["id"]: p["script"] for lane in lane_map["lanes"] for p in lane["parts"]}
    assert scripts["hub-p2"] and (tmp_path / scripts["hub-p2"]).is_file()


def test_lanes_flags_without_an_inventory_are_refused(tmp_path):
    with pytest.raises(ValueError, match="requires inventory_path"):
        op._dispatch_emit({"plan_path": "p.md", "output_path": str(tmp_path / "x.workflow.mjs"), "lanes": True}, tmp_path)
    with pytest.raises(ValueError, match="part requires lanes"):
        op._dispatch_emit({"inventory_path": "i.md", "part": "a"}, tmp_path)


def test_lanes_refuses_an_explicit_output_path(tmp_path):
    inv = _write_master(tmp_path, _inventory([("R1", "pkg/one.py", "-", "pending")]))
    with pytest.raises(ValueError, match="output_path is not accepted"):
        _lanes(inv, tmp_path, output_path=str(tmp_path / "x.workflow.mjs"))


def test_a_changed_row_set_refuses_against_the_pinned_map(tmp_path):
    inv = _write_master(tmp_path, _inventory([("R1", "pkg/one.py", "-", "pending")]))
    _lanes(inv, tmp_path, hot_files=0)
    inv.write_text(
        _inventory([("R1", "pkg/one.py", "-", "pending"), ("R9", "pkg/nine.py", "-", "pending")]),
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(lanes.LaneMapStaleError, match="R9"):
        _lanes(inv, tmp_path)


def test_a_falsifier_plan_with_a_py_instrument_gets_its_can_report_red_json(tmp_path):
    plan = tmp_path / "docs" / "plans" / "fixture.md"
    plan.parent.mkdir(parents=True)
    instrument = tmp_path / "scripts" / "probe.py"
    instrument.parent.mkdir(parents=True)
    instrument.write_text("import sys\nok = True\nsys.exit(0 if ok else 1)\n", encoding="utf-8")
    plan.write_text(
        textwrap.dedent(
            """\
            ---
            title: fixture
            prime_exit_criterion:
              statement: the probe passes
              falsifier:
                how: python scripts/probe.py
                baseline_output: exit 1
                expected_when_true: exit 0
            ---

            # Fixture

            ```yaml plan-tasks
            - id: R1
              title: does R1
              change_kind: code-edit
              surface: pkg/one.py
              writes:
              - pkg/one.py
              disposition: open
              body: do it
            ```
            """
        ),
        encoding="utf-8",
    )
    inv = _write_master(tmp_path, _inventory([("R1", "pkg/one.py", "-", "pending")]))
    reply = op._dispatch_emit({"inventory_path": str(inv), "output_path": str(tmp_path / "out.workflow.mjs")}, tmp_path)

    report = Path(home_dir(str(tmp_path), "mise-inventory")) / f"{RUN}.can-report-red" / "fixture.json"
    assert report.is_file()
    assert isinstance(json.loads(report.read_text(encoding="utf-8")), dict)
    script = Path(reply["path"]).read_text(encoding="utf-8")
    assert "coordinator:falsifier-integrity-reviewer" in script
    assert f"{RUN}.can-report-red/fixture.json" in script
    assert "_reviewPlans = ['docs/plans/fixture.md']" in script


def test_a_part_the_estimate_cannot_shrink_is_refused_after_three_headroom_reductions_and_leaves_no_files(
    tmp_path, monkeypatch
):
    from coordinator_core.ops.dispatch_emit import emit

    def chain(n):
        return _inventory(
            [(f"H{i}", "shared/hot.py", "-" if i == 1 else f"H{i - 1}", "pending") for i in range(1, n + 1)]
        )

    # A single row's written script is over the cap, so halving the part to one row cannot
    # help: the cap is one byte short of the one-row script.
    probe_root = tmp_path / "probe"
    probe_inv = _write_master(probe_root, chain(1))
    probe_parts = _lanes(probe_inv, probe_root, hot_files=0)["parts"]
    one_row_bytes = max(Path(p["path"]).stat().st_size for p in probe_parts)

    root = tmp_path / "real"
    inv = _write_master(root, chain(5))
    monkeypatch.setattr(emit, "_WORKFLOW_SCRIPT_BYTE_CAP", one_row_bytes - 1)
    monkeypatch.setattr(
        op, "_cost_model", lambda chunk_rows, **kw: ({op._strip_backtick(r["id"]): 1 for r in chunk_rows}, 0, {})
    )

    with pytest.raises(lanes.RowOverBudgetError, match="3 headroom reductions"):
        _lanes(inv, root, hot_files=0)

    leftovers = sorted(p.name for p in Path(home_dir(str(root), "mise-inventory")).iterdir())
    assert not any(name.endswith(".workflow.mjs") for name in leftovers), leftovers


def _plan_chain(n: int) -> str:
    """A hub chain of ``n`` rows, each from its own plan, all writing one file."""
    body = "".join(
        f"| H{i} | `docs/plans/p{i}.md` | does H{i} | `shared/hot.py` | {'-' if i == 1 else f'H{i - 1}'} "
        f"| scoped pytest | S | pending |\n"
        for i in range(1, n + 1)
    )
    return (
        f"---\nrun_id: {RUN}\nstart_sha: {'a' * 40}\n---\n\n# Mise inventory\n\n"
        f"## Chunk table\n\n{_HEADER}{body}"
    )


def _hub_part_rows(root: Path, reply: dict) -> list:
    lane_map = json.loads((root / reply["lane_map"]).read_text(encoding="utf-8"))
    (lane,) = lane_map["lanes"]
    return [p["rows"] for p in lane["parts"]]


def test_a_hub_of_plans_is_cut_to_one_plan_per_part_by_the_per_plan_cost(tmp_path, monkeypatch):
    # Each plan's material measures 400 KB over the shell, so two plans overrun the part
    # budget while one fits; a row-only estimate packs all three plans into one part.
    monkeypatch.setattr(op, "_plan_probe_bytes", lambda *args, **kwargs: 400_000)
    inv = _write_master(tmp_path, _plan_chain(3))

    reply = _lanes(inv, tmp_path, hot_files=0)

    assert _hub_part_rows(tmp_path, reply) == [["H1"], ["H2"], ["H3"]]
    assert [p["part"] for p in reply["parts"]] == ["a-p1"]
    assert reply["part_resplits"] == 0


def test_a_hub_that_composes_over_the_cap_is_resplit_at_plan_boundaries_without_headroom_retries(
    tmp_path, monkeypatch
):
    from coordinator_core.ops.dispatch_emit import emit

    monkeypatch.setattr(op, "_plan_probe_bytes", lambda *args, **kwargs: None)

    def written_size(n: int) -> int:
        probe_root = tmp_path / f"probe{n}"
        probe_inv = _write_master(probe_root, _plan_chain(n))
        return Path(_lanes(probe_inv, probe_root, hot_files=0)["parts"][0]["path"]).stat().st_size

    one_plan, two_plans = written_size(1), written_size(2)
    assert one_plan < two_plans
    # Strictly between the written one-plan and two-plan scripts: a one-plan part fits, a part of
    # two or three plans cannot.
    cap = one_plan + (two_plans - one_plan) // 2

    root = tmp_path / "real"
    inv = _write_master(root, _plan_chain(3))
    # The row estimate fits all three plans in one part, but the composed three-plan script is
    # over this cap: the resplit cuts at a plan boundary instead of retrying at lower headroom.
    monkeypatch.setattr(emit, "_WORKFLOW_SCRIPT_BYTE_CAP", cap)
    monkeypatch.setattr(
        op, "_cost_model", lambda chunk_rows, **kw: ({op._strip_backtick(r["id"]): 1 for r in chunk_rows}, 0, {})
    )

    reply = _lanes(inv, root, hot_files=0)

    parts = _hub_part_rows(root, reply)
    assert reply["part_resplits"] >= 1
    assert [r for rows in parts for r in rows] == ["H1", "H2", "H3"]
    assert parts[0] == ["H1"] and len(parts) >= 2
    assert Path(reply["parts"][0]["path"]).stat().st_size <= emit._WORKFLOW_SCRIPT_BYTE_CAP


def test_a_part_that_composes_over_the_cap_is_resplit_in_place_with_later_parts_renumbered(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import emit

    def written_size(n: int) -> int:
        probe_root = tmp_path / f"probe{n}"
        rows = [(f"H{i}", "shared/hot.py", "-" if i == 1 else f"H{i - 1}", "pending") for i in range(1, n + 1)]
        probe_inv = _write_master(probe_root, _inventory(rows))
        return Path(_lanes(probe_inv, probe_root, hot_files=0)["parts"][0]["path"]).stat().st_size

    two_rows, three_rows = written_size(2), written_size(3)
    assert two_rows < three_rows
    # Strictly between the written 2-row and 3-row scripts, nearer the 3-row: a 2-row part fits, a 3-row part cannot.
    cap = two_rows + (three_rows - two_rows) * 2 // 3

    root = tmp_path / "real"
    inv = _write_master(
        root,
        _inventory(
            [
                ("H1", "shared/hot.py", "-", "pending"),
                ("H2", "shared/hot.py", "H1", "pending"),
                ("H3", "shared/hot.py", "H2", "pending"),
                ("H4", "shared/hot.py", "H3", "pending"),
                ("H5", "shared/hot.py", "H4", "pending"),
            ]
        ),
    )
    # The row stub is sized so the first estimate packs [H1..H4] | [H5] and the tightened one still
    # fits all four, so the over-cap part is halved: the written 2-row part fits, so exactly one
    # split happens, and it must renumber the pre-existing later part H5.
    per_row = int(cap * 0.95 / 4.5)
    monkeypatch.setattr(emit, "_WORKFLOW_SCRIPT_BYTE_CAP", cap)
    monkeypatch.setattr(
        op, "_cost_model", lambda chunk_rows, **kw: ({op._strip_backtick(r["id"]): per_row for r in chunk_rows}, 0, {})
    )

    reply = _lanes(inv, root, hot_files=0)

    assert reply["part_resplits"] == 1
    assert [p["part"] for p in reply["parts"]] == ["a-p1"]
    lane_map = json.loads((root / reply["lane_map"]).read_text(encoding="utf-8"))
    (lane,) = lane_map["lanes"]
    assert [p["id"] for p in lane["parts"]] == ["a-p1", "a-p2", "a-p3"]
    assert [p["rows"] for p in lane["parts"]] == [["H1", "H2"], ["H3", "H4"], ["H5"]]
    assert [p["after"] for p in lane["parts"]] == [[], ["a-p1"], ["a-p2"]]
    assert lane["parts"][0]["script"] and not any(p["script"] for p in lane["parts"][1:])
    assert Path(reply["parts"][0]["path"]).stat().st_size <= emit._WORKFLOW_SCRIPT_BYTE_CAP
    names = {p.name for p in Path(home_dir(str(root), "mise-inventory")).iterdir()}
    assert f"{RUN}-a.md" not in names and f"{RUN}-a-p1.md" in names
