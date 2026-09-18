"""
Tests for ``coordinator_core.ops.dispatch_emit.op :: restamp`` -- the
engine-owned re-stamp function the published ``--restamp <script>`` surface
documents.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § S1-C6.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from coordinator_core.ops.dispatch_emit.op import (
    ForeignSessionRestampError,
    NoReceiptToRestampError,
    emission_receipt_path,
    restamp,
)

_RECEIPT_KEYS = {"sha256", "session_id", "emitted_at", "plan"}


def _write_script_and_receipt(tmp_path, *, session_id, body=b"// original"):
    script_path = tmp_path / "emitted.mjs"
    script_path.write_bytes(body)
    receipt_path = emission_receipt_path(script_path)
    receipt = {
        "sha256": hashlib.sha256(body).hexdigest(),
        "session_id": session_id,
        "emitted_at": "2026-09-16T09:00:00",
        "plan": "some-plan.md",
    }
    receipt_path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return script_path, receipt_path


def test_round_trip_restamps_sha256_over_edited_bytes(tmp_path):
    script_path, receipt_path = _write_script_and_receipt(tmp_path, session_id="sess-ours")
    script_path.write_bytes(b"// edited after emission")

    result = restamp(script_path, "sess-ours")

    on_disk = json.loads(receipt_path.read_text(encoding="utf-8"))
    expected_sha = hashlib.sha256(b"// edited after emission").hexdigest()

    assert result == on_disk
    assert set(on_disk) == _RECEIPT_KEYS
    assert on_disk["sha256"] == expected_sha
    assert on_disk["session_id"] == "sess-ours"
    assert on_disk["plan"] == "some-plan.md"
    assert on_disk["emitted_at"] == "2026-09-16T09:00:00"


def test_round_trip_output_is_accepted_by_test_op_emission_receipts_reader_shape(tmp_path):
    """Same shape ``test_op_emission_receipt.py`` reads: sorted keys, two-space
    indent, one trailing newline, sha256 matching the bytes on disk."""
    script_path, receipt_path = _write_script_and_receipt(tmp_path, session_id="sess-ours")
    script_path.write_bytes(b"// edited")

    restamp(script_path, "sess-ours")

    raw = receipt_path.read_text(encoding="utf-8")
    assert raw.endswith("}\n")
    parsed = json.loads(raw)
    assert raw == json.dumps(parsed, indent=2, sort_keys=True) + "\n"
    assert parsed["sha256"] == hashlib.sha256(script_path.read_bytes()).hexdigest()


def test_refuses_a_receipt_naming_a_foreign_session(tmp_path):
    script_path, receipt_path = _write_script_and_receipt(tmp_path, session_id="sess-peer")
    before = receipt_path.read_text(encoding="utf-8")

    with pytest.raises(ForeignSessionRestampError):
        restamp(script_path, "sess-ours")

    assert receipt_path.read_text(encoding="utf-8") == before


def test_refuses_when_session_id_is_empty(tmp_path):
    script_path, receipt_path = _write_script_and_receipt(tmp_path, session_id="sess-peer")
    before = receipt_path.read_text(encoding="utf-8")

    with pytest.raises(ForeignSessionRestampError):
        restamp(script_path, "")

    assert receipt_path.read_text(encoding="utf-8") == before


def test_refuses_when_no_receipt_exists(tmp_path):
    script_path = tmp_path / "emitted.mjs"
    script_path.write_bytes(b"// no receipt beside this one")

    with pytest.raises(NoReceiptToRestampError):
        restamp(script_path, "sess-ours")


def test_refuses_when_script_does_not_exist(tmp_path):
    script_path = tmp_path / "missing.mjs"

    with pytest.raises(NoReceiptToRestampError):
        restamp(script_path, "sess-ours")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
