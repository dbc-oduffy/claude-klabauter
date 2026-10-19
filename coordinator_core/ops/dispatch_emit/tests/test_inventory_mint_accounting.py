"""mint_spine's accounting_out ledger: every Chunk-table item either owns a
minted row or carries a reason, at every non-minting site."""

from __future__ import annotations

import yaml

from coordinator_core.ops.dispatch_emit import inventory_mint as im
from coordinator_core.ops.dispatch_emit.inventory_accounting import IN_ANOTHER_PART
from coordinator_core.ops.dispatch_emit.tests.test_inventory_tranche_and_withheld import (
    _chunk,
    _n_rows,
    _repo,
)

_HEAD = (
    "---\nrun_id: 20261019T000000-acct\n---\n\n## Chunk table\n\n"
    "| id | spec path | summary | footprint | deps | verification | complexity | disposition |\n"
    "|---|---|---|---|---|---|---|---|\n"
)


def _dogfood_inventory(tmp_path):
    """59 items: 43 pending, 2 pending dependents of routed-out rows, 7
    already-fixed, 7 routed-out (one `plan not prepped`)."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "mise-inventory").mkdir(parents=True)
    lines = []

    def add(i, deps="—", disp="pending"):
        lines.append(f"| m{i} | — | s{i} | `src/m{i}.py` | {deps} | v | S | {disp} |\n")

    for i in range(1, 44):
        add(i)
    for i in range(44, 51):
        add(i, disp="already-fixed")
    for i in range(51, 57):
        add(i, disp=f"routed-out: reason {i}")
    add(57, disp="routed-out: plan not prepped")
    add(58, deps="m51")
    add(59, deps="m58")
    inv = tmp_path / "state" / "mise-inventory" / "inv.md"
    inv.write_text(_HEAD + "".join(lines), encoding="utf-8")
    return inv


def _ids(inv):
    return [im._strip_backtick(r["id"]) for r in im.parse_chunk_table(inv.read_text(encoding="utf-8"))]


def _minted_items(text, item_ids):
    rows = yaml.safe_load(text.split("```yaml plan-tasks\n", 1)[1].split("```", 1)[0]) or []
    return set(im._row_item_ids(rows, item_ids))


def test_every_dogfood_item_is_minted_or_carries_a_reason(tmp_path):
    inv = _dogfood_inventory(tmp_path)
    ids = _ids(inv)
    assert len(ids) == 59
    ledger: dict = {}
    text, _ = im.mint_spine(str(inv), accounting_out=ledger)
    minted = _minted_items(text, ids)
    assert minted.isdisjoint(ledger)
    assert minted | set(ledger) == set(ids)
    assert len(minted) == 43
    assert ledger["m57"] == "routed-out: plan not prepped"
    assert ledger["m44"] == "already-fixed"
    assert ledger["m58"] == "dependency m51 routed out"
    assert ledger["m59"] == "dependency m58 routed out"


def test_tranche_skipped_names_every_non_minted_item(tmp_path):
    inv = _dogfood_inventory(tmp_path)
    ids = _ids(inv)
    ledger: dict = {}
    report: dict = {}
    text, _ = im.mint_spine(str(inv), row_budget=20, tranche_out=report, accounting_out=ledger)
    skipped = {s["plan"] for s in report["skipped"]}
    deferred = {d["plan"] for d in report["deferred"]}
    taken = set(report["plans"])
    assert skipped | deferred | taken == set(ids)
    assert not (skipped & deferred) and not (skipped & taken) and not (deferred & taken)
    assert len(taken) == 20 and len(deferred) == 23
    assert set(ledger) == skipped | deferred
    assert all("no live rows" in s["reason"] for s in report["skipped"])
    assert any("plan not prepped" in s["reason"] for s in report["skipped"] if s["plan"] == "m57")
    assert next(s["reason"] for s in report["skipped"] if s["plan"] == "m58").endswith("dependency m51 routed out)")
    assert ledger["m43"] and "row_budget" in ledger["m43"]


def test_part_marks_items_of_other_parts(tmp_path):
    inv = _dogfood_inventory(tmp_path)
    ids = _ids(inv)
    ledger: dict = {}
    items: list = []
    text, _ = im.mint_spine(str(inv), part=(1, 2), accounting_out=ledger, part_items_out=items)
    other = {i for i, why in ledger.items() if why == IN_ANOTHER_PART}
    assert other and other.isdisjoint(items)
    assert set(items) | set(ledger) == set(ids)
    assert _minted_items(text, ids) == set(items)


def test_plan_sites_withheld_landed_and_skip_landed(tmp_path):
    held = [
        _chunk(
            "H1",
            "src/h1.py",
            depends_on_plan=[{"plan": "docs/plans/pred.md", "chunk": "P1", "gate_kind": "output-consumption-runtime"}],
        )
    ]
    inv = _repo(
        tmp_path,
        {
            "pred": [_chunk("P1", "src/p1.py")],
            "held": held,
            "after": _n_rows("A", 1),
            "done": [_chunk("D1", "src/d1.py", disposition="coded")],
            "free": _n_rows("F", 1),
        },
        [("free", "—"), ("held", "—"), ("after", "held"), ("done", "—")],
    )
    ledger: dict = {}
    rows = im.mint_rows(
        im.parse_chunk_table(inv.read_text(encoding="utf-8")), inventory_path=inv, accounting_out=ledger
    )
    minted = {r["id"].split(".")[0] for r in rows}
    assert minted == {"free"}
    assert "fully withheld" in ledger["held"]
    assert "held" in ledger["after"]
    assert "no live rows" in ledger["done"]

    ledger = {}
    rows = im.mint_rows(
        im.parse_chunk_table(inv.read_text(encoding="utf-8")),
        inventory_path=inv,
        skip_landed=True,
        accounting_out=ledger,
    )
    assert {r["id"].split(".")[0] for r in rows}.isdisjoint(ledger)
    assert set(ledger) | {r["id"].split(".")[0] for r in rows} == {"free", "held", "after", "done"}
