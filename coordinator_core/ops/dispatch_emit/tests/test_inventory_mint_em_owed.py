"""mint_spine's tranche report names the EM-owed holds (em-performed / external_gate) and the
plans each one stalls; an ordinary uncleared predecessor is never listed."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import inventory_mint as im
from coordinator_core.ops.dispatch_emit.tests.test_inventory_tranche_and_withheld import (
    _chunk,
    _n_rows,
    _repo,
)


def _inventory(tmp_path):
    em_row = _chunk("A1", "docs/a1.md", performer="em", change_kind="doc-edit")
    gate_row = _chunk("B1", "src/b1.py", external_gate=[{"id": "owner-lands-first"}])
    ordinary = _chunk(
        "D1",
        "src/d1.py",
        depends_on_plan=[{"plan": "docs/plans/pred.md", "chunk": "P1", "gate_kind": "output-consumption-runtime"}],
    )
    return _repo(
        tmp_path,
        {
            "pred": [_chunk("P1", "src/p1.py")],
            "a": [em_row],
            "b": [gate_row],
            "c": _n_rows("C", 1),
            "d": [ordinary],
            "e": _n_rows("E", 1),
            "free": _n_rows("F", 2),
            "g": _n_rows("G", 1),
        },
        [
            ("free", "—"),
            ("a", "—"),
            ("b", "—"),
            ("c", "a"),
            ("g", "c"),
            ("d", "—"),
            ("e", "d"),
        ],
    )


def test_tranche_report_names_em_owed_holds_and_their_stalled_dependents(tmp_path):
    report: dict = {}
    im.mint_spine(str(_inventory(tmp_path)), row_budget=10, tranche_out=report)

    owed = {entry["plan"]: entry for entry in report["em_owed"]}
    assert set(owed) == {"a", "b"}
    assert owed["a"]["holds"] == [{"row": "A1", "reason": "em-performed"}]
    assert owed["b"]["holds"] == [{"row": "B1", "reason": "external_gate"}]
    assert owed["a"]["stalls"] == ["c", "g"]
    assert owed["b"]["stalls"] == []


def test_ordinary_predecessor_hold_and_its_dependents_stay_out_of_the_list(tmp_path):
    report: dict = {}
    im.mint_spine(str(_inventory(tmp_path)), row_budget=10, tranche_out=report, withheld_out={})

    listed = {e["plan"] for e in report["em_owed"]} | {p for e in report["em_owed"] for p in e["stalls"]}
    assert not listed & {"d", "e"}
    waiting = {e["plan"] for e in report["remaining"]["waiting_on_predecessors"]}
    assert {"d", "e", "a", "b", "c", "g"} <= waiting
    assert report["plans"] == ["free"]


def test_no_em_owed_key_when_nothing_is_em_owed(tmp_path):
    inv = _repo(tmp_path, {"free": _n_rows("F", 2), "g": _n_rows("G", 1)}, [("free", "—"), ("g", "—")])
    report: dict = {}
    im.mint_spine(str(inv), row_budget=2, tranche_out=report)
    assert "em_owed" not in report
