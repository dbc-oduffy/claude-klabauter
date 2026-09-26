
from __future__ import annotations

from typing import Any

import pytest

import coordinator_core.contract.apply_base as apply_base
from coordinator_core.authz.dispatchable import ASSEMBLER_DISPATCHABLE
from coordinator_core.authz.registration_quad import _live_registry
from coordinator_core.contract.apply_base import UnrecognizedDirective
from coordinator_core.baton_assemble import apply as ba_apply

_BATON_CLI_VERBS = (
    "coordinator-doc-new",
    "lint-frontmatter",
    "session-claim-cli",
    "handoff.stamp_phase",
    "handoff.author_fork",
    "handoff.supersede_predecessor",
    "handoff-carry-gate",
    "baton-stamp-carried-ids",
)
_MIGRATED_VERBS = ("handoff.stamp_phase", "handoff.author_fork")
_MIGRATED_VERBS__SUBJECT_CLASS = "op-name"
_NON_MIGRATED_VERBS = tuple(v for v in _BATON_CLI_VERBS if v not in _MIGRATED_VERBS)


def test_baton_cli_verbs_are_the_expected_closed_set() -> None:
    assert set(ba_apply._CLI_DISPATCH) == set(_BATON_CLI_VERBS)


def test_op_migrated_verbs_constant_matches_the_authz_allowlist() -> None:
    assert ba_apply._OP_MIGRATED_VERBS == ASSEMBLER_DISPATCHABLE["baton_assemble"]


class TestDiscriminatorFindingCheckedLive:

    def test_migrated_verbs_are_registered(self) -> None:
        registry = _live_registry()
        missing = [v for v in _MIGRATED_VERBS if v not in registry]
        assert missing == [], (
            f"baton_assemble verb(s) expected registered but not found in "
            f"_REGISTRY: {missing!r} -- the C5 discriminator decision needs "
            "re-deriving"
        )

    def test_supersede_predecessor_is_not_registered(self) -> None:
        registry = _live_registry()
        assert "handoff.supersede_predecessor" not in registry


class TestMigrateOpNamedDirectives:

    def test_migrated_verb_gets_rewritten_to_op_key(self) -> None:
        directives = [{"id": "d3", "cli": "handoff.author_fork", "args": ["x"]}]
        out = ba_apply._migrate_op_named_directives(directives)
        assert out == [{"id": "d3", "op": "handoff.author_fork", "args": ["x"]}]

    def test_non_migrated_verb_passes_through_unchanged(self) -> None:
        directives = [{"id": "d6", "cli": "handoff.supersede_predecessor", "args": []}]
        out = ba_apply._migrate_op_named_directives(directives)
        assert out == directives

    def test_original_directive_dicts_are_not_mutated(self) -> None:
        original = {"id": "d3", "cli": "handoff.author_fork", "args": []}
        directives = [original]
        ba_apply._migrate_op_named_directives(directives)
        assert original == {"id": "d3", "cli": "handoff.author_fork", "args": []}

    def test_mixed_list_only_rewrites_the_migrated_entries(self) -> None:
        directives = [
            {"id": "d3", "cli": "handoff.author_fork", "args": []},
            {"id": "d6", "cli": "handoff.supersede_predecessor", "args": []},
        ]
        out = ba_apply._migrate_op_named_directives(directives)
        assert out[0]["op"] == "handoff.author_fork"
        assert "cli" not in out[0]
        assert out[1]["cli"] == "handoff.supersede_predecessor"
        assert "op" not in out[1]


class TestResolveCliUnitUnchangedForEveryVerb:
    """The unit did not change for any of the seven verbs -- `resolve_cli`
    still resolves each to its existing hand-written adapter, migrated or
    not (migration only changes which KEY a directive carries, never the
    handler `_CLI_DISPATCH` names)."""

    @pytest.mark.parametrize("verb", _BATON_CLI_VERBS)
    def test_resolve_cli_still_resolves_each_verb(self, verb: str) -> None:
        handler = ba_apply._resolve_cli(verb)
        assert handler is ba_apply._CLI_DISPATCH[verb]

    def test_resolve_cli_unrecognized_name_still_raises(self) -> None:
        with pytest.raises(UnrecognizedDirective):
            ba_apply._resolve_cli("not-a-real-baton-cli-name")


class TestResolveOpForMigratedVerbs:
    """Positive proof: each migrated verb reaches its EXISTING handler
    through the `op` seam, allowlisted for `baton_assemble`."""

    @pytest.mark.parametrize("verb", _MIGRATED_VERBS)
    def test_resolve_op_resolves_to_the_same_adapter_as_resolve_cli(self, verb: str) -> None:
        handler = apply_base.resolve_op(ba_apply._CLI_DISPATCH, "baton_assemble", verb)
        assert handler is ba_apply._CLI_DISPATCH[verb]
        assert handler is ba_apply._resolve_cli(verb)


class TestResolveOpReachesNothingForNonMigratedVerbs:

    @pytest.mark.parametrize("verb", _NON_MIGRATED_VERBS)
    def test_resolve_op_refuses_each_non_migrated_verb(self, verb: str) -> None:
        with pytest.raises(UnrecognizedDirective):
            apply_base.resolve_op(ba_apply._CLI_DISPATCH, "baton_assemble", verb)


class TestExecuteDirectivesThreadsAssemblerName:
    """`_execute_directives` passes `assembler_name="baton_assemble"`
    unconditionally into `apply_base.execute_directives`, so any directive
    `_migrate_op_named_directives` rewrote to carry an `op` key resolves
    through the allowlisted seam rather than falling back to
    `ASSEMBLER_DISPATCHABLE.get(None, frozenset())`'s always-empty default."""

    def test_a_migrated_op_directive_dispatches_successfully(self, tmp_path) -> None:
        calls = []

        def _tracking_handler(args, repo_root):
            calls.append(args)
            return {"cli": "handoff.stamp_phase", "args": args}

        table = dict(ba_apply._CLI_DISPATCH)
        table["handoff.stamp_phase"] = _tracking_handler
        directives = [{"id": "d1", "op": "handoff.stamp_phase", "args": ["x"]}]
        exit_code, report = apply_base.execute_directives(
            directives, [], tmp_path, table, assembler_name="baton_assemble"
        )
        assert exit_code == apply_base.APPLY_EXIT_OK
        assert calls == [["x"]]
        assert report["landed"] == ["d1"]

    def test_execute_directives_threads_assembler_name_into_apply_base(
        self, monkeypatch, tmp_path
    ) -> None:
        captured: dict[str, Any] = {}

        def _fake_execute_directives(directives, judgment_points, repo_root, dispatch_table, **kwargs):
            captured.update(kwargs)
            return apply_base.APPLY_EXIT_OK, {"landed": []}

        monkeypatch.setattr(apply_base, "execute_directives", _fake_execute_directives)
        ba_apply._execute_directives([], [], tmp_path)
        assert captured.get("assembler_name") == "baton_assemble"


class TestCliKeyedMigratedVerbIsRefused:

    def test_cli_keyed_handoff_stamp_phase_reaches_nothing(self, tmp_path) -> None:
        directives = [{"id": "d1", "cli": "handoff.stamp_phase", "args": ["x"]}]
        exit_code, report = ba_apply._execute_directives(directives, [], tmp_path)
        assert exit_code == apply_base.APPLY_EXIT_TRANSPORT_FAIL
        assert report["landed"] == []
