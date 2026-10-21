"""The emission receipt carries the script's landing markers and a landed stamp."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import emission_receipt as er
from coordinator_core.ops.dispatch_emit.ask_contract import ASK_MANIFEST_MARKER
from coordinator_core.ops.dispatch_emit.commit_request import MARKER_PREFIX


def _script(tmp_path, body):
    p = tmp_path / "run-t2-p2.workflow.mjs"
    p.write_text(body, encoding="utf-8", newline="\n")
    return p


def test_writer_records_both_marker_kinds_in_order(tmp_path):
    a = MARKER_PREFIX + '{"x":1}'
    b = ASK_MANIFEST_MARKER + '{"y":2}'
    s = _script(tmp_path, f"// head\n{a}\ncode();\n{b}\n")
    assert er._write_emission_receipt(s, None, {}) is not None
    data = json.loads(er.emission_receipt_path(s).read_text(encoding="utf-8"))
    assert data["landing_markers"] == [a, b]
    assert er.receipt_landing_text(s) == a + "\n" + b


def test_markerless_script_gets_empty_list_and_no_text(tmp_path):
    s = _script(tmp_path, "code();\n")
    er._write_emission_receipt(s, None, {})
    data = json.loads(er.emission_receipt_path(s).read_text(encoding="utf-8"))
    assert data["landing_markers"] == []
    assert er.receipt_landing_text(s) is None


def test_reader_none_when_receipt_absent_or_unreadable(tmp_path):
    s = _script(tmp_path, "x\n")
    assert er.receipt_landing_text(s) is None
    er.emission_receipt_path(s).write_text("{not json", encoding="utf-8")
    assert er.receipt_landing_text(s) is None
    assert er.receipt_unlanded(s) is False


def test_extras_cannot_shadow_markers_or_landed(tmp_path):
    a = MARKER_PREFIX + "{}"
    s = _script(tmp_path, a + "\n")
    er._write_emission_receipt(
        s, None, {}, extras={"landing_markers": ["forged"], "landed": {"sha": "z"}}
    )
    data = json.loads(er.emission_receipt_path(s).read_text(encoding="utf-8"))
    assert data["landing_markers"] == [a]
    assert "landed" not in data


def test_unlanded_then_stamp_landed_keeps_other_keys(tmp_path):
    s = _script(tmp_path, MARKER_PREFIX + "{}\n")
    er._write_emission_receipt(s, None, {})
    assert er.receipt_unlanded(s) is True
    er.stamp_landed(s, "abc123")
    data = json.loads(er.emission_receipt_path(s).read_text(encoding="utf-8"))
    assert data["landed"]["sha"] == "abc123" and data["landed"]["at"]
    assert data["landing_markers"] and data["sha256"]
    assert er.receipt_unlanded(s) is False


def test_stamp_landed_without_receipt_is_a_noop(tmp_path):
    s = _script(tmp_path, "x\n")
    er.stamp_landed(s, "abc")
    assert not er.emission_receipt_path(s).exists()


def test_reemit_drops_prior_landed_stamp(tmp_path):
    s = _script(tmp_path, MARKER_PREFIX + "{}\n")
    er._write_emission_receipt(s, None, {})
    er.stamp_landed(s, "abc")
    er._write_emission_receipt(s, None, {})
    assert er.receipt_unlanded(s) is True
