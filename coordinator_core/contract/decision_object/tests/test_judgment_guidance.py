"""Tests for `build_disposition`'s optional `guidance` parameter.

Covers AC1 (omitted by default, emitted when supplied -- and every
existing caller's output stays byte-identical), AC2 (fail-loud `ValueError`
on a non-str, non-None value).

Spec backlink: docs/plans/2026-08-13-build-disposition-per-option-guidance.md
"""

from __future__ import annotations

import pytest

from coordinator_core.contract.decision_object.judgment import build_disposition


class TestGuidanceOmittedByDefault:
    def test_no_guidance_kwarg_omits_the_key_entirely(self):
        disposition = build_disposition("accept", ["d1"])
        assert disposition == {"value": "accept", "resolves": ["d1"]}
        assert "guidance" not in disposition

    def test_explicit_none_also_omits_the_key(self):
        disposition = build_disposition("accept", ["d1"], guidance=None)
        assert "guidance" not in disposition

    def test_default_resolves_still_empty_list(self):
        disposition = build_disposition("accept")
        assert disposition == {"value": "accept", "resolves": []}


class TestGuidanceEmittedWhenSupplied:
    def test_guidance_string_carried_on_the_disposition(self):
        disposition = build_disposition(
            "accept", ["d1"], guidance="Accept and action now."
        )
        assert disposition["guidance"] == "Accept and action now."

    def test_guidance_co_located_alongside_value_and_resolves(self):
        disposition = build_disposition("accept", ["d1"], guidance="text")
        assert set(disposition) == {"value", "resolves", "guidance"}


class TestGuidanceRejectsBadType:
    def test_int_guidance_raises_value_error(self):
        with pytest.raises(ValueError, match="guidance"):
            build_disposition("accept", ["d1"], guidance=123)

    def test_list_guidance_raises_value_error(self):
        with pytest.raises(ValueError, match="guidance"):
            build_disposition("accept", ["d1"], guidance=["not", "a", "string"])

    def test_dict_guidance_raises_value_error(self):
        with pytest.raises(ValueError, match="guidance"):
            build_disposition("accept", ["d1"], guidance={"not": "a string"})


class TestGuidanceRejectsVacuousString:
    def test_empty_string_guidance_raises_value_error(self):
        with pytest.raises(ValueError, match="guidance"):
            build_disposition("accept", ["d1"], guidance="")

    def test_whitespace_only_guidance_raises_value_error(self):
        with pytest.raises(ValueError, match="guidance"):
            build_disposition("accept", ["d1"], guidance="   \t\n")
