"""Pins the ghidra-program-facts/v1 schema against the sample export fixture."""
import copy
import json
from pathlib import Path

import jsonschema
import pytest

HERE = Path(__file__).resolve().parent
SCHEMA = json.loads((HERE.parent.parent / "contract" / "ghidra-program-facts.schema.json").read_text(encoding="utf-8"))
SAMPLE = json.loads((HERE / "fixtures" / "sample-export.json").read_text(encoding="utf-8"))

SORT_KEYS = {
    "functions": ("entry",),
    "xrefs": ("from", "to", "ref_type"),
    "imports": ("library", "symbol", "address"),
    "strings": ("address", "encoding"),
}


def _valid(doc):
    jsonschema.Draft202012Validator(SCHEMA).validate(doc)


def _invalid(doc):
    with pytest.raises(jsonschema.ValidationError):
        _valid(doc)


def test_schema_is_itself_valid():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)


def test_fixture_validates():
    _valid(SAMPLE)


def test_fixture_covers_every_array_and_is_sorted():
    for name, keys in SORT_KEYS.items():
        rows = SAMPLE[name]
        assert rows, name
        assert rows == sorted(rows, key=lambda r: tuple(r[k] for k in keys)), name


def test_additional_properties_refused_at_every_level():
    for path in (None, "program", "functions", "xrefs", "imports", "strings"):
        doc = copy.deepcopy(SAMPLE)
        target = doc if path is None else (doc[path] if path == "program" else doc[path][0])
        target["extra"] = 1
        _invalid(doc)


def test_address_must_be_lowercase_hex():
    for bad in ("0X1000", "0xABCD", "4096", "0x"):
        doc = copy.deepcopy(SAMPLE)
        doc["functions"][0]["entry"] = bad
        _invalid(doc)


def test_wrong_schema_version_refused():
    doc = copy.deepcopy(SAMPLE)
    doc["schema_version"] = "ghidra-program-facts/v2"
    _invalid(doc)


def test_missing_required_field_refused():
    doc = copy.deepcopy(SAMPLE)
    del doc["program"]["sha256"]
    _invalid(doc)
