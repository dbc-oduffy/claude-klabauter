"""
coordinator_core.authz.tests.test_assembler_dispatchable — tests for
`ASSEMBLER_DISPATCHABLE`, the per-assembler op admission control.

Purpose: proves the load-bearing properties of C1's control: (AC7) every
op-family entry is a live registered op carrying an `OP_CLASSIFICATION` entry, no
phantom names; the mapping is immutable at runtime (`MappingProxyType` raises
`TypeError` on mutation, matching `OP_CLASSIFICATION`'s own shape); (AC8, resolver
end) a planted op name absent from the mapping is reported as not-dispatchable —
the mapping's own default-deny property, checked directly against the mapping
rather than through `apply_base.assert_dispatchable` (C2's seam, not this
chunk's); and (AC8, emitter end, C8) no completion-family module's own declared
`CONSUMES_MANIFEST` names a verb absent from its `ASSEMBLER_DISPATCHABLE` entry —
the static-surface closing guard, not merely a runtime-emitted-subset check.

No process spawn, no git — fast tier.

Spec backlink: pln-directives-name-an-op-not-a-cl-e283a9 § C1, § C8
"""

from __future__ import annotations

import pytest

from coordinator_core.authz.dispatchable import ASSEMBLER_DISPATCHABLE
from coordinator_core.authz.registration_quad import _live_classification, _live_registry


def _is_dispatchable(assembler_name: str, op_name: str) -> bool:
    """Mirrors ASSEMBLER_DISPATCHABLE's own default-deny semantics: absence from the
    mapping, or absence from the assembler's own set, is a refusal. A standalone
    check against the mapping itself — not a call into C2's `assert_dispatchable`,
    which does not exist yet in this chunk."""
    return op_name in ASSEMBLER_DISPATCHABLE.get(assembler_name, frozenset())


class TestShipsEmpty:
    """C1 shipped this mapping empty except for entries a later migration chunk
    adds. That is a claim about C1's OWN commit, not an invariant true of every
    later state of the tree -- C7 has since populated the completion family, so
    asserting emptiness here would be a false red the moment C7 lands, not a
    check of anything C1 actually guarantees going forward."""


class TestAC8ClosingGuard:
    """C8: no emission site names a verb absent from ASSEMBLER_DISPATCHABLE for
    its own assembler. This is AC8's teeth at the EMITTER end (a static-surface
    check) rather than only at the resolver end (a runtime refusal, which is
    correct but late).

    Per the phantom-cli-guard-seam precedent (DoE-claude
    coordinator/docs/wiki/coordinator-tripwires/phantom-cli-guard-seam.md), the
    check walks each completion-family module's own DECLARED static emission
    surface -- its `CONSUMES_MANIFEST`, the closed set every `directive["cli"]`
    value is drawn from (see e.g. workday_complete/brief.py's own docstring) --
    not whichever subset of verbs one test run happened to emit. A manifest
    member never actually dispatched during any single run is still checked,
    closing exactly the gap the precedent names: "a membership guard plus a
    runtime-emitted-subset guard compose to leave a manifest member
    undetectably undispatchable."

    Scope: only assembler modules that are themselves keys in
    ASSEMBLER_DISPATCHABLE (i.e. modules that have adopted the C2 admission
    control) are checked here -- `consolidate_assemble` and
    `contract/apply_base.py` are walked by C8's sweep too, but neither is
    (yet) an ASSEMBLER_DISPATCHABLE key: `consolidate_assemble`'s verbs stay
    `cli`-named by design (C6) and `apply_base.py` is the shared resolver, not
    an emission site of its own."""

    def test_every_consumes_manifest_member_is_in_assembler_dispatchable(self) -> None:
        from coordinator_core.workday_complete.brief import (
            CONSUMES_MANIFEST as workday_manifest,
        )
        from coordinator_core.workstream_complete import (
            CONSUMES_MANIFEST as workstream_manifest,
        )
        from coordinator_core.workweek_complete.brief import (
            CONSUMES_MANIFEST as workweek_manifest,
        )

        manifests_by_assembler = {
            "workday_complete": workday_manifest,
            "workstream_complete": workstream_manifest,
            "workweek_complete": workweek_manifest,
        }

        undispatchable = {}
        for assembler_name, manifest in manifests_by_assembler.items():
            missing = set(manifest) - set(
                ASSEMBLER_DISPATCHABLE.get(assembler_name, frozenset())
            )
            if missing:
                undispatchable[assembler_name] = sorted(missing)

        assert undispatchable == {}, (
            f"CONSUMES_MANIFEST member(s) with no corresponding "
            f"ASSEMBLER_DISPATCHABLE entry for their own assembler -- an "
            f"emission site could name a verb the C2 admission control would "
            f"refuse: {undispatchable!r}"
        )


