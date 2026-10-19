"""plan.schema.json 2.40.0 -- `census[]` admits the command form and the typed (`kind`) form,
and rejects a mixed or half-typed entry. Contract: coordinator_core/roadmap/census.py."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.frontmatter import schema_validate

_SCHEMA = Path(schema_validate.__file__).resolve().parent / "schemas" / "plan.schema.json"

_COMMAND = {"question": "q", "command": "ls | wc -l", "result": "3"}
_TYPED = {"question": "q", "kind": "next-free-number", "dir": "supabase/migrations",
          "claims": 715, "result": "715"}


def _errors(census: list) -> list:
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    census_schema = schema["properties"]["census"]
    probe = {"type": "object", "properties": {"census": census_schema}}
    out = schema_validate._dispatch_validate(probe, {"census": census})
    return [] if out.get("ok") else out.get("errors", [])


def test_schema_version_is_2_40_0():
    assert json.loads(_SCHEMA.read_text(encoding="utf-8"))["x-schema-version"] == "2.40.0"


def test_command_form_validates():
    assert _errors([_COMMAND]) == []


def test_command_form_with_count_and_ref_validates():
    assert _errors([{**_COMMAND, "count": 3, "ref": "HEAD"}]) == []


def test_typed_form_validates():
    assert _errors([_TYPED]) == []


def test_mixed_forms_in_one_list_validate():
    assert _errors([_COMMAND, _TYPED]) == []


def test_mixed_entry_fails():
    assert _errors([{**_TYPED, "command": "ls"}])


def test_typed_with_count_fails():
    assert _errors([{**_TYPED, "count": 1}])


def test_command_entry_carrying_typed_keys_fails():
    assert _errors([{**_COMMAND, "dir": "x"}])


def test_half_typed_entries_fail():
    for missing in ("dir", "claims"):
        entry = {k: v for k, v in _TYPED.items() if k != missing}
        assert _errors([entry]), missing
    assert _errors([{k: v for k, v in _TYPED.items() if k != "kind"}])


def test_unknown_kind_and_bad_claims_fail():
    assert _errors([{**_TYPED, "kind": "other"}])
    assert _errors([{**_TYPED, "claims": -1}])
    assert _errors([{**_TYPED, "claims": "715"}])
    assert _errors([{**_TYPED, "dir": ""}])


def test_entry_with_neither_command_nor_kind_fails():
    assert _errors([{"question": "q", "result": "1"}])
