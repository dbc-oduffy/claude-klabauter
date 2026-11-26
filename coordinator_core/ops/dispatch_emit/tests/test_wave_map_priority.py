"""build_waves priority: a named row (and its predecessors) schedules first; priority_report names what cannot be honoured."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED, EmitterRow
from coordinator_core.ops.dispatch_emit.wave_map import build_waves, priority_report


def _row(id_, writes, depends_on=None):
    return EmitterRow(
        id=id_,
        title=f"title-{id_}",
        surface="test",
        writes=writes,
        reads=[],
        depends_on=depends_on or [],
    )


def _flat(waves):
    return [row.id for wave in waves for row in wave]


def _contending():
    return [_row(f"C{i}", ["shared.py"]) for i in range(1, 6)]


def test_default_priority_leaves_order_unchanged():
    rows = _contending()
    assert _flat(build_waves(rows)) == _flat(build_waves(rows, priority=()))
    assert _flat(build_waves(rows)) == ["C1", "C2", "C3", "C4", "C5"]


def test_priority_row_takes_the_first_wave_over_write_contenders():
    waves = build_waves(_contending(), priority=["C4"])
    assert waves[0][0].id == "C4"
    assert _flat(waves) == ["C4", "C1", "C2", "C3", "C5"]


def test_priority_order_is_given_order_and_rest_keep_input_order():
    assert _flat(build_waves(_contending(), priority=["C5", "C3"])) == [
        "C5", "C3", "C1", "C2", "C4",
    ]


def test_priority_pulls_transitive_predecessors_first_and_edges_hold():
    rows = [
        _row("C1", ["a.py"]),
        _row("C2", ["b.py"]),
        _row("C3", ["c.py"], depends_on=[{"chunk": "C2"}]),
    ]
    waves = build_waves(rows, priority=["C3"])
    order = _flat(waves)
    assert order.index("C2") < order.index("C3")
    wave_of = {row.id: i for i, wave in enumerate(waves) for row in wave}
    assert wave_of["C2"] < wave_of["C3"]


def test_unknown_priority_id_is_ignored_by_build_waves():
    assert _flat(build_waves(_contending(), priority=["ZZ"])) == [
        "C1", "C2", "C3", "C4", "C5",
    ]


def test_priority_report_applied_and_unhonoured():
    rows = _contending() + [
        _row("H", UNDECLARED, depends_on=[{"chunk": "C1", "gate_kind": "epistemic-premise"}])
    ]
    applied, unhonoured = priority_report(rows, ["C4", "ZZ", "H", "C4"])
    assert applied == ["C4"]
    by_id = {u["id"]: u["reason"] for u in unhonoured}
    assert set(by_id) == {"ZZ", "H"}
    assert "not in this pass" in by_id["ZZ"]
    assert by_id["H"].startswith("held out:")
    assert _flat(build_waves(rows, priority=["H"])) == ["C1", "C2", "C3", "C4", "C5"]
