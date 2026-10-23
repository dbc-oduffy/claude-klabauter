from coordinator_core.ops.dispatch_emit.ledger_prerule import fully_decided, prerule

ITEMS = [
    {"id": "R1", "paths": ["a.py"]},
    {"id": "R2", "paths": ["b.py"]},
    {"id": "R3", "paths": ["c.py", "d.py"]},
    {"id": "R4", "paths": []},
]


def _run(**kw):
    args = dict(
        items=ITEMS,
        landed={"c.py": "abc1234"},
        withheld_rows=[{"id": "C9", "register_rows": ["R1"]}],
        register_status={"R2": "deferred", "R4": "waived", "R3": "open"},
    )
    args.update(kw)
    return {r["id"]: r for r in prerule(**args)}


def test_withheld_row():
    r = _run()["R1"]
    assert r["withheld_by"] == "C9"
    assert r["remaining"] == ["a.py"]
    assert "status" not in r


def test_deferred_status_copied():
    r = _run()
    assert r["R2"]["status"] == "deferred"
    assert r["R4"]["status"] == "waived"


def test_open_row_with_unlanded_path():
    r = _run()["R3"]
    assert r["remaining"] == ["d.py"]
    assert r["met_by"] == ["abc1234"]
    assert "status" not in r and "withheld_by" not in r


def test_never_sets_met():
    assert all(r.get("status") != "met" for r in prerule(ITEMS, {"a.py": "x"}, [], {"R1": "met"}))


def test_fully_decided_only_when_all_deferred_or_waived():
    assert not fully_decided(list(_run().values()))
    out = prerule(ITEMS[1:2] + ITEMS[3:], {}, [], {"R2": "deferred", "R4": "waived"})
    assert fully_decided(out)
    assert not fully_decided([])
