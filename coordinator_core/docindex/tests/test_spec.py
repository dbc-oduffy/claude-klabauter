from __future__ import annotations

import datetime

import pytest

from coordinator_core.docindex.spec import (
    EntryField,
    IndexSpecError,
    coerce_to_string,
    parse_index_spec,
)


def test_parses_full_spec():
    fm_text = """
index_source_dir: docs/architecture/systems/
entry_kind: wiki-entry
entry_fields:
  - field: system
    label: System
  - field: depends_on
    label: Dependencies
index_exclude_when:
  status: retired
"""
    spec = parse_index_spec(fm_text)
    assert spec.index_source_dir == "docs/architecture/systems/"
    assert spec.entry_kind == "wiki-entry"
    assert spec.entry_fields == (
        EntryField(field="system", label="System"),
        EntryField(field="depends_on", label="Dependencies"),
    )
    assert spec.index_exclude_when == {"status": "retired"}


def test_index_exclude_when_optional():
    fm_text = """
index_source_dir: docs/plugins/
entry_kind: plugin-manifest
entry_fields:
  - field: name
    label: Name
"""
    spec = parse_index_spec(fm_text)
    assert spec.index_exclude_when is None


def test_missing_index_source_dir_raises_named_error():
    fm_text = """
entry_kind: wiki-entry
entry_fields:
  - field: system
    label: System
"""
    with pytest.raises(IndexSpecError, match="index_source_dir"):
        parse_index_spec(fm_text)


def test_missing_entry_kind_raises_named_error():
    fm_text = """
index_source_dir: docs/x/
entry_fields:
  - field: system
    label: System
"""
    with pytest.raises(IndexSpecError, match="entry_kind"):
        parse_index_spec(fm_text)


def test_missing_entry_fields_raises_named_error():
    fm_text = """
index_source_dir: docs/x/
entry_kind: wiki-entry
"""
    with pytest.raises(IndexSpecError, match="entry_fields"):
        parse_index_spec(fm_text)


def test_empty_entry_fields_raises_named_error():
    fm_text = """
index_source_dir: docs/x/
entry_kind: wiki-entry
entry_fields: []
"""
    with pytest.raises(IndexSpecError, match="entry_fields"):
        parse_index_spec(fm_text)


def test_entry_field_missing_label_raises():
    fm_text = """
index_source_dir: docs/x/
entry_kind: wiki-entry
entry_fields:
  - field: system
"""
    with pytest.raises(IndexSpecError, match="entry_fields\\[0\\]"):
        parse_index_spec(fm_text)


def test_malformed_index_exclude_when_raises():
    fm_text = """
index_source_dir: docs/x/
entry_kind: wiki-entry
entry_fields:
  - field: system
    label: System
index_exclude_when: not-a-mapping
"""
    with pytest.raises(IndexSpecError, match="index_exclude_when"):
        parse_index_spec(fm_text)


def test_entry_fields_order_preserved():
    fm_text = """
index_source_dir: docs/x/
entry_kind: wiki-entry
entry_fields:
  - field: c
    label: C
  - field: a
    label: A
  - field: b
    label: B
"""
    spec = parse_index_spec(fm_text)
    assert [ef.field for ef in spec.entry_fields] == ["c", "a", "b"]


def test_coerce_to_string_bool():
    assert coerce_to_string(True) == "true"
    assert coerce_to_string(False) == "false"


def test_coerce_to_string_date_never_equals_bool_string():
    d = datetime.date(2026, 8, 6)
    assert coerce_to_string(d) == "2026-08-06"
    assert coerce_to_string(d) != coerce_to_string(True)


def test_coerce_to_string_plain_string():
    assert coerce_to_string("retired") == "retired"


def test_coerce_to_string_int():
    assert coerce_to_string(41403) == "41403"
