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

from pathlib import Path

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


class TestC2InProcessConvergence:
    """C2 (docs/plans/2026-08-26-merges-directives-stop-starting-
    interpreters.md): three of the six named AC3 verbs converge onto
    `ceremony_common.cli_dispatch` (no subprocess); three stay on
    `_run_py_script` because none has an in-scope argument path for its own
    repo root (see each handler's own docstring for the specific gap).
    `node-ceremony-gate` and `tier-u-grant` are untouched by this chunk."""

    _CONVERGED_HANDLERS = (
        ma_apply._dispatch_merge_recovery_and_tag_cut,
        ma_apply._dispatch_portability_sweep,
        ma_apply._dispatch_check_no_illegal_paths,
    )
    _STILL_SPAWNING_HANDLERS = (
        ma_apply._dispatch_merge_gate_and_pr,
        ma_apply._dispatch_merge_release_notes_derive,
        ma_apply._dispatch_orphan_branch_sweep,
    )

    def test_converged_handlers_never_call_run_py_script(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*args, **kwargs):  # pragma: no cover - only fires on regression
            raise AssertionError("converged handler unexpectedly spawned a subprocess")

        monkeypatch.setattr(ma_apply, "_run_py_script", _boom)
        repo_root = Path(".").resolve()
        result = ma_apply._dispatch_check_no_illegal_paths([], repo_root)
        assert result["cli"] == "check-no-illegal-paths"

    def test_still_spawning_handlers_call_run_py_script(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []

        class _FakeProc:
            returncode = 0
            stdout = ""
            stderr = ""

        def _fake_run_py_script(script_name, args, repo_root):
            calls.append(script_name)
            return _FakeProc()

        monkeypatch.setattr(ma_apply, "_run_py_script", _fake_run_py_script)
        repo_root = Path(".").resolve()
        ma_apply._dispatch_merge_gate_and_pr(["pr-body"], repo_root)
        ma_apply._dispatch_merge_release_notes_derive(["flip-tags"], repo_root)
        ma_apply._dispatch_orphan_branch_sweep(["--format", "text"], repo_root)
        assert calls == ["merge-gate-and-pr", "merge-release-notes-derive", "orphan-branch-sweep"]

    def test_portability_sweep_absent_producer_raises_unrecognized_directive(self) -> None:
        repo_root = Path(".").resolve()
        assert not (repo_root / "coordinator" / "bin" / "portability-sweep.py").is_file()
        with pytest.raises(UnrecognizedDirective):
            ma_apply._dispatch_portability_sweep(
                [str(repo_root), "--diff-only", "origin/main..HEAD", "--report-format", "md"],
                repo_root,
            )

    def test_check_no_illegal_paths_dispatches_clean_in_process(self) -> None:
        repo_root = Path(".").resolve()
        result = ma_apply._dispatch_check_no_illegal_paths([], repo_root)
        assert result == {"cli": "check-no-illegal-paths", "returncode": 0, "stdout": ""}

    def test_merge_recovery_resolve_tag_prefix_anchors_relative_config(self) -> None:
        repo_root = Path(".").resolve()
        result = ma_apply._dispatch_merge_recovery_and_tag_cut(
            ["resolve-tag-prefix", "--config", "coordinator.local.md"], repo_root
        )
        assert result["cli"] == "merge-recovery-and-tag-cut"
        assert result["returncode"] == 0

    def test_merge_recovery_config_anchor_helper_only_touches_relative_paths(self) -> None:
        repo_root = Path("X_repo_root")
        out = ma_apply._anchor_merge_recovery_config_path(
            ["resolve-tag-prefix", "--config", "coordinator.local.md"], repo_root
        )
        assert out[-1] == str(repo_root / "coordinator.local.md")

        already_absolute = str((repo_root / "coordinator.local.md").resolve())
        out2 = ma_apply._anchor_merge_recovery_config_path(
            ["resolve-tag-prefix", "--config", already_absolute], repo_root
        )
        assert out2[-1] == already_absolute

    def test_merge_recovery_cut_tag_gets_explicit_repo_root_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, list[str]] = {}

        def _fake_dispatch_in_process(cli, script_name, args):
            captured["args"] = args
            return {"cli": cli, "returncode": 0, "stdout": ""}

        monkeypatch.setattr(ma_apply, "_dispatch_in_process", _fake_dispatch_in_process)
        repo_root = Path(".").resolve()
        ma_apply._dispatch_merge_recovery_and_tag_cut(["cut-tag", "v1.2.3"], repo_root)
        assert captured["args"] == ["cut-tag", "v1.2.3", "--repo-root", str(repo_root)]
