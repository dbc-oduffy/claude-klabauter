"""Retired `the Staff Engineer-review` kind resolves to review-sidecar through the reader alias.

review-sidecar 2.0.0 dropped the persona-named kind from `kinds`; two archived
sidecars outside the `applies_to` glob still declare it, so `match_schema` must
resolve it via `_LEGACY_KIND_ALIASES`. The alias is a named map, not a catch-all.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.frontmatter import schema_validate as sv

_SCHEMAS_DIR = Path(sv.__file__).resolve().parent / "schemas"
_ARCHIVED_SIDECAR = "docs/plans/2026-06-17-cross-platform-install-hardening.md.patrik-review.md"


def _schemas() -> dict:
    return sv.load_schemas(_SCHEMAS_DIR)


def test_retired_kind_is_not_registered():
    assert "patrik-review" not in _schemas()["_byKind"]


def test_retired_kind_resolves_to_review_sidecar_outside_the_glob():
    resolved = sv.match_schema(_ARCHIVED_SIDECAR, {"kind": "patrik-review"}, _schemas())
    assert resolved is not None and resolved["schemaName"] == "review-sidecar"


def test_live_kind_still_resolves_to_review_sidecar():
    resolved = sv.match_schema(_ARCHIVED_SIDECAR, {"kind": "staff-eng-review"}, _schemas())
    assert resolved is not None and resolved["schemaName"] == "review-sidecar"


def test_alias_is_a_named_map_not_a_catch_all():
    resolved = sv.match_schema(_ARCHIVED_SIDECAR, {"kind": "unregistered-review"}, _schemas())
    assert resolved is None or resolved["schemaName"] != "review-sidecar"
