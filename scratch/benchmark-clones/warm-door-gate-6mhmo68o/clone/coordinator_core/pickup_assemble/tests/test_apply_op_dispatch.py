"""
coordinator_core.pickup_assemble.tests.test_apply_op_dispatch — C4 coverage
proving the plan's discriminator on the smallest assembler-family table.

Purpose: `pickup_assemble` is the smallest of the five assembler-family
tables (plan § C4) — chosen to prove the discriminator's shape before the
wider sweep (C5/C6). Measured this chunk: none of pickup's three
`_CLI_DISPATCH` entries (`session-claim-cli`, `archive-stamp-cli`,
`coordinator-tasks-mirror`) resolve to a registered op, so all three stay
`cli`-named and `ASSEMBLER_DISPATCHABLE` gains no `"pickup_assemble"` entry
from this chunk — see the decision comment above `_CLI_DISPATCH` in
`pickup_assemble/apply.py`.

This is a negative proof, not an inert one: it asserts the refusal shape a
verb this table does NOT migrate still gets, at both seams C1-C3 built —
`resolve_op` (AC8's "reaches nothing", not merely "fails the happy path")
and the unchanged `resolve_cli` happy path (the unit genuinely did not
change for any of the three).

Spec backlink: docs/plans/2026-08-19-directives-name-an-op-not-a-cli.md § C4

No process spawn, no git — fast tier.
"""

from __future__ import annotations

import pytest

import coordinator_core.contract.apply_base as apply_base
from coordinator_core.authz.dispatchable import ASSEMBLER_DISPATCHABLE
from coordinator_core.authz.registration_quad import _live_registry
from coordinator_core.contract.apply_base import UnrecognizedDirective
from coordinator_core.pickup_assemble import apply as pa_apply

_PICKUP_CLI_VERBS = ("session-claim-cli", "archive-stamp-cli", "coordinator-tasks-mirror")


def test_pickup_cli_verbs_are_the_expected_closed_set() -> None:
    assert set(pa_apply._CLI_DISPATCH) == set(_PICKUP_CLI_VERBS)


class TestNoneOfPickupsVerbsAreRegisteredOps:
    """The C4 discriminator finding, checked live rather than only asserted
    in a comment: none of pickup's three verbs resolve to a registered op."""

    def test_none_resolve_via_live_registry(self) -> None:
        registry = _live_registry()
        registered = [v for v in _PICKUP_CLI_VERBS if v in registry]
        assert registered == [], (
            f"pickup_assemble verb(s) unexpectedly found in _REGISTRY: {registered!r} — "
            "the C4 discriminator decision (none migrate) needs re-deriving"
        )


class TestZeroEntriesMigrated:
    """C1's "ship it EMPTY except for entries actually migrated" — zero
    migrated here, so pickup_assemble must carry no entry at all."""

    def test_pickup_assemble_has_no_assembler_dispatchable_entry(self) -> None:
        assert "pickup_assemble" not in ASSEMBLER_DISPATCHABLE


class TestResolveCliUnitUnchanged:
    """The unit did not change for any of the three verbs — `resolve_cli`
    still resolves each to its existing hand-written adapter."""

    @pytest.mark.parametrize("verb", _PICKUP_CLI_VERBS)
    def test_resolve_cli_still_resolves_each_verb(self, verb: str) -> None:
        handler = pa_apply._resolve_cli(verb)
        assert handler is pa_apply._CLI_DISPATCH[verb]

    def test_resolve_cli_unrecognized_name_still_raises(self) -> None:
        with pytest.raises(UnrecognizedDirective):
            pa_apply._resolve_cli("not-a-real-pickup-cli-name")


class TestResolveOpReachesNothingForPickupsVerbs:
    """AC8's shape: attempting to dispatch any of pickup's three verbs via
    the `op` seam (`resolve_op`) — the path a directive would need to use to
    treat them as op-named — is refused, since none is allowlisted for
    `pickup_assemble` (in fact no `pickup_assemble` entry exists at all)."""

    @pytest.mark.parametrize("verb", _PICKUP_CLI_VERBS)
    def test_resolve_op_refuses_each_verb(self, verb: str) -> None:
        with pytest.raises(UnrecognizedDirective):
            apply_base.resolve_op(pa_apply._CLI_DISPATCH, "pickup_assemble", verb)
