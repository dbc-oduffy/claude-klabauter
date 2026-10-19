"""An inventory mint honours a non-dispatchable disposition on the source plan row:
the row mints nothing, the accounting names why, and its dependent is released."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import inventory_mint as im
from coordinator_core.ops.dispatch_emit.tests.test_inventory_tranche_and_withheld import (
    _chunk,
    _plan_text,
    _repo,
)

_EDGE = [{"chunk": "C1", "gate_kind": "output-consumption-runtime"}]


def _single_chunk_inventory(tmp_path, c1_disposition):
    """Two single-chunk-reference items, `C1` and `C2` (C2 depends on C1), of one plan."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "mise-inventory").mkdir(parents=True)
    c1 = _chunk("C1", "src/c1.py")
    if c1_disposition:
        c1["disposition"] = c1_disposition
    plan_rows = [c1, _chunk("C2", "src/c2.py", depends_on=_EDGE)]
    (tmp_path / "docs" / "plans" / "live.md").write_text(_plan_text(plan_rows), encoding="utf-8")
    head = (
        "---\nrun_id: 20261019T000000-de\n---\n\n## Chunk table\n\n"
        "| id | spec path | summary | footprint | deps | verification | complexity | disposition |\n"
        "|---|---|---|---|---|---|---|---|\n"
    )
    body = (
        "| C1 | `docs/plans/live.md` | s1 | `src/c1.py` | — | v | S | pending |\n"
        "| C2 | `docs/plans/live.md` | s2 | `src/c2.py` | C1 | v | S | pending |\n"
    )
    inv = tmp_path / "state" / "mise-inventory" / "inv.md"
    inv.write_text(head + body, encoding="utf-8")
    return inv


def _mint(inv, **kwargs):
    ledger: dict = {}
    rows = im.mint_rows(
        im.parse_chunk_table(inv.read_text(encoding="utf-8")),
        inventory_path=inv,
        accounting_out=ledger,
        **kwargs,
    )
    return {r["id"]: r for r in rows}, ledger


def test_single_chunk_delivered_elsewhere_is_skipped_and_dependent_released(tmp_path):
    inv = _single_chunk_inventory(tmp_path, "delivered_elsewhere")
    minted, ledger = _mint(inv)
    assert set(minted) == {"C2"}
    assert "depends_on" not in minted["C2"]
    assert "delivered_elsewhere" in ledger["C1"]


def test_every_non_dispatchable_disposition_skips_the_row(tmp_path):
    for disposition in sorted(im.NON_DISPATCHABLE_DISPOSITIONS - {"coded"}):
        sub = tmp_path / disposition
        sub.mkdir()
        minted, ledger = _mint(_single_chunk_inventory(sub, disposition))
        assert set(minted) == {"C2"}, disposition
        assert disposition in ledger["C1"]


def test_open_row_is_still_minted_and_still_gates_its_dependent(tmp_path):
    inv = _single_chunk_inventory(tmp_path, None)
    minted, ledger = _mint(inv)
    assert set(minted) == {"C1", "C2"}
    assert [e["chunk"] for e in minted["C2"]["depends_on"]] == ["C1"]
    assert ledger == {}


def test_coded_row_still_mints_without_skip_landed_and_skips_with_it(tmp_path):
    inv = _single_chunk_inventory(tmp_path, "coded")
    minted, _ = _mint(inv)
    assert set(minted) == {"C1", "C2"}
    skipped: list = []
    minted, ledger = _mint(inv, skip_landed=True, skipped_out=skipped)
    assert set(minted) == {"C2"}
    assert skipped == ["C1"]
    assert "skip_landed" in ledger["C1"]


def test_delivered_elsewhere_is_not_reported_as_landed(tmp_path):
    inv = _single_chunk_inventory(tmp_path, "delivered_elsewhere")
    skipped: list = []
    _mint(inv, skip_landed=True, skipped_out=skipped)
    assert skipped == []


def test_whole_plan_expansion_drops_delivered_elsewhere_chunk_and_releases_dependent(tmp_path):
    plan = [
        _chunk("C1", "src/c1.py", disposition="delivered_elsewhere"),
        _chunk("C2", "src/c2.py", depends_on=_EDGE),
    ]
    inv = _repo(tmp_path, {"live": plan}, [("live", "—")])
    minted, _ = _mint(inv)
    assert set(minted) == {"live.C2"}
    assert "depends_on" not in minted["live.C2"]