class TestMutationRaises:
    """MappingProxyType wrapping means runtime mutation raises rather than silently
    escalating a dispatch privilege — the same property OP_CLASSIFICATION has."""

    def test_top_level_mutation_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            ASSEMBLER_DISPATCHABLE["pickup_assemble"] = frozenset({"planted.op"})

    def test_top_level_deletion_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            del ASSEMBLER_DISPATCHABLE["pickup_assemble"]


class TestDefaultDenyControl:
    """AC8: a planted op name absent from the mapping (whole assembler unknown, or
    assembler known but op not in its set) is reported as not-dispatchable — not
    merely 'fails to match the happy path'."""

    def test_planted_op_for_unknown_assembler_is_not_dispatchable(self) -> None:
        assert _is_dispatchable("planted_assembler", "planted.op") is False

    def test_planted_op_absent_from_known_assemblers_own_set_is_not_dispatchable(
        self,
    ) -> None:
        for assembler_name in ASSEMBLER_DISPATCHABLE:
            assert _is_dispatchable(assembler_name, "planted.op.not.a.real.op") is False


# The mixed-end-state discriminator (plan § The discriminator for the mixed end
# state) is keyed by ASSEMBLER_DISPATCHABLE's assembler-name key, not by per-entry
# name shape: the five assembler-family module names dispatch via `resolve_cli` /
# `resolve_op` and their entries must be live registered ops; the three
# completion-family module names dispatch via a private `_resolve_cli` built from
# CONSUMES_MANIFEST and are validated by that membership check instead (already
# asserted by workstream_complete/apply.py; not re-derived here). This split is
# named explicitly rather than inferred from whether an entry happens to be in
# `_REGISTRY`, so a genuine assembler-family phantom name cannot be
# misclassified as a completion-family exemption.
_COMPLETION_FAMILY_ASSEMBLERS = frozenset(
    {"workday_complete", "workstream_complete", "workweek_complete"}
)


class TestAC7RegistryAndClassificationBacking:
    """Every op-family entry in ASSEMBLER_DISPATCHABLE (assembler-family assemblers
    only) must be a live registered op (present in `_REGISTRY`) carrying an
    `OP_CLASSIFICATION` entry — no phantom names. Reuses registration_quad's
    live-table accessors (its own discovery walk already imports the full ops
    tree) rather than a plain `import coordinator_core.ops`, which under-discovers
    per that module's own docstring.

    Completion-family entries are exempt from this check by construction — see
    `_COMPLETION_FAMILY_ASSEMBLERS` above. C1 ships no entries of either family,
    so this loop is currently vacuous over an empty mapping; it is written
    generically so C4-C7's additions are covered without editing this test."""

    def test_every_assembler_family_entry_is_registered_and_classified(self) -> None:
        registry = _live_registry()
        classification = _live_classification()

        not_registered = []
        missing_classification = []
        for assembler_name, op_names in ASSEMBLER_DISPATCHABLE.items():
            if assembler_name in _COMPLETION_FAMILY_ASSEMBLERS:
                continue
            for op_name in op_names:
                if op_name not in registry:
                    not_registered.append((assembler_name, op_name))
                    continue
                if op_name not in classification:
                    missing_classification.append((assembler_name, op_name))

        assert not_registered == [], (
            f"ASSEMBLER_DISPATCHABLE assembler-family entries naming an op absent "
            f"from the live _REGISTRY (phantom names): {not_registered!r}"
        )
        assert missing_classification == [], (
            f"ASSEMBLER_DISPATCHABLE assembler-family entries registered but "
            f"missing an OP_CLASSIFICATION entry: {missing_classification!r}"
        )
