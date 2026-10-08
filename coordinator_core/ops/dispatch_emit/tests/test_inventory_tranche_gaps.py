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
    # a+b are 3 rows together: deferred as a group; c fits.
    assert report["plans"] == ["c"]
    reasons = {d["plan"]: d["reason"] for d in report["deferred"]}
    assert set(reasons) == {"a", "b"}
    assert "src/shared.py" in reasons["a"] and "exceed row_budget 2 alone" in reasons["a"]
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
