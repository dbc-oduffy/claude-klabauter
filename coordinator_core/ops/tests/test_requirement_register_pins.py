"""Tests for the register-row source_text hash pin functions."""
from __future__ import annotations

import re

import yaml

from coordinator_core.ops import requirement_register as rr


def _reg(*texts):
    rows = [{"id": f"R{i}", "source_text": t} for i, t in enumerate(texts, 1)]
    return rr.Register(rows=rows)


def _pins(reg):
    return rr.register_pins(reg, [r["id"] for r in reg.rows])


def test_pin_shape():
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", rr.row_pin("x"))


def test_empty_pins_no_drift():
    assert rr.pin_drift(_reg("a"), {}) == []


def test_matching_no_drift():
    reg = _reg("a b", "c")
    assert rr.pin_drift(reg, _pins(reg)) == []


def test_whitespace_edit_is_not_drift():
    pins = _pins(_reg("a  b\n c"))
    assert rr.pin_drift(_reg("a b c"), {"R1": pins["R1"]}) == []


def test_word_edit_drifts():
    pinned = _pins(_reg("a b"))
    out = rr.pin_drift(_reg("a c"), pinned)
    assert out == [{"id": "R1", "pinned": pinned["R1"], "current": rr.row_pin("a c")}]


def test_removed_row_drifts_with_current_none():
    pinned = _pins(_reg("a"))
    out = rr.pin_drift(rr.Register(rows=[]), pinned)
    assert out == [{"id": "R1", "pinned": pinned["R1"], "current": None}]


def test_malformed_pin_is_drift_and_carried_through():
    out = rr.pin_drift(_reg("a"), {"R1": "sha256:XYZ"})
    assert out[0]["pinned"] == "sha256:XYZ" and out[0]["current"] == rr.row_pin("a")


def test_unpinned_rows_ignored():
    reg = _reg("a", "b")
    assert rr.pin_drift(_reg("a", "changed"), {"R1": _pins(reg)["R1"]}) == []


def test_register_pins_skips_absent_ids():
    assert list(rr.register_pins(_reg("a"), ["R1", "R9"])) == ["R1"]


def test_plan_pins_absent_malformed_valid():
    assert rr.plan_pins("title: x\n") == {}
    assert rr.plan_pins("register_claims: {pins: [1]}\n") == {}
    assert rr.plan_pins(": : [") == {}
    pin = rr.row_pin("a")
    assert rr.plan_pins(f"register_claims:\n  sizing: s\n  pins:\n    R1: '{pin}'\n") == {"R1": pin}


def _write_sizing(tmp_path, text):
    p = tmp_path / "state" / "sizings" / "s.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(
        {"requirement_register": {"sources": [{"kind": "pm"}],
                                  "rows": [{"id": "R1", "source_text": text}]}}), encoding="utf-8")


def _fm(pin):
    return f"register_claims:\n  sizing: state/sizings/s.yaml\n  rows: [R1]\n  pins:\n    R1: '{pin}'\n"


def test_plan_pin_drift_missing_sizing_returns_empty(tmp_path):
    assert rr.plan_pin_drift(_fm(rr.row_pin("a")), tmp_path) == []


def test_plan_pin_drift_detects_edit(tmp_path):
    _write_sizing(tmp_path, "a")
    pin = rr.row_pin("a")
    assert rr.plan_pin_drift(_fm(pin), tmp_path) == []
    _write_sizing(tmp_path, "b")
    assert [d["id"] for d in rr.plan_pin_drift(_fm(pin), tmp_path)] == ["R1"]


def test_plan_pin_drift_no_pins_or_escaping_sizing(tmp_path):
    assert rr.plan_pin_drift("register_claims:\n  sizing: s\n  rows: [R1]\n", tmp_path) == []
    fm = f"register_claims:\n  sizing: ../../etc/x\n  pins:\n    R1: '{rr.row_pin('a')}'\n"
    assert rr.plan_pin_drift(fm, tmp_path) == []
