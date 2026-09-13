"""
test_provenance_conditionals_survive_enum_widen — tripwire for a site-matcher
that silently matches nothing.

`_is_provenance_site` recognises a ProvenanceEnvelope by EXACT membership of
`_ALL_SOURCE_KINDS`. That set is built from two hand-maintained lists, so
widening `SourceKind` on the model without updating them makes every emitted
enum a different length than the matcher expects — zero matches, no error, and
every provenance conditional silently absent from every entity.

It happened on the 4.7.0 Perforce bump (2026-09-12): adding `p4_server` /
`p4_workspace` took the emitted enum to 10 against a matcher expecting 8, and
all 58 conditionals across 30 files vanished. Emission succeeded. Byte-identity
checks on the git `ref` member passed. The regression was invisible to every
check being run at the time and was caught only by DoE counting `allOf` nodes
before committing the regen.

What the conditionals enforce is the D5/D9 bidirectional presence rule:
VCS-backed sources MUST carry a non-null `ref`, non-VCS sources MUST carry a
null one. Without them a `local_fs` fact can carry a populated `ref` and
validate — the precise lie the envelope exists to prevent.

Two independent guards here, because they fail differently:
  - the parity test catches the CAUSE (lists drifted from the model) at edit
    time, naming the fix;
  - the liveness test proves the runtime tripwire in `emit_schema` can still
    report red, so it cannot rot into a no-op that asserts nothing.
"""
from __future__ import annotations

import typing

import pytest

from coordinator_core.contract.cockpit_schema import emit_schema
from coordinator_core.contract.cockpit_schema.provenance import (
    ProvenanceEnvelope,
    SourceKind,
)


def test_source_kind_lists_match_the_model():
    """The emitter's partition must cover the model's enum exactly.

    Every member must appear in exactly one of the two lists: a member in
    NEITHER breaks the site-matcher (the 4.7.0 regression); a member in BOTH
    would emit contradictory conditionals requiring `ref` to be simultaneously
    null and non-null.
    """
    model_kinds = set(typing.get_args(SourceKind))
    vcs = set(emit_schema._VCS_BACKED_ENUM)
    non_vcs = set(emit_schema._NON_GIT_ENUM)

    assert vcs | non_vcs == model_kinds, (
        "SourceKind and the emitter's partition have drifted. Unpartitioned "
        f"(in the model, in neither list): {sorted(model_kinds - (vcs | non_vcs))}. "
        f"Stale (in a list, not in the model): {sorted((vcs | non_vcs) - model_kinds)}. "
        "A member in neither list makes _ALL_SOURCE_KINDS the wrong size, so "
        "_is_provenance_site matches NOTHING and every conditional is silently "
        "dropped. Add the member to _VCS_BACKED_ENUM (ref must be non-null) or "
        "_NON_GIT_ENUM (ref must be null) in emit_schema.py."
    )
    assert vcs & non_vcs == set(), (
        f"source kinds in BOTH lists: {sorted(vcs & non_vcs)} — would emit "
        "conditionals requiring `ref` to be null and non-null at once."
    )
    assert emit_schema._ALL_SOURCE_KINDS == model_kinds


def test_provenance_entity_actually_carries_conditionals():
    """The outcome, not the shape of the lists that produce it."""
    schema = emit_schema.build_entity_schema(ProvenanceEnvelope)
    assert emit_schema._count_injected_conditionals(schema) > 0
    assert emit_schema._count_provenance_sites(schema) > 0


def test_perforce_kinds_are_partitioned_as_vcs_backed():
    """p4 facts are server/workspace observations at a changelist — they carry
    a ref. Landing them in _NON_GIT_ENUM would invert the rule and forbid it."""
    assert "p4_server" in emit_schema._VCS_BACKED_ENUM
    assert "p4_workspace" in emit_schema._VCS_BACKED_ENUM


def test_runtime_tripwire_reports_red_on_a_matcherless_site():
    """Liveness: an instrument that cannot fail is not a guard.

    Feeds the assertion a provenance-SHAPED schema carrying no conditionals —
    exactly the 4.7.0 emission — and requires it to raise.
    """
    unguarded = {
        "properties": {
            "source_kind": {"enum": ["something_unrecognised"]},
            "derivation": {"type": "string"},
            "observed_at": {"type": "string"},
        }
    }
    with pytest.raises(RuntimeError, match="NO conditionals were injected"):
        emit_schema._assert_provenance_conditionals_injected("probe", unguarded)


def test_runtime_tripwire_stays_quiet_on_a_healthy_emission():
    """And does not fire on the real thing — a guard that always fires is noise."""
    healthy = emit_schema.build_entity_schema(ProvenanceEnvelope)
    emit_schema._assert_provenance_conditionals_injected("provenance-envelope", healthy)
