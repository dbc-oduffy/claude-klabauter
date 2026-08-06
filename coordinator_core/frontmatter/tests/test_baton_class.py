"""
Tests for coordinator_core.frontmatter.baton_class's `canonical_kind` and
`kind_values_for_canonical` — the C4 baton-kind-vocabulary migration's
single normaliser and query-term helper (see that module's own "Vocabulary
bridge" section).

Spec backlink: docs/plans/2026-07-29-baton-kind-vocabulary-one-axis-per-field.md § C4
"""

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
    # Every retired->successor pair in the alias table must be reachable
    # from kind_values_for_canonical(successor) — the query-string helper
    # (audit.py / number_stubs.py) trusts this to cover the full live
    # vocabulary without a second hand-authored copy of the pairing.
    for retired, canonical in _PRE_RENAME_ALIASES.items():
        assert retired in kind_values_for_canonical(canonical)


def test_every_handoff_kind_except_spike_result_has_a_baton_class_mapping_entry():
    """Schema-parity guard (Finding 3, 28a20f28 review): `baton_class()` nulls
    on ANY `kind` absent from the vendored schema's `x-baton-class.mapping`,
    not only `spike-result` — the field docstring's `spike-result` framing
    describes TODAY's `HandoffKind` enum (the one kind currently unmapped),
    not the derivation's actual contract. A future `HandoffKind` addition
    landed without a matching mapping entry would silently null with no
    signal; this is the tripwire that catches that at test time instead of
    relying on `test_handoff_kind_baton_widen.py`'s union-widen check, which
    only asserts the enum grows, never that new members stay mapped."""
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
