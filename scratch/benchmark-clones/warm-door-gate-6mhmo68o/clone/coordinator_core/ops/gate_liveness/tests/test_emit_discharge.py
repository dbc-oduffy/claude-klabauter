"""
Tier T tests for coordinator_core.ops.gate_liveness.emit_discharge.

Spec backlink: docs/plans/2026-08-21-a-discharged-gate-tells-the-row-waiting.md § C4
"""
from __future__ import annotations

import pytest

from coordinator_core.ops.gate_liveness.emit_discharge import (
    CLOSURE_KEY_KINDS,
    emit_discharge,
    render_discharges_block,
    validate_closure_key,
    validate_discharge_evidence,
    validate_discharges_block,
    validate_landed_at,
)

_VALID_CLOSURE_KEY = {"kind": "memo-thread", "id": "2026-08-21-some-memo.md"}


class TestValidateClosureKey:
    def test_valid_memo_thread(self):
        assert validate_closure_key({"kind": "memo-thread", "id": "x"}) is None

    def test_valid_deliverable(self):
        assert validate_closure_key({"kind": "deliverable", "id": "dlv-abc"}) is None

    def test_rejects_non_mapping(self):
        error = validate_closure_key("not-a-dict")
        assert error is not None
        assert "mapping" in error

    def test_rejects_unknown_subkey(self):
        error = validate_closure_key({"kind": "deliverable", "id": "x", "extra": "y"})
        assert error is not None
        assert "unrecognized" in error

    def test_rejects_bad_kind(self):
        error = validate_closure_key({"kind": "commit", "id": "x"})
        assert error is not None
        assert "kind" in error

    def test_rejects_missing_id(self):
        error = validate_closure_key({"kind": "deliverable", "id": ""})
        assert error is not None
        assert "id" in error

    def test_closed_enum_is_exactly_two_members(self):
        assert CLOSURE_KEY_KINDS == ("deliverable", "memo-thread")


class TestValidateDischargeEvidence:
    def test_accepts_sentinel_inline(self):
        assert validate_discharge_evidence("inline") is None

    def test_accepts_path(self):
        assert validate_discharge_evidence("docs/plans/2026-08-21-foo.md") is None

    def test_accepts_hex_sha(self):
        assert validate_discharge_evidence("72f56b1f") is None
        assert validate_discharge_evidence("a" * 40) is None

    def test_rejects_bare_word(self):
        error = validate_discharge_evidence("done")
        assert error is not None

    def test_rejects_empty(self):
        error = validate_discharge_evidence("")
        assert error is not None

    def test_rejects_non_string(self):
        error = validate_discharge_evidence(12345)
        assert error is not None
        assert "string" in error


class TestValidateLandedAt:
    def test_accepts_date(self):
        assert validate_landed_at("2026-08-21") is None

    def test_rejects_non_date_string(self):
        assert validate_landed_at("2026/08/21") is not None

    def test_rejects_non_string(self):
        assert validate_landed_at(20260821) is not None

    def test_rejects_empty(self):
        assert validate_landed_at("") is not None


class TestValidateDischargesBlock:
    def test_all_valid_returns_empty(self):
        assert validate_discharges_block(_VALID_CLOSURE_KEY, "inline", "2026-08-21") == []

    def test_all_invalid_reports_every_field(self):
        errors = validate_discharges_block("bad", "bad", "bad")
        assert len(errors) == 3


class TestRenderDischargesBlock:
    def test_renders_nested_mapping(self):
        fragment = render_discharges_block(_VALID_CLOSURE_KEY, "inline", "2026-08-21")
        assert fragment.startswith("discharges:")
        assert "closure_key:" in fragment
        assert "kind: \"memo-thread\"" in fragment
        assert "id: \"2026-08-21-some-memo.md\"" in fragment
        assert "evidence: \"inline\"" in fragment
        assert "landed_at: \"2026-08-21\"" in fragment


class TestEmitDischarge:
    _BASE_KWARGS = dict(
        from_id="repos.claude_klabauter",
        to="repos.doe_claude",
        topic="gate-closure-demo",
        title="Gate closure demo",
        body="The blocker landed.",
        today="2026-08-21",
    )

    def test_composes_schema_valid_memo_with_discharges_block(self):
        composed = emit_discharge(
            closure_key=_VALID_CLOSURE_KEY,
            evidence="inline",
            landed_at="2026-08-21",
            **self._BASE_KWARGS,
        )
        assert composed.startswith("---\n")
        assert "title: \"Gate closure demo\"" in composed
        assert "status: open" in composed
        assert "delivery_mode: receiver-repo" in composed
        assert "discharges:" in composed
        assert "The blocker landed." in composed
        # discharges block sits inside the frontmatter, before the closing marker
        front, _, rest = composed.partition("\n---\n")
        assert "discharges:" in front
        assert "discharges:" not in rest

    def test_no_kind_discharge_enum_member_emitted(self):
        composed = emit_discharge(
            closure_key=_VALID_CLOSURE_KEY,
            evidence="inline",
            landed_at="2026-08-21",
            **self._BASE_KWARGS,
        )
        assert 'kind: "discharge"' not in composed

    def test_invalid_closure_key_raises_value_error(self):
        with pytest.raises(ValueError, match="closure_key"):
            emit_discharge(
                closure_key={"kind": "commit", "id": "x"},
                evidence="inline",
                landed_at="2026-08-21",
                **self._BASE_KWARGS,
            )

    def test_invalid_evidence_raises_value_error(self):
        with pytest.raises(ValueError, match="evidence"):
            emit_discharge(
                closure_key=_VALID_CLOSURE_KEY,
                evidence="a-bare-word",
                landed_at="2026-08-21",
                **self._BASE_KWARGS,
            )

    def test_invalid_landed_at_raises_value_error(self):
        with pytest.raises(ValueError, match="landed_at"):
            emit_discharge(
                closure_key=_VALID_CLOSURE_KEY,
                evidence="inline",
                landed_at="not-a-date",
                **self._BASE_KWARGS,
            )

    def test_never_writes_or_sends(self, tmp_path, monkeypatch):
        # No file I/O of any kind — this module is a pure composer. Assert
        # the tmp_path directory stays empty across a call, as a cheap
        # negative-spec check on the "does not write a file" claim.
        monkeypatch.chdir(tmp_path)
        emit_discharge(
            closure_key=_VALID_CLOSURE_KEY,
            evidence="inline",
            landed_at="2026-08-21",
            **self._BASE_KWARGS,
        )
        assert list(tmp_path.iterdir()) == []
