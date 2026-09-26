
from __future__ import annotations

import typing

from coordinator_core.contract.cockpit_schema.entities.summaries import HandoffKind
from coordinator_core.frontmatter.baton_class import (
    _PRE_RENAME_ALIASES,
    _load_mapping,
    canonical_kind,
    kind_values_for_canonical,
)


def test_canonical_kind_maps_each_retired_value_to_its_successor():
    assert canonical_kind("spinoff-roadmap") == "roadmap-baton"
    assert canonical_kind("spinoff-roadmap-creator") == "roadmap-seed"
    assert canonical_kind("spinoff-goal") == "goal-seed"


def test_canonical_kind_passes_canonical_values_through_unchanged():
    assert canonical_kind("roadmap-baton") == "roadmap-baton"
    assert canonical_kind("roadmap-seed") == "roadmap-seed"
    assert canonical_kind("goal-seed") == "goal-seed"
    assert canonical_kind("session-handoff") == "session-handoff"


def test_canonical_kind_normalises_case_and_whitespace():
    assert canonical_kind("  Spinoff-Roadmap  ") == "roadmap-baton"
    assert canonical_kind("  ROADMAP-BATON ") == "roadmap-baton"


def test_canonical_kind_handles_absent_or_falsy_kind():
    assert canonical_kind(None) == ""
    assert canonical_kind("") == ""
    assert canonical_kind("   ") == ""


def test_kind_values_for_canonical_includes_canonical_and_retired_alias():
    values = kind_values_for_canonical("roadmap-baton")
    assert values[0] == "roadmap-baton"
    assert "spinoff-roadmap" in values


def test_kind_values_for_canonical_no_alias_returns_canonical_only():
    assert kind_values_for_canonical("session-handoff") == ["session-handoff"]


def test_kind_values_for_canonical_covers_every_alias_target():
    for retired, canonical in _PRE_RENAME_ALIASES.items():
        assert retired in kind_values_for_canonical(canonical)


def test_every_handoff_kind_except_spike_result_has_a_baton_class_mapping_entry():
    mapping = _load_mapping()
    for kind in typing.get_args(HandoffKind):
        if kind == "spike-result":
            continue
        canonical = canonical_kind(kind)
        assert canonical in mapping, (
            f"HandoffKind {kind!r} (canonical {canonical!r}) has no "
            "x-baton-class.mapping entry in the vendored handoff schema — "
            "baton_class() will silently return None for it"
        )
