"""
coordinator_core.consolidate_assemble.tests.test_apply_op_dispatch — C6
coverage proving the plan's discriminator on consolidate_assemble's
six-entry table.

Purpose: measured this chunk, none of consolidate's six `_CLI_DISPATCH`
entries (`delete-only`, `cherry-pick-and-delete`, `merge-and-delete`,
`worktree-remove`, `worktree-prune`, `fetch-prune`) resolve to a registered
op, so all six stay `cli`-named and `ASSEMBLER_DISPATCHABLE` gains no
`"consolidate_assemble"` entry from this chunk — see the decision comment
above `_CLI_DISPATCH` in `consolidate_assemble/apply.py`.

Same negative-proof shape `pickup_assemble`'s own C4 all-cli/none-migrate
suite established (`coordinator_core/pickup_assemble/tests/
test_apply_op_dispatch.py`): asserts the refusal a non-migrated verb still
gets at the `op` seam, and that the unchanged `cli` happy path is untouched.

Spec backlink: docs/plans/2026-08-19-directives-name-an-op-not-a-cli.md § C6

No process spawn, no git — fast tier.
"""

from __future__ import annotations

import pytest

import coordinator_core.contract.apply_base as apply_base
from coordinator_core.authz.dispatchable import ASSEMBLER_DISPATCHABLE
from coordinator_core.authz.registration_quad import _live_registry
from coordinator_core.contract.apply_base import UnrecognizedDirective
from coordinator_core.consolidate_assemble import apply as ca_apply

_CONSOLIDATE_CLI_VERBS = (
    "delete-only",
    "cherry-pick-and-delete",
    "merge-and-delete",
    "worktree-remove",
    "worktree-prune",
    "fetch-prune",
)


def test_consolidate_cli_verbs_are_the_expected_closed_set() -> None:
    assert set(ca_apply._CLI_DISPATCH) == set(_CONSOLIDATE_CLI_VERBS)


class TestNoneOfConsolidatesVerbsAreRegisteredOps:
    """The C6 discriminator finding, checked live rather than only asserted
    in a comment: none of consolidate's six verbs resolve to a registered
    op."""

    def test_none_resolve_via_live_registry(self) -> None:
        registry = _live_registry()
        registered = [v for v in _CONSOLIDATE_CLI_VERBS if v in registry]
        assert registered == [], (
            f"consolidate_assemble verb(s) unexpectedly found in _REGISTRY: "
            f"{registered!r} — the C6 discriminator decision (none migrate) "
            "needs re-deriving"
        )


class TestZeroEntriesMigrated:
    """C1's "ship it EMPTY except for entries actually migrated" — zero
    migrated here, so consolidate_assemble must carry no entry at all."""

    def test_consolidate_assemble_has_no_assembler_dispatchable_entry(self) -> None:
        assert "consolidate_assemble" not in ASSEMBLER_DISPATCHABLE


class TestResolveCliUnitUnchanged:
    """The unit did not change for any of the six verbs — `resolve_cli`
    still resolves each to its existing hand-written adapter."""

    @pytest.mark.parametrize("verb", _CONSOLIDATE_CLI_VERBS)
    def test_resolve_cli_still_resolves_each_verb(self, verb: str) -> None:
        handler = ca_apply._CLI_DISPATCH[verb]
        assert apply_base.resolve_cli(ca_apply._CLI_DISPATCH, verb) is handler

    def test_resolve_cli_unrecognized_name_still_raises(self) -> None:
        with pytest.raises(UnrecognizedDirective):
            apply_base.resolve_cli(ca_apply._CLI_DISPATCH, "not-a-real-consolidate-cli-name")


class TestResolveOpReachesNothingForConsolidatesVerbs:
    """AC8's shape: attempting to dispatch any of consolidate's six verbs
    via the `op` seam (`resolve_op`) — the path a directive would need to
    use to treat them as op-named — is refused, since none is allowlisted
    for `consolidate_assemble` (in fact no `consolidate_assemble` entry
    exists at all)."""

    @pytest.mark.parametrize("verb", _CONSOLIDATE_CLI_VERBS)
    def test_resolve_op_refuses_each_verb(self, verb: str) -> None:
        with pytest.raises(UnrecognizedDirective):
            apply_base.resolve_op(ca_apply._CLI_DISPATCH, "consolidate_assemble", verb)
