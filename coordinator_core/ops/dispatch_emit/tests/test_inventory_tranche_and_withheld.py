"""Inventory mint: a fully-withheld plan spine is withheld (never flattened to a
whole-plan executor), and `row_budget` cuts a dependency-ordered tranche."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import inventory_mint as im


def _plan_text(rows: list) -> str:
    return "# plan\n\n## Tasks\n\n```yaml plan-tasks\n" + yaml.safe_dump(rows, sort_keys=False) + "```\n"


def _chunk(cid: str, path: str, **extra) -> dict:
    row = {"id": cid, "title": cid, "surface": path, "writes": [path], "change_kind": "code-edit"}
    row.update(extra)
    return row


def _repo(tmp_path: Path, plans: dict, table: list) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "mise-inventory").mkdir(parents=True)
    for name, rows in plans.items():
        (tmp_path / "docs" / "plans" / f"{name}.md").write_text(_plan_text(rows), encoding="utf-8")
    head = (
        "---\nrun_id: 20261008T000000-tr\n---\n\n## Chunk table\n\n"
        "| id | spec path | summary | footprint | deps | verification | complexity | disposition |\n"
        "|---|---|---|---|---|---|---|---|\n"
    )
    body = "".join(
        f"| {i} | `docs/plans/{i}.md` | s {i} | `docs/plans/{i}.md` | {deps} | v | S | in_progress |\n"
        for i, deps in table
    )
    inv = tmp_path / "state" / "mise-inventory" / "inv.md"
    inv.write_text(head + body, encoding="utf-8")
    return inv


def _n_rows(prefix: str, n: int) -> list:
    return [_chunk(f"{prefix}{k}", f"src/{prefix}{k}.py") for k in range(n)]


def test_fully_withheld_plan_is_withheld_not_flattened(tmp_path):
    held = [
        _chunk(
            "H1",
            "src/h1.py",
            depends_on_plan=[{"plan": "docs/plans/pred.md", "chunk": "P1", "gate_kind": "output-consumption-runtime"}],
        ),
        _chunk("H2", "src/h2.py", depends_on=[{"chunk": "H1", "gate_kind": "output-consumption-runtime"}]),
    ]
    inv = _repo(
        tmp_path,
        {"pred": [_chunk("P1", "src/p1.py")], "held": held, "free": _n_rows("F", 1), "after": _n_rows("A", 1)},
        [("free", "—"), ("held", "—"), ("after", "held")],
    )
    withheld: dict = {}
    rows = im.mint_rows(
        im.parse_chunk_table(inv.read_text(encoding="utf-8")), inventory_path=inv, withheld_out=withheld
    )
    ids = {r["id"] for r in rows}
    assert ids == {"free.F0"}
    assert set(withheld) == {"held", "after"}
    assert "fully withheld" in withheld["held"] and "depends_on_plan" in withheld["held"]
    assert "held" in withheld["after"]


def test_plan_without_a_spine_still_mints_one_whole_plan_row(tmp_path):
    inv = _repo(tmp_path, {}, [("doc", "—")])
    (tmp_path / "docs" / "plans" / "doc.md").write_text("# not a plan\n", encoding="utf-8")
    withheld: dict = {}
    rows = im.mint_rows(
        im.parse_chunk_table(inv.read_text(encoding="utf-8")), inventory_path=inv, withheld_out=withheld
    )
    assert [r["id"] for r in rows] == ["doc"]
    assert withheld == {}


def _tranche_inventory(tmp_path):
    return _repo(
        tmp_path,
        {"a": _n_rows("A", 3), "b": _n_rows("B", 3), "c": _n_rows("C", 2), "d": _n_rows("D", 1)},
        [("a", "—"), ("b", "a"), ("c", "—"), ("d", "b")],
    )


def test_row_budget_takes_whole_plans_in_dependency_order(tmp_path):
    inv = _tranche_inventory(tmp_path)
    report: dict = {}
    text, _ = im.mint_spine(str(inv), row_budget=5, tranche_out=report)
    # a (3) fits; b (3) would make 6; c (2) fits and is independent; d waits on b.
    assert report["plans"] == ["a", "c"]
    assert report["rows"] == 5
    assert {d["plan"] for d in report["deferred"]} == {"b", "d"}
    assert all(f"{p}." in text for p in ("a", "c"))
    assert "b.B0" not in text and "d.D0" not in text


def test_row_budget_never_leaves_an_edge_to_a_deferred_plan(tmp_path):
    inv = _tranche_inventory(tmp_path)
    report: dict = {}
    text, _ = im.mint_spine(str(inv), row_budget=8, tranche_out=report)
    assert report["plans"] == ["a", "b", "c"]
    spine = yaml.safe_load(text.split("```yaml plan-tasks\n", 1)[1].split("```", 1)[0])
    ids = {r["id"] for r in spine}
    for row in spine:
        for edge in row.get("depends_on", []):
            assert edge["chunk"] in ids


def test_row_budget_smaller_than_every_plan_refuses(tmp_path):
    inv = _tranche_inventory(tmp_path)
    with pytest.raises(im.InventoryTooLargeError, match="--row-budget"):
        im.mint_spine(str(inv), row_budget=1)


def test_row_budget_supersedes_the_max_rows_refusal(tmp_path):
    inv = _tranche_inventory(tmp_path)
    im.mint_spine(str(inv), max_rows=2, row_budget=5)


def test_max_rows_refusal_names_the_tranche_option(tmp_path):
    inv = _tranche_inventory(tmp_path)
    with pytest.raises(im.InventoryTooLargeError, match="--row-budget N"):
        im.mint_spine(str(inv), max_rows=2)


def test_tranche_number_continues_past_outputs_without_a_record(tmp_path):
    """Numbering reads emitted outputs, not just `-tN.md` records."""
    inv = _tranche_inventory(tmp_path)
    run = "20261008T000000-tr"
    (inv.parent / f"{run}-t1-p1.workflow.mjs").write_text("x", encoding="utf-8")
    out: dict = {}
    im.mint_spine(str(inv), row_budget=5, tranche_inventory_out=out)
    assert Path(out["path"]).name == "inv-t2.md"
    assert f"run_id: {run}-t2" in out["text"]


def test_spent_tranche_record_with_nonbinding_budget_mints_next_n(tmp_path):
    inv = _tranche_inventory(tmp_path)
    t1_out: dict = {}
    im.mint_spine(str(inv), row_budget=5, tranche_inventory_out=t1_out)
    t1 = Path(t1_out["path"])
    t1.write_text(t1_out["text"], encoding="utf-8")
    run = "20261008T000000-tr"
    (inv.parent / f"{run}-t1.workflow.mjs").write_text("x", encoding="utf-8")
    (inv.parent / f"{run}-t1-resume.workflow.mjs").write_text("x", encoding="utf-8")
    out: dict = {}
    _, spine_path = im.mint_spine(str(t1), row_budget=50, tranche_inventory_out=out)
    assert spine_path.name == f"{run}-t2.spine.md"
    assert Path(out["path"]).name == "inv-t2.md"


def test_unspent_tranche_record_with_nonbinding_budget_keeps_its_run_id(tmp_path):
    inv = _tranche_inventory(tmp_path)
    t1_out: dict = {}
    im.mint_spine(str(inv), row_budget=5, tranche_inventory_out=t1_out)
    t1 = Path(t1_out["path"])
    t1.write_text(t1_out["text"], encoding="utf-8")
    out: dict = {}
    _, spine_path = im.mint_spine(str(t1), row_budget=50, tranche_inventory_out=out)
    assert spine_path.name == "20261008T000000-tr-t1.spine.md"
    assert not out


def test_shared_path_group_with_untaken_external_prerequisite_keeps_fireable_members():
    groups = [["a", "b", "c"], ["z"]]
    size = {"a": 1, "b": 1, "c": 1, "z": 5}
    prereqs = {"a": set(), "b": {"z"}, "c": {"b"}, "z": set()}
    shared = {"a": "s/one.json"}
    taken, deferred = im._pick_groups(groups, size, prereqs, shared, 3)
    assert taken == ["a"]
    assert "prerequisite z" in deferred["b"]
    assert "prerequisite b" in deferred["c"]


def _rows(item: str, n: int, edge_to: str | None = None) -> list:
    rows = [{"id": f"{item}.{item}{k}"} for k in range(n)]
    if edge_to:
        rows[0]["depends_on"] = [{"chunk": edge_to}]
    return rows


@pytest.mark.parametrize("budget", [100, 95])
def test_unblocked_72_row_group_is_taken_within_budget(budget):
    selected, report = im.select_tranche(_rows("big", 72), ["big"], budget)
    assert report["plans"] == ["big"] and len(selected) == 72


@pytest.mark.parametrize("budget", [100, 95])
def test_edge_onto_unemitted_chunk_of_free_item_defers_dependent_not_the_free_item(budget):
    # An unemitted chunk has not run this tranche, so its dependent must wait for it.
    rows = _rows("free", 1) + _rows("dep", 72, edge_to="free.F9")
    selected, report = im.select_tranche(rows, ["free", "dep"], budget)
    assert report["plans"] == ["free"]
    assert len(selected) == 1


def test_all_blocked_refusal_names_each_group_size_and_blocker():
    rows = _rows("aa", 3, edge_to="bb.B0") + _rows("bb", 4, edge_to="aa.A0")
    with pytest.raises(im.InventoryTooLargeError) as exc:
        im.select_tranche(rows, ["aa", "bb"], 100)
    msg = str(exc.value)
    assert "smallest dispatchable group" not in msg
    for item, n, blocker in (("aa", "3", "bb"), ("bb", "4", "aa")):
        assert re.search(rf"{item}\W[^;\n]*\b{n} rows?\b[^;\n]*prerequisite {blocker}", msg), msg


def test_bound_shared_path_group_still_defers_whole():
    groups = [["a", "b"], ["z"]]
    size = {"a": 1, "b": 1, "z": 5}
    prereqs = {"a": set(), "b": {"z"}, "z": set()}
    shared = {"a": "s/one.json"}
    taken, deferred = im._pick_groups(
        groups, size, prereqs, shared, 3, {"a": "b (depends_on_plan of a)"}
    )
    assert taken == [] and {"a", "b"} <= set(deferred)
