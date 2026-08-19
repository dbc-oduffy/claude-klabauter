"""
coordinator_core.merge_assemble.tests.test_apply_op_dispatch — C6 coverage
proving the plan's discriminator on merge_assemble's eight-entry table, the
largest of the three C6 tables.

Purpose: measured this chunk, none of merge's eight `_CLI_DISPATCH` entries
(`node-ceremony-gate`, `merge-recovery-and-tag-cut`, `merge-gate-and-pr`,
`portability-sweep`, `check-no-illegal-paths`, `merge-release-notes-derive`,
`orphan-branch-sweep`, `tier-u-grant`) resolve to a registered op, so all
eight stay `cli`-named and `ASSEMBLER_DISPATCHABLE` gains no
`"merge_assemble"` entry from this chunk — see the decision comment above
`_CLI_DISPATCH` in `merge_assemble/apply.py`. `orphan-branch-sweep` is the
one name closest to a registered surface (its own bin script composes four
registered `git_branch.*` ops internally), checked live here specifically
so that near-miss is verified rather than merely asserted in a comment.

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
from coordinator_core.merge_assemble import apply as ma_apply

_MERGE_CLI_VERBS = (
    "node-ceremony-gate",
    "merge-recovery-and-tag-cut",
    "merge-gate-and-pr",
    "portability-sweep",
    "check-no-illegal-paths",
    "merge-release-notes-derive",
    "orphan-branch-sweep",
    "tier-u-grant",
)


def test_merge_cli_verbs_are_the_expected_closed_set() -> None:
    assert set(ma_apply._CLI_DISPATCH) == set(_MERGE_CLI_VERBS)


class TestNoneOfMergesVerbsAreRegisteredOps:
    """The C6 discriminator finding, checked live rather than only asserted
    in a comment: none of merge's eight verbs resolve to a registered op —
    including `orphan-branch-sweep`, the one name closest to a registered
    surface."""

    def test_none_resolve_via_live_registry(self) -> None:
        registry = _live_registry()
        registered = [v for v in _MERGE_CLI_VERBS if v in registry]
        assert registered == [], (
            f"merge_assemble verb(s) unexpectedly found in _REGISTRY: "
            f"{registered!r} — the C6 discriminator decision (none migrate) "
            "needs re-deriving"
        )

    def test_orphan_branch_sweep_specifically_is_not_registered(self) -> None:
        registry = _live_registry()
        assert "orphan-branch-sweep" not in registry


class TestZeroEntriesMigrated:
    """C1's "ship it EMPTY except for entries actually migrated" — zero
    migrated here, so merge_assemble must carry no entry at all."""

    def test_merge_assemble_has_no_assembler_dispatchable_entry(self) -> None:
        assert "merge_assemble" not in ASSEMBLER_DISPATCHABLE


class TestResolveCliUnitUnchanged:
    """The unit did not change for any of the eight verbs — `resolve_cli`
    still resolves each to its existing hand-written adapter."""

    @pytest.mark.parametrize("verb", _MERGE_CLI_VERBS)
    def test_resolve_cli_still_resolves_each_verb(self, verb: str) -> None:
        handler = ma_apply._CLI_DISPATCH[verb]
        assert apply_base.resolve_cli(ma_apply._CLI_DISPATCH, verb) is handler

    def test_resolve_cli_unrecognized_name_still_raises(self) -> None:
        with pytest.raises(UnrecognizedDirective):
            apply_base.resolve_cli(ma_apply._CLI_DISPATCH, "not-a-real-merge-cli-name")


class TestResolveOpReachesNothingForMergesVerbs:
    """AC8's shape: attempting to dispatch any of merge's eight verbs via
    the `op` seam (`resolve_op`) — the path a directive would need to use
    to treat them as op-named — is refused, since none is allowlisted for
    `merge_assemble` (in fact no `merge_assemble` entry exists at all)."""

    @pytest.mark.parametrize("verb", _MERGE_CLI_VERBS)
    def test_resolve_op_refuses_each_verb(self, verb: str) -> None:
        with pytest.raises(UnrecognizedDirective):
            apply_base.resolve_op(ma_apply._CLI_DISPATCH, "merge_assemble", verb)
