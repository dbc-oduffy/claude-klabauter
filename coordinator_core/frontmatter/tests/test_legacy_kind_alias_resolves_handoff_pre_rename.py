"""Pre-rename handoff kinds resolve through the reader alias.

The handoff schema's kind enum no longer registers `spinoff-roadmap`,
`spinoff-roadmap-creator` or `spinoff-goal`; `_schema_name_for_kind` must still
resolve each to its D1 target's schema, with the pairing sourced from
`baton_class._PRE_RENAME_ALIASES`. The alias is a named map, not a catch-all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.frontmatter import schema_validate as sv
from coordinator_core.frontmatter.baton_class import _PRE_RENAME_ALIASES

_SCHEMAS_DIR = Path(sv.__file__).resolve().parent / "schemas"


def _schemas() -> dict:
    return sv.load_schemas(_SCHEMAS_DIR)


@pytest.mark.parametrize("retired,target", sorted(_PRE_RENAME_ALIASES.items()))
def test_retired_kind_is_unregistered_and_resolves_to_its_target_schema(retired: str, target: str) -> None:
    schemas = _schemas()
    assert retired not in schemas["_byKind"]
    expected = schemas["_byKind"].get(target)
    assert expected is not None
    assert sv._schema_name_for_kind(schemas, retired) == expected


def test_alias_covers_every_pre_rename_pair() -> None:
    for retired, target in _PRE_RENAME_ALIASES.items():
        assert sv._LEGACY_KIND_ALIASES[retired] == target


def test_legacy_staff_review_alias_unchanged() -> None:
    assert sv._LEGACY_KIND_ALIASES["patrik-review"] == "staff-eng-review"


def test_alias_is_a_named_map_not_a_catch_all() -> None:
    assert sv._schema_name_for_kind(_schemas(), "spinoff-unregistered") is None
