"""evidence-record schema accepts a well-formed record and refuses malformed ones."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import jsonschema
import pytest

_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "evidence-record.schema.json").read_text(encoding="utf-8")
)

_VALID = {
    "schema_version": "evidence-record/v1",
    "id": "ev-0001",
    "claim_id": "claim-7",
    "stance": "supports",
    "source_kind": "file",
    "source_ref": "src/main.c",
    "locator": "L10-L20",
    "excerpt": "int main(void)",
    "observed_at": "2026-11-02T00:00:00Z",
    "observer": "session-1",
}


def _errors(record: dict) -> list:
    return list(jsonschema.Draft202012Validator(_SCHEMA).iter_errors(record))


def test_schema_is_itself_valid():
    jsonschema.Draft202012Validator.check_schema(_SCHEMA)


def test_well_formed_record_validates():
    assert _errors(_VALID) == []


def test_explicit_nulls_validate():
    rec = {**_VALID, "locator": None, "excerpt": None}
    assert _errors(rec) == []


@pytest.mark.parametrize("field", sorted(_VALID))
def test_missing_required_field_is_refused(field):
    rec = copy.deepcopy(_VALID)
    del rec[field]
    assert _errors(rec)


def test_unknown_key_is_refused():
    assert _errors({**_VALID, "extra": 1})


def test_wrong_schema_version_is_refused():
    assert _errors({**_VALID, "schema_version": "evidence-record/v2"})


@pytest.mark.parametrize("bad_id", ["", "a/b", "../x", "a\\b", ".hidden"])
def test_id_unusable_as_filename_is_refused(bad_id):
    assert _errors({**_VALID, "id": bad_id})


def test_unknown_enum_value_is_refused():
    assert _errors({**_VALID, "stance": "maybe"})
    assert _errors({**_VALID, "source_kind": "telepathy"})
