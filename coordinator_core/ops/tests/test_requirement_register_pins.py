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


def _plan(claims_tail, nl="\n"):
    text = (
        "---\ntitle: t\nregister_claims:\n  sizing: state/sizings/s.yaml\n" + claims_tail
        + "status: reviewed\n---\n\n# body\npins: {not: touched}\n"
    )
    return text.replace("\n", nl)


def _repin(text, reg):
    new, changed = rr.repin_plan_text(text, reg)
    fm = new.replace("\r\n", "\n").split("---\n")[1]
    return new, changed, rr.plan_pins(fm)


def test_repin_flow_style_rewrites_only_pins_line():
    old, cur = _reg("a"), _reg("b")
    text = _plan(f'  rows: ["R1"]\n  pins: {{"R1": "{_pins(old)["R1"]}"}}\n')
    new, changed, pins = _repin(text, cur)
    assert pins == _pins(cur)
    assert changed == [{"id": "R1", "pinned": _pins(old)["R1"], "current": _pins(cur)["R1"]}]
    assert new.replace(f'  pins: {{"R1": "{_pins(cur)["R1"]}"}}\n', "") == text.replace(
        f'  pins: {{"R1": "{_pins(old)["R1"]}"}}\n', "")


def test_repin_block_style_collapses_to_flow_and_keeps_following_keys():
    old, cur = _reg("a"), _reg("b")
    text = _plan(f"  rows: [R1]\n  pins:\n    R1: '{_pins(old)['R1']}'\n")
    new, changed, pins = _repin(text, cur)
    assert pins == _pins(cur) and len(changed) == 1
    assert "    R1:" not in new and "status: reviewed" in new


def test_repin_inserts_pins_after_rows_when_absent():
    cur = _reg("b")
    text = _plan("  rows:\n    - R1\n")
    new, changed, pins = _repin(text, cur)
    assert pins == _pins(cur)
    assert changed == [{"id": "R1", "pinned": None, "current": _pins(cur)["R1"]}]
    assert new.index("- R1") < new.index("  pins:") < new.index("status:")


def test_repin_keeps_crlf_and_body_bytes():
    old, cur = _reg("a"), _reg("b")
    text = _plan(f'  rows: [R1]\n  pins: {{"R1": "{_pins(old)["R1"]}"}}\n', nl="\r\n")
    new, _, pins = _repin(text, cur)
    assert pins == _pins(cur)
    assert "\n" not in new.replace("\r\n", "")
    assert new.endswith("# body\r\npins: {not: touched}\r\n")


def test_repin_noop_returns_text_unchanged():
    reg = _reg("a")
    text = _plan(f'  rows: [R1]\n  pins: {{"R1": "{_pins(reg)["R1"]}"}}\n')
    assert rr.repin_plan_text(text, reg) == (text, [])


def test_repin_raises_naming_absent_claimed_id():
    text = _plan('  rows: [R1, R2]\n')
    try:
        rr.repin_plan_text(text, _reg("a"))
    except ValueError as e:
        assert "R2" in str(e) and "R1" not in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_remedy_names_repin_op_and_forbids_hand_edit():
    assert rr.REPIN_OP in rr.PIN_DRIFT_REMEDY and "never hand-edit" in rr.PIN_DRIFT_REMEDY
    assert rr.REGISTER_PIN_DRIFT_KIND == "register-pin-drift"


def test_plan_pin_drift_no_pins_or_escaping_sizing(tmp_path):
    assert rr.plan_pin_drift("register_claims:\n  sizing: s\n  rows: [R1]\n", tmp_path) == []
    fm = f"register_claims:\n  sizing: ../../etc/x\n  pins:\n    R1: '{rr.row_pin('a')}'\n"
    assert rr.plan_pin_drift(fm, tmp_path) == []


def test_repin_inserts_after_block_list_rows_at_key_indent():
    cur = _reg("b")
    text = _plan("  rows:\n  - R1\n")
    new, _, pins = _repin(text, cur)
    assert pins == _pins(cur)
    assert new.index("- R1") < new.index("  pins:") < new.index("status:")
