"""plan-tasks row `authorization`: optional, em-inferred record validates, unknown source rejected."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.frontmatter import schema_validate as sv

_SCHEMA = Path(sv.__file__).parent / "schemas" / "plan-tasks.schema.json"

_EM_INFERRED = {"source": "em-inferred", "rationale": "PM would want this", "on": "2026-10-14"}


def _row(**extra) -> dict:
    row = {"id": "C1", "title": "t", "surface": "s", "change_kind": "doctrine-edit", "writes": ["a.py"]}
    row.update(extra)
    return row


def _errors(row: dict) -> list:
    return sv.validate_frontmatter(row, _SCHEMA)


def test_row_without_authorization_validates():
    assert _errors(_row()) == []


def test_em_inferred_record_validates():
    assert _errors(_row(authorization=dict(_EM_INFERRED))) == []


def test_unknown_source_is_rejected():
    assert _errors(_row(authorization={**_EM_INFERRED, "source": "guessed"}))


@pytest.mark.parametrize("missing", ["source", "rationale", "on"])
def test_em_inferred_requires_each_key(missing):
    rec = {k: v for k, v in _EM_INFERRED.items() if k != missing}
    assert _errors(_row(authorization=rec))


def test_em_inferred_rejects_extra_key():
    assert _errors(_row(authorization={**_EM_INFERRED, "approver": "x"}))


def test_empty_rationale_is_rejected():
    assert _errors(_row(authorization={**_EM_INFERRED, "rationale": ""}))


def test_bare_scalar_is_rejected():
    assert _errors(_row(authorization="em-inferred"))


def test_signoff_record_still_validates_as_authorization():
    rec = {"source": "pm", "pm_quote": "do it", "on": "2026-10-14"}
    assert _errors(_row(authorization=rec)) == []
