"""Tranche selector: satisfied cross-plan predecessors, shared-path groups,
skipped plans, per-tranche inventory files, remaining estimate, and the
inventory `--review-only` route; consumes-derived withholding in read_spine."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import inventory_mint as im
from coordinator_core.ops.dispatch_emit import op as op_mod
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
from coordinator_core.ops.dispatch_emit.spine_read import read_spine
from coordinator_core.ops.dispatch_emit.tests.test_emit_wake_digest import (
    _V5_FRAGMENT,
    _V5_STAGE_SCHEMAS,
)
from coordinator_core.ops.dispatch_emit.tests.test_inventory_tranche_and_withheld import (
    _chunk,
    _n_rows,
    _plan_text,
    _repo,
)


def _tranche(inv, budget, **kw):
    report: dict = {}
    text, spine = im.mint_spine(str(inv), row_budget=budget, tranche_out=report, **kw)
    return report, text, spine


def test_coded_cross_plan_predecessor_counts_as_satisfied(tmp_path):
    dep = [
        _chunk(
            "D1",
            "src/d1.py",
            depends_on_plan=[{"plan": "docs/plans/pred.md", "chunk": "P1", "gate_kind": "output-consumption-runtime"}],
        )
    ]
    inv = _repo(
        tmp_path,
        {"pred": [_chunk("P1", "src/p1.py", disposition="coded")], "dep": dep},
        [("pred", "—"), ("dep", "—")],
    )
    report, _, _ = _tranche(inv, 5)
    assert report["plans"] == ["dep"]
    assert [s["plan"] for s in report["skipped"]] == ["pred"]


def test_plans_writing_one_path_are_taken_together(tmp_path):
    inv = _repo(
        tmp_path,
        {
            "a": [_chunk("A0", "src/shared.py")],
            "b": [_chunk("B0", "src/shared.py"), _chunk("B1", "src/b1.py")],
            "c": _n_rows("C", 1),
        },
        [("a", "—"), ("b", "—"), ("c", "—")],
    )
    report, _, _ = _tranche(inv, 2)
    # a+b are 3 rows together, over the budget: split plan-by-plan. a (1 row)
    # and c (1 row) fill the tranche; b waits for the next one.
    assert report["plans"] == ["a", "c"]
    reasons = {d["plan"]: d["reason"] for d in report["deferred"]}
    assert set(reasons) == {"b"}
    assert "src/shared.py" in reasons["b"] and "split across tranches" in reasons["b"]
    report, _, _ = _tranche(inv, 3)
    assert report["plans"] == ["a", "b"]


def test_append_only_shared_path_does_not_group(tmp_path):
    inv = _repo(
        tmp_path,
        {
            "a": [_chunk("A0", "src/hub.py", appends=["src/hub.py"]), _chunk("A1", "src/a1.py")],
            "b": [_chunk("B0", "src/hub.py", appends=["src/hub.py"]), _chunk("B1", "src/b1.py")],
        },
        [("a", "—"), ("b", "—")],
    )
    report, _, _ = _tranche(inv, 2)
    assert report["plans"] == ["a"]


def test_plan_with_no_live_rows_is_skipped_and_reported(tmp_path):
    held = [
        _chunk(
            "H1",
            "src/h1.py",
            depends_on_plan=[{"plan": "docs/plans/pred.md", "chunk": "P1", "gate_kind": "output-consumption-runtime"}],
        )
    ]
    inv = _repo(
        tmp_path,
        {"pred": [_chunk("P1", "src/p1.py")], "held": held, "free": _n_rows("F", 1)},
        [("free", "—"), ("held", "—")],
    )
    withheld: dict = {}
    report, _, _ = _tranche(inv, 5, withheld_out=withheld)
    assert report["plans"] == ["free"]
    assert report["rows"] == 1
    assert [s["plan"] for s in report["skipped"]] == ["held"]
    assert "withheld" in report["skipped"][0]["reason"] or "depends_on_plan" in report["skipped"][0]["reason"]
    assert report["remaining"]["withheld_not_counted"] == 1
    assert report["remaining"]["rows_remaining"] == 0
    assert report["remaining"]["rows_waiting_on_predecessors"] == 1
    assert [w["plan"] for w in report["remaining"]["waiting_on_predecessors"]] == ["held"]


def test_remaining_estimate_simulates_further_tranches(tmp_path):
    inv = _repo(
        tmp_path,
        {"a": _n_rows("A", 3), "b": _n_rows("B", 3), "c": _n_rows("C", 3), "d": _n_rows("D", 1)},
        [("a", "—"), ("b", "a"), ("c", "b"), ("d", "—")],
    )
    report, _, _ = _tranche(inv, 4)
    assert report["plans"] == ["a", "d"]
    # b then c each need their own tranche once their predecessor lands.
    assert report["remaining"] == {"passes_remaining": 2, "rows_remaining": 6}


def test_tranche_writes_its_own_numbered_inventory(tmp_path):
    inv = _repo(
        tmp_path,
        {"a": _n_rows("A", 2), "b": _n_rows("B", 2)},
        [("a", "—"), ("b", "—")],
    )
    inv_dir = inv.parent
    texts = {}
    for n in (1, 2):
        info: dict = {}
        report: dict = {}
        _, spine = im.mint_spine(str(inv), row_budget=2, tranche_out=report, tranche_inventory_out=info)
        info["path"].write_text(info["text"], encoding="utf-8")
        texts[n] = (report, info, spine)
        # Land the tranche's plan so the next one picks the other.
        for plan in report["plans"]:
            p = tmp_path / "docs" / "plans" / f"{plan}.md"
            p.write_text(p.read_text().replace("change_kind: code-edit", "change_kind: code-edit\n  disposition: coded"), encoding="utf-8")
    assert [texts[n][1]["path"].name for n in (1, 2)] == ["inv-t1.md", "inv-t2.md"]
    report1, info1, spine1 = texts[1]
    assert report1["run_id"] == "20261008T000000-tr-t1"
    assert "run_id: 20261008T000000-tr-t1" in info1["text"]
    assert "start_sha: HEAD" in info1["text"]
    assert "`docs/plans/b.md`" not in info1["text"] and "`docs/plans/a.md`" in info1["text"]
    assert spine1.name == "20261008T000000-tr-t1.spine.md"
    assert texts[2][0]["plans"] == ["b"]
    assert "run_id: 20261008T000000-tr-t1" not in texts[2][1]["text"]
    assert inv.read_text().count("20261008T000000-tr\n") == 1  # main spine inventory untouched


@pytest.fixture
def _review_loaders(monkeypatch):
    monkeypatch.setattr(op_mod.review_mint_op, "load_fragment", lambda: _V5_FRAGMENT)
    monkeypatch.setattr(op_mod.review_mint_op, "load_stage_schemas", lambda: _V5_STAGE_SCHEMAS)


def test_inventory_review_only_emits_review_without_rows(tmp_path, _review_loaders):
    inv = _repo(tmp_path, {"a": _n_rows("A", 3)}, [("a", "—")])
    result = _dispatch_emit(
        {
            "inventory_path": str(inv),
            "target_root": str(tmp_path),
            "review_only_rows": ["a.A0", "a.A1"],
            "run_base_sha": "abc1234def",
            "max_rows": 1,
        }
    )
    assert result["ok"] is True, result["findings"]
    script = next(tmp_path.glob("state/mise-inventory/*.workflow.mjs")).read_text()
    assert "Review-only: rows a.A0, a.A1, base abc1234def" in script


def test_inventory_review_only_cli_requires_rows(tmp_path, capsys):
    from coordinator_core.ops.dispatch_emit import cli

    inv = _repo(tmp_path, {"a": _n_rows("A", 1)}, [("a", "—")])
    code = cli.main(["--inventory", str(inv), "--review-only", "--run-base", "abc1234", "--repo-root", str(tmp_path)])
    assert code == cli.EXIT_USAGE
    assert "names no row" in capsys.readouterr().err
    code = cli.main(
        ["--inventory", str(inv), "--review-only", "--rows", "a.A0", "--row-budget", "3", "--run-base", "abc1234"]
    )
    assert code == cli.EXIT_USAGE


def _consume_plan(tmp_path, rows):
    path = tmp_path / "p.md"
    path.write_text(_plan_text(rows), encoding="utf-8")
    return path


_HELD = {"plan": "docs/plans/pred.md", "chunk": "P1", "gate_kind": "output-consumption-runtime"}


def test_withholding_propagates_along_consumes(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "docs" / "plans" / "pred.md").write_text(_plan_text([_chunk("P1", "src/p1.ts")]), encoding="utf-8")
    plan = _consume_plan(
        tmp_path,
        [
            _chunk("B1", "src/b1.ts", depends_on_plan=[_HELD]),
            _chunk("S1", "src/s1.ts", consumes=["src/b1.ts"]),
            _chunk("S2", "src/s2.ts", consumes=["src/s1.ts"]),
            _chunk("H1", "src/hub.md", appends=["src/hub.md"], depends_on_plan=[_HELD]),
            _chunk("R1", "src/r1.ts", consumes=["src/hub.md"]),
            _chunk("F1", "src/f1.ts"),
        ],
    )
    exclusions: list = []
    rows = read_spine(plan, exclusions=exclusions)
    assert {r.id for r in rows} == {"R1", "F1"}
    detail = {e["id"]: e["detail"] for e in exclusions}
    assert detail["S1"] == "withheld: consumes src/b1.ts written by withheld B1"
    assert detail["S2"].startswith("withheld: consumes src/s1.ts written by withheld S1")


def _hub_set(tmp_path, n=6, rows_each=2):
    names = [f"p{k}" for k in range(n)]
    plans = {
        nm: [_chunk(f"{nm.upper()}H", "src/hub.py")] + _n_rows(nm.upper(), rows_each - 1)
        for nm in names
    }
    return _repo(tmp_path, plans, [(nm, "—") for nm in names]), names


def test_oversize_hub_group_splits_across_passes_and_estimate_counts_it(tmp_path):
    inv, names = _hub_set(tmp_path)  # six plans of 2 rows write src/hub.py: 12 rows
    report, _, _ = _tranche(inv, 5)
    assert report["plans"] == names[:2]
    assert report["rows"] == 4
    assert "unplaceable" not in report["remaining"]
    assert report["remaining"] == {"passes_remaining": 2, "rows_remaining": 8}


def test_group_that_fits_stays_whole(tmp_path):
    inv, names = _hub_set(tmp_path, n=3)
    report, _, _ = _tranche(inv, 6)
    assert report["plans"] == names
    assert report["remaining"]["passes_remaining"] == 0


def test_only_a_single_over_budget_plan_is_unplaceable(tmp_path):
    inv = _repo(
        tmp_path,
        {
            "big": [_chunk("B0", "src/hub.py")] + _n_rows("BX", 4),
            "s1": [_chunk("S0", "src/hub.py")],
            "s2": [_chunk("T0", "src/hub.py")],
            "dep": _n_rows("D", 1),
        },
        [("s1", "—"), ("s2", "—"), ("big", "—"), ("dep", "big")],
    )
    report, _, _ = _tranche(inv, 3)
    assert report["plans"] == ["s1", "s2"]
    remaining = report["remaining"]
    assert remaining["unplaceable"] == ["big"]
    assert remaining["blocked_by_unplaceable"] == ["dep"]
    assert remaining["passes_remaining"] == 0
    reasons = {d["plan"]: d["reason"] for d in report["deferred"]}
    assert "exceed row_budget 3 alone" in reasons["big"]


def test_part_review_specs_cover_only_that_parts_plans(tmp_path, monkeypatch, _review_loaders):
    inv = _repo(
        tmp_path,
        {nm: _n_rows(nm.upper(), 2) for nm in ("a", "b", "c", "d")},
        [(nm, "—") for nm in ("a", "b", "c", "d")],
    )
    seen: list = []
    monkeypatch.setattr(
        op_mod, "_inventory_review_specs", lambda inventory, only: seen.append(only) or []
    )
    _dispatch_emit(
        {"inventory_path": str(inv), "target_root": str(tmp_path), "inventory_part": [1, 2]}
    )
    assert seen == [frozenset({"docs/plans/a.md", "docs/plans/b.md"})]


def _cyclic_groups():
    # {a, b} and {c, d} each share a path; a needs c and d needs b, so each
    # group holds the other's prerequisite while the plans themselves are ordered.
    groups = [["a", "b"], ["c", "d"]]
    size = {"a": 2, "b": 2, "c": 2, "d": 2}
    prereqs = {"a": {"c"}, "b": set(), "c": set(), "d": {"b"}}
    shared = {"a": "s/one.ts", "c": "s/two.ts"}
    return groups, size, prereqs, shared


def test_groups_holding_each_others_prerequisites_are_taken_as_one_unit():
    groups, size, prereqs, shared = _cyclic_groups()
    taken, deferred = im._pick_groups(groups, size, prereqs, shared, 8)
    assert sorted(taken) == ["a", "b", "c", "d"] and deferred == {}


def test_an_over_budget_group_cycle_places_plan_by_plan():
    groups, size, prereqs, shared = _cyclic_groups()
    taken, deferred = im._pick_groups(groups, size, prereqs, shared, 4)
    assert sorted(taken) == ["b", "c"]
    assert set(deferred) == {"a", "d"}
