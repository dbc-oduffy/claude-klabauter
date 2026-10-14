"""Pins the intake-sizing contract (sizing_intake_contract).

Run: python -m pytest coordinator_core/ops/tests/test_sizing_intake_contract.py -q
"""

from __future__ import annotations

import copy

import pytest

from coordinator_core.frontmatter import schema_validate
from coordinator_core.ops import sizing_intake_contract as c

_SCHEMA = (schema_validate._SCHEMAS_DIR / "sizing-object.schema.json")
_TURN = {"turn": 4, "ts": "2026-10-14T10:00:00Z", "text": "ship it"}


def _rec(**kw):
    return c.intake_record(name="x", turn=_TURN, today="2026-10-14", **kw)


def test_intake_record_validates():
    assert schema_validate.validate_frontmatter(_rec(), _SCHEMA) == []


def test_intake_record_seeds_exactly_one_verbatim():
    rec = _rec()
    assert rec["pm_verbatims"] == [_TURN]
    assert rec["intent"] == "ship it"
    assert rec["intent_source"] == "pm-verbatim"
    assert rec["status"] == "draft"


def test_intake_record_accepts_log_prompt_key():
    rec = c.intake_record(name="x", turn={"turn": 0, "prompt": "hi"}, today="d")
    assert rec["pm_verbatims"] == [{"turn": 0, "text": "hi"}]


def test_intake_record_refuses_blank_turn():
    with pytest.raises(ValueError):
        c.intake_record(name="x", turn={"text": "  "}, today="d")


def test_intake_record_clamps_name_to_schema_max():
    rec = c.intake_record(name="n" * 100, turn=_TURN, today="d")
    assert len(rec["name"]) == 60
    assert schema_validate.validate_frontmatter(rec, _SCHEMA) == []


@pytest.mark.parametrize("value", [None, "", "   ", 5])
def test_read_em_brief_absent_or_blank(value):
    doc = _rec()
    if value is not None:
        doc["em_analysis"][c.EM_BRIEF_KEY] = value
    assert c.read_em_brief(doc) is None
    assert c.read_em_brief({}) is None


def test_read_em_brief_set_is_stripped():
    doc = _rec()
    doc["em_analysis"][c.EM_BRIEF_KEY] = "  do X first \n"
    assert c.read_em_brief(doc) == "do X first"
    assert schema_validate.validate_frontmatter(doc, _SCHEMA) == []


def test_is_intake_true_only_for_the_mints_shape():
    doc = _rec()
    assert c.is_intake(doc)
    for mutate in (
        lambda d: d.pop("em_analysis"),
        lambda d: d["em_analysis"].update(intake="other"),
        lambda d: d.update(status="routed"),
    ):
        other = copy.deepcopy(doc)
        mutate(other)
        assert not c.is_intake(other)
    assert not c.is_intake({"route": "plan", "status": "draft"})
