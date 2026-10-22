"""Contract pins: LedgerHalt.ruled, ManifestRow.register_rows, and the footprint constants."""

from coordinator_core.ops.dispatch_emit import ask_contract as ac


def _halt(**kw):
    return ac.LedgerHalt(run_id="r", plan_rel="docs/plans/p.md", sizing_rel="s.yaml", blocking=("a",), **kw)


def _row(**kw):
    return ac.ManifestRow(id="C1", agent_type="x", model="m", brief_path="b", writes=("w",), wave=0, **kw)


def test_constants():
    assert ac.CLAIM_PARAM == "claim"
    assert ac.OUTSIDE_ROW_FOOTPRINT == "outside-row-footprint"
    assert ac.LEG_FOOTPRINT_FILE


def test_halt_ruled_omitted_when_empty():
    j = _halt().to_json()
    assert "ruled" not in j
    assert ac.LedgerHalt.from_json(j).ruled == ()


def test_halt_ruled_round_trip():
    h = _halt(ruled=("R3", "R4"))
    j = h.to_json()
    assert j["ruled"] == ["R3", "R4"]
    assert ac.LedgerHalt.from_json(j).ruled == ("R3", "R4")


def test_row_register_rows_omitted_when_empty():
    j = _row().to_json()
    assert "register_rows" not in j
    assert ac.ManifestRow.from_json(j).register_rows == ()


def test_row_register_rows_round_trip():
    j = _row(register_rows=("R1", "R2")).to_json()
    assert j["register_rows"] == ["R1", "R2"]
    assert ac.ManifestRow.from_json(j).register_rows == ("R1", "R2")
