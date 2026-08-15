"""
Tests for coordinator_core.contract.apply_base — the shared Tier-B apply
runner every mutating computed-skill assembler's own `apply()` composes
(DR-092's named un-defer trigger; B4 chunk C2).

`pickup_assemble/apply.py` is the first (and, at the time this suite was
written, only) real consumer — its own `coordinator_core/test_pickup_apply.py`
exercises this module's generic machinery indirectly through pickup's thin
wrappers. This suite tests `apply_base` DIRECTLY, against a synthetic
dispatch table with no pickup-specific concept (no `_CLI_DISPATCH`, no
handoff/memo classification) — proving the module's own claim that it holds
no domain opinion: `resolve_cli`/`execute_directives` take a dispatch table
as a plain parameter, and `scoped_commit` takes a `run_git` callable, never
importing or assuming anything pickup-shaped.

Six areas under test, mirroring apply_base's own structure:
  (a) `resolve_cli` — closed-dispatch-table resolution, unrecognized name.
  (b) `normalize_primitive_result` — bool/int return-convention normalization.
  (c) `order_by_depends_on` / `DirectiveDependencyCycle` — topological sort,
      cycle detection.
  (d) `disposition_resolves_directive` / `directive_gate_open` — the
      value-aware judgment-point predicate (a disposition being SET is not
      sufficient; its OWN `resolves` list must name the directive).
  (e) `execute_directives` — the full per-directive-halt seam: clean run,
      halted-at-judgment (mixed per-directive), claim-denied, transport-fail
      (cycle, unrecognized cli), partial-mutation (handler raises), and the
      `already_satisfied` skip-without-dispatch path.
  (f) `assert_in_repo_root` / `reject_path_traversal` / `scoped_commit` — path
      safety and the pathspec-scoped commit, via a recording fake `run_git`
      (no real git process — the pickup-side `test_pickup_assemble_scoped_
      commit.py` already covers the real-git integration path).

Spec backlink: DoE-claude DoE-claude:pln-b4-baton-branch-lifecycle-comp-780d48, chunk C2
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from coordinator_core.contract import apply_base


# ---------------------------------------------------------------------------
# A synthetic, non-pickup-shaped dispatch table — proves the module carries
# no domain opinion of its own.
# ---------------------------------------------------------------------------

def _handler_ok(args: list[str], repo_root: Path) -> dict[str, Any]:
    return {"args": args, "repo_root": str(repo_root)}


def _handler_raises(args: list[str], repo_root: Path) -> dict[str, Any]:
    raise RuntimeError("synthetic handler failure")


_DISPATCH_TABLE = {"noop-cli": _handler_ok, "raising-cli": _handler_raises}


# ---------------------------------------------------------------------------
# (a) resolve_cli
# ---------------------------------------------------------------------------

class TestResolveCli:
    def test_resolves_a_real_entry(self):
        assert apply_base.resolve_cli(_DISPATCH_TABLE, "noop-cli") is _handler_ok

    def test_unrecognized_name_raises(self):
        with pytest.raises(apply_base.UnrecognizedDirective):
            apply_base.resolve_cli(_DISPATCH_TABLE, "rm")

    def test_never_mutates_the_supplied_table(self):
        table = dict(_DISPATCH_TABLE)
        with pytest.raises(apply_base.UnrecognizedDirective):
            apply_base.resolve_cli(table, "unknown")
        assert table == _DISPATCH_TABLE


# ---------------------------------------------------------------------------
# (b) normalize_primitive_result
# ---------------------------------------------------------------------------

class TestNormalizePrimitiveResult:
    def test_bool_true_passes_through(self):
        assert apply_base.normalize_primitive_result(True) is True

    def test_bool_false_passes_through(self):
        assert apply_base.normalize_primitive_result(False) is False

    def test_int_zero_is_success(self):
        assert apply_base.normalize_primitive_result(0) is True

    def test_nonzero_int_is_failure(self):
        assert apply_base.normalize_primitive_result(1) is False
        assert apply_base.normalize_primitive_result(-1) is False

    def test_unrecognized_type_raises(self):
        with pytest.raises(TypeError):
            apply_base.normalize_primitive_result("0")


# ---------------------------------------------------------------------------
# (c) order_by_depends_on / DirectiveDependencyCycle
# ---------------------------------------------------------------------------

class TestOrderByDependsOn:
    def test_stable_order_when_no_dependencies(self):
        directives = [{"id": "d1"}, {"id": "d2"}, {"id": "d3"}]
        ordered = apply_base.order_by_depends_on(directives)
        assert [d["id"] for d in ordered] == ["d1", "d2", "d3"]

    def test_dependency_forces_downstream_ordering(self):
        directives = [
            {"id": "d2", "depends_on": "d1"},
            {"id": "d1"},
        ]
        ordered = apply_base.order_by_depends_on(directives)
        assert [d["id"] for d in ordered] == ["d1", "d2"]

    def test_depends_on_naming_a_non_directive_id_is_ignored(self):
        # e.g. a judgment-point id left on the dict by the assembler.
        directives = [{"id": "d1", "depends_on": "j1"}]
        ordered = apply_base.order_by_depends_on(directives)
        assert [d["id"] for d in ordered] == ["d1"]

    def test_cycle_raises_directive_dependency_cycle(self):
        directives = [
            {"id": "d1", "depends_on": "d2"},
            {"id": "d2", "depends_on": "d1"},
        ]
        with pytest.raises(apply_base.DirectiveDependencyCycle):
            apply_base.order_by_depends_on(directives)


# ---------------------------------------------------------------------------
# (d) disposition_resolves_directive / directive_gate_open
# ---------------------------------------------------------------------------

_JP_TWO_WAY = {
    "id": "j1",
    "dispositions": [
        {"value": "accept", "resolves": ["d1"]},
        {"value": "reject", "resolves": []},
    ],
}


class TestDispositionResolvesDirective:
    def test_no_decision_entry_does_not_resolve(self):
        assert apply_base.disposition_resolves_directive(_JP_TWO_WAY, {}, "d1") is False

    def test_disposition_whose_resolves_names_the_directive(self):
        decisions = {"j1": {"disposition": "accept"}}
        assert apply_base.disposition_resolves_directive(_JP_TWO_WAY, decisions, "d1") is True

    def test_disposition_set_but_resolves_list_empty_does_not_resolve(self):
        # A disposition being PICKED is not sufficient — its own `resolves`
        # list must name the directive (the Director of Engineering v2 finding-1 predicate).
        decisions = {"j1": {"disposition": "reject"}}
        assert apply_base.disposition_resolves_directive(_JP_TWO_WAY, decisions, "d1") is False

    def test_disposition_resolving_a_different_directive_does_not_resolve_this_one(self):
        decisions = {"j1": {"disposition": "accept"}}
        assert apply_base.disposition_resolves_directive(_JP_TWO_WAY, decisions, "d2") is False


class TestDirectiveGateOpen:
    def test_no_depends_on_is_always_ready(self):
        ready, blocking = apply_base.directive_gate_open({"id": "d1"}, {}, {})
        assert ready is True
        assert blocking == []

    def test_unresolved_dependency_blocks(self):
        directive = {"id": "d1", "depends_on": "j1"}
        jp_by_id = {"j1": _JP_TWO_WAY}
        ready, blocking = apply_base.directive_gate_open(directive, jp_by_id, {})
        assert ready is False
        assert blocking == ["j1"]

    def test_resolved_dependency_opens_the_gate(self):
        directive = {"id": "d1", "depends_on": "j1"}
        jp_by_id = {"j1": _JP_TWO_WAY}
        decisions = {"j1": {"disposition": "accept"}}
        ready, blocking = apply_base.directive_gate_open(directive, jp_by_id, decisions)
        assert ready is True
        assert blocking == []

    def test_depends_on_naming_an_absent_judgment_point_never_gates(self):
        directive = {"id": "d1", "depends_on": "ghost"}
        ready, blocking = apply_base.directive_gate_open(directive, {}, {})
        assert ready is True
        assert blocking == []


# ---------------------------------------------------------------------------
# (e) execute_directives
# ---------------------------------------------------------------------------

class TestExecuteDirectives:
    def test_clean_run_dispatches_and_reports_landed(self, tmp_path):
        directives = [{"id": "d1", "cli": "noop-cli", "args": ["x"]}]
        exit_code, report = apply_base.execute_directives(
            directives, [], tmp_path, _DISPATCH_TABLE
        )
        assert exit_code == apply_base.APPLY_EXIT_OK
        assert report["landed"] == ["d1"]
        assert report["results"][0]["detail"] == {"args": ["x"], "repo_root": str(tmp_path)}

    def test_already_satisfied_is_skipped_without_dispatch(self, tmp_path):
        directives = [{"id": "d1", "cli": "raising-cli", "already_satisfied": True}]
        exit_code, report = apply_base.execute_directives(
            directives, [], tmp_path, _DISPATCH_TABLE
        )
        assert exit_code == apply_base.APPLY_EXIT_OK
        assert report["landed"] == ["d1"]
        assert report["results"][0]["already_satisfied"] is True
        assert report["results"][0]["detail"] is None

    def test_no_directives_no_judgment_points_is_a_clean_noop(self, tmp_path):
        exit_code, report = apply_base.execute_directives([], [], tmp_path, _DISPATCH_TABLE)
        assert exit_code == apply_base.APPLY_EXIT_OK
        assert report["landed"] == []

    def test_no_directives_with_judgment_points_halts(self, tmp_path):
        exit_code, report = apply_base.execute_directives(
            [], [{"id": "j1"}], tmp_path, _DISPATCH_TABLE
        )
        assert exit_code == apply_base.APPLY_EXIT_HALTED_AT_JUDGMENT
        assert report["unresolved_judgment_points"] == ["j1"]

    def test_per_directive_halt_lets_ready_directives_still_land(self, tmp_path):
        directives = [
            {"id": "d1", "cli": "noop-cli"},
            {"id": "d2", "cli": "noop-cli", "depends_on": "j1"},
        ]
        exit_code, report = apply_base.execute_directives(
            directives, [_JP_TWO_WAY], tmp_path, _DISPATCH_TABLE
        )
        assert exit_code == apply_base.APPLY_EXIT_HALTED_AT_JUDGMENT
        assert report["landed"] == ["d1"]
        assert report["unresolved_judgment_points"] == ["j1"]

    def test_recommendation_key_is_never_a_control_flow_input(self, tmp_path):
        directives = [{"id": "d1", "cli": "noop-cli", "depends_on": "j1"}]
        bare = [_JP_TWO_WAY]
        with_recommendation = [{**_JP_TWO_WAY, "recommendation": "do it anyway"}]

        exit_bare, report_bare = apply_base.execute_directives(
            directives, bare, tmp_path, _DISPATCH_TABLE
        )
        exit_reco, report_reco = apply_base.execute_directives(
            directives, with_recommendation, tmp_path, _DISPATCH_TABLE
        )
        assert exit_bare == exit_reco
        assert report_bare["landed"] == report_reco["landed"]

    def test_claim_denied_short_circuits_before_any_dispatch(self, tmp_path):
        directives = [{"id": "d1", "cli": "noop-cli"}]
        exit_code, report = apply_base.execute_directives(
            directives,
            [],
            tmp_path,
            _DISPATCH_TABLE,
            resolve_claim_grant=lambda: {"verdict": "denied"},
        )
        assert exit_code == apply_base.APPLY_EXIT_CLAIM_DENIED
        assert report["landed"] == []

    def test_granted_with_warning_verdict_still_dispatches(self, tmp_path):
        directives = [{"id": "d1", "cli": "noop-cli"}]
        exit_code, report = apply_base.execute_directives(
            directives,
            [],
            tmp_path,
            _DISPATCH_TABLE,
            resolve_claim_grant=lambda: {"verdict": "granted-with-warning"},
        )
        assert exit_code == apply_base.APPLY_EXIT_OK
        assert report["landed"] == ["d1"]

    def test_dependency_cycle_is_transport_fail_with_nothing_landed(self, tmp_path):
        directives = [
            {"id": "d1", "cli": "noop-cli", "depends_on": "d2"},
            {"id": "d2", "cli": "noop-cli", "depends_on": "d1"},
        ]
        exit_code, report = apply_base.execute_directives(
            directives, [], tmp_path, _DISPATCH_TABLE
        )
        assert exit_code == apply_base.APPLY_EXIT_TRANSPORT_FAIL
        assert report["landed"] == []

    def test_unrecognized_cli_aborts_the_whole_run_pre_validation(self, tmp_path):
        directives = [
            {"id": "d1", "cli": "noop-cli"},
            {"id": "d2", "cli": "rm"},
        ]
        exit_code, report = apply_base.execute_directives(
            directives, [], tmp_path, _DISPATCH_TABLE
        )
        assert exit_code == apply_base.APPLY_EXIT_TRANSPORT_FAIL
        # "mutates nothing" — d1 never dispatched despite being valid, because
        # the WHOLE directive list is pre-validated before any of it runs.
        assert report["landed"] == []

    def test_handler_exception_is_partial_mutation_with_prior_landed_preserved(self, tmp_path):
        directives = [
            {"id": "d1", "cli": "noop-cli"},
            {"id": "d2", "cli": "raising-cli"},
        ]
        exit_code, report = apply_base.execute_directives(
            directives, [], tmp_path, _DISPATCH_TABLE
        )
        assert exit_code == apply_base.APPLY_EXIT_PARTIAL_MUTATION
        assert report["landed"] == ["d1"]
        assert report["failed_directive"] == "d2"


# ---------------------------------------------------------------------------
# (e-2) execute_directives — the optional `compensators` seam. Additive and
# opt-in: every test in `TestExecuteDirectives` above already proves the
# byte-identical-when-omitted claim (none of them pass `compensators` at
# all). These tests exercise the seam itself, directly against the same
# synthetic dispatch table.
# ---------------------------------------------------------------------------

class TestExecuteDirectivesCompensators:
    def test_fires_on_partial_mutation_in_reverse_landing_order(self, tmp_path):
        directives = [
            {"id": "d1", "cli": "noop-cli"},
            {"id": "d2", "cli": "noop-cli"},
            {"id": "d3", "cli": "raising-cli"},
        ]
        order: list[str] = []
        compensators = {
            "d1": lambda directive, repo_root, detail: order.append("d1"),
            "d2": lambda directive, repo_root, detail: order.append("d2"),
        }
        exit_code, report = apply_base.execute_directives(
            directives, [], tmp_path, _DISPATCH_TABLE, compensators=compensators
        )
        assert exit_code == apply_base.APPLY_EXIT_PARTIAL_MUTATION
        assert order == ["d2", "d1"]  # reverse landing order
        assert report["compensation"] == [
            {"directive_id": "d2", "attempted": True, "succeeded": True},
            {"directive_id": "d1", "attempted": True, "succeeded": True},
        ]
        # the original failure signal is unchanged
        assert report["failed_directive"] == "d3"
        assert report["landed"] == ["d1", "d2"]

    def test_does_not_fire_on_ok(self, tmp_path):
        directives = [{"id": "d1", "cli": "noop-cli"}]
        called = {"count": 0}
        compensators = {"d1": lambda directive, repo_root, detail: called.__setitem__("count", 1)}
        exit_code, report = apply_base.execute_directives(
            directives, [], tmp_path, _DISPATCH_TABLE, compensators=compensators
        )
        assert exit_code == apply_base.APPLY_EXIT_OK
        assert called["count"] == 0
        assert "compensation" not in report

    def test_does_not_fire_on_halted_at_judgment(self, tmp_path):
        directives = [
            {"id": "d1", "cli": "noop-cli"},
            {"id": "d2", "cli": "noop-cli", "depends_on": "j1"},
        ]
        called = {"count": 0}
        compensators = {"d1": lambda directive, repo_root, detail: called.__setitem__("count", 1)}
        exit_code, report = apply_base.execute_directives(
            directives, [_JP_TWO_WAY], tmp_path, _DISPATCH_TABLE, compensators=compensators
        )
        assert exit_code == apply_base.APPLY_EXIT_HALTED_AT_JUDGMENT
        assert called["count"] == 0
        assert "compensation" not in report

    def test_never_compensates_an_already_satisfied_directive(self, tmp_path):
        directives = [
            {"id": "d1", "cli": "noop-cli", "already_satisfied": True},
            {"id": "d2", "cli": "raising-cli"},
        ]
        called = {"count": 0}
        compensators = {"d1": lambda directive, repo_root, detail: called.__setitem__("count", 1)}
        exit_code, report = apply_base.execute_directives(
            directives, [], tmp_path, _DISPATCH_TABLE, compensators=compensators
        )
        assert exit_code == apply_base.APPLY_EXIT_PARTIAL_MUTATION
        assert called["count"] == 0
        assert report["compensation"] == []

    def test_a_directive_with_no_registered_compensator_is_skipped(self, tmp_path):
        directives = [
            {"id": "d1", "cli": "noop-cli"},
            {"id": "d2", "cli": "raising-cli"},
        ]
        called = {"count": 0}
        # "d1" has no entry here — only an unrelated id is registered — so
        # the compensation pass must run (compensators is non-empty) but
        # find nothing to do for the one directive that actually landed.
        compensators = {"unrelated-id": lambda directive, repo_root, detail: called.__setitem__("count", 1)}
        exit_code, report = apply_base.execute_directives(
            directives, [], tmp_path, _DISPATCH_TABLE, compensators=compensators
        )
        assert exit_code == apply_base.APPLY_EXIT_PARTIAL_MUTATION
        assert called["count"] == 0
        assert report["compensation"] == []

    def test_raising_compensator_is_recorded_and_does_not_mask_original_error(self, tmp_path):
        directives = [
            {"id": "d1", "cli": "noop-cli"},
            {"id": "d2", "cli": "raising-cli"},
        ]

        def _raising_compensator(directive, repo_root, detail):
            raise RuntimeError("compensator boom")

        exit_code, report = apply_base.execute_directives(
            directives, [], tmp_path, _DISPATCH_TABLE, compensators={"d1": _raising_compensator}
        )
        assert exit_code == apply_base.APPLY_EXIT_PARTIAL_MUTATION
        assert report["error"] == "synthetic handler failure"
        assert report["failed_directive"] == "d2"
        assert report["compensation"] == [
            {"directive_id": "d1", "attempted": True, "succeeded": False, "error": "compensator boom"}
        ]

    def test_compensator_receives_the_directive_dict_and_detail(self, tmp_path):
        directives = [
            {"id": "d1", "cli": "noop-cli", "args": ["x"]},
            {"id": "d2", "cli": "raising-cli"},
        ]
        captured = {}

        def _compensator(directive, repo_root, detail):
            captured["directive"] = directive
            captured["repo_root"] = repo_root
            captured["detail"] = detail

        apply_base.execute_directives(
            directives, [], tmp_path, _DISPATCH_TABLE, compensators={"d1": _compensator}
        )
        assert captured["directive"] == directives[0]
        assert captured["repo_root"] == tmp_path
        assert captured["detail"] == {"args": ["x"], "repo_root": str(tmp_path)}


# ---------------------------------------------------------------------------
# (e-3) execute_directives — the per-directive `"advisory"` marker
# (AC3-AC6, docs/plans/2026-08-15-coverage-gate-advisory-failure-and-
# warn-flood.md chunk C2). Every test in `TestExecuteDirectives` above
# already proves the byte-identical-when-omitted claim (none of them set
# `"advisory"` on any directive). These tests exercise the marker itself.
# ---------------------------------------------------------------------------

class TestExecuteDirectivesAdvisory:
    def test_advisory_failure_does_not_take_the_run_to_partial_mutation(self, tmp_path):
        directives = [{"id": "d1", "cli": "raising-cli", "advisory": True}]
        exit_code, report = apply_base.execute_directives(
            directives, [], tmp_path, _DISPATCH_TABLE
        )
        assert exit_code != apply_base.APPLY_EXIT_PARTIAL_MUTATION
        assert exit_code == apply_base.APPLY_EXIT_OK

    def test_advisory_failure_does_not_block_remaining_directives(self, tmp_path):
        directives = [
            {"id": "d1", "cli": "raising-cli", "advisory": True},
            {"id": "d2", "cli": "noop-cli"},
        ]
        exit_code, report = apply_base.execute_directives(
            directives, [], tmp_path, _DISPATCH_TABLE
        )
        assert exit_code == apply_base.APPLY_EXIT_OK
        assert report["landed"] == ["d2"]

    def test_advisory_failure_is_named_in_the_report(self, tmp_path):
        directives = [{"id": "d1", "cli": "raising-cli", "advisory": True}]
        exit_code, report = apply_base.execute_directives(
            directives, [], tmp_path, _DISPATCH_TABLE
        )
        assert report["advisory_failures"] == [
            {"directive_id": "d1", "error": "synthetic handler failure"}
        ]

    def test_advisory_failure_does_not_invoke_a_registered_compensator(self, tmp_path):
        directives = [
            {"id": "d1", "cli": "noop-cli"},
            {"id": "d2", "cli": "raising-cli", "advisory": True},
        ]
        called: dict[str, int] = {}
        compensators = {"d1": lambda directive, repo_root, detail: called.__setitem__("count", 1)}
        exit_code, report = apply_base.execute_directives(
            directives, [], tmp_path, _DISPATCH_TABLE, compensators=compensators
        )
        assert exit_code == apply_base.APPLY_EXIT_OK
        assert "count" not in called
        assert "compensation" not in report

    def test_omitting_the_marker_is_the_pre_existing_partial_mutation_behaviour(self, tmp_path):
        directives = [{"id": "d1", "cli": "raising-cli"}]
        exit_code, report = apply_base.execute_directives(
            directives, [], tmp_path, _DISPATCH_TABLE
        )
        assert exit_code == apply_base.APPLY_EXIT_PARTIAL_MUTATION
        assert "advisory_failures" not in report

    def test_a_clean_run_with_no_advisory_directive_omits_the_key_entirely(self, tmp_path):
        directives = [{"id": "d1", "cli": "noop-cli"}]
        exit_code, report = apply_base.execute_directives(
            directives, [], tmp_path, _DISPATCH_TABLE
        )
        assert exit_code == apply_base.APPLY_EXIT_OK
        assert "advisory_failures" not in report

    def test_earlier_advisory_failure_survives_a_later_partial_mutation(self, tmp_path):
        # An advisory directive fails first (recorded, run continues), then
        # a later NON-advisory directive fails (returns PARTIAL_MUTATION).
        # The earlier advisory failure must still be named in the report —
        # dropping it here is exactly the silent-swallow AC4 exists to stop.
        directives = [
            {"id": "d1", "cli": "raising-cli", "advisory": True},
            {"id": "d2", "cli": "raising-cli"},
        ]
        exit_code, report = apply_base.execute_directives(
            directives, [], tmp_path, _DISPATCH_TABLE
        )
        assert exit_code == apply_base.APPLY_EXIT_PARTIAL_MUTATION
        assert report["failed_directive"] == "d2"
        assert report["advisory_failures"] == [
            {"directive_id": "d1", "error": "synthetic handler failure"}
        ]


# ---------------------------------------------------------------------------
# (f) assert_in_repo_root / reject_path_traversal / scoped_commit
# ---------------------------------------------------------------------------

class TestAssertInRepoRoot:
    def test_in_repo_relative_path_resolves(self, tmp_path):
        resolved = apply_base.assert_in_repo_root(Path("state/foo.md"), tmp_path)
        assert resolved == (tmp_path / "state/foo.md").resolve()

    def test_out_of_repo_path_raises(self, tmp_path):
        with pytest.raises(apply_base.OutOfRepoPath):
            apply_base.assert_in_repo_root(Path("../../etc/passwd"), tmp_path)


class TestRejectPathTraversal:
    def test_bare_segment_passes_through(self):
        assert apply_base.reject_path_traversal("h1.md", label="basename") == "h1.md"

    @pytest.mark.parametrize("value", ["", "..", ".", "a/b", "a\\b"])
    def test_traversal_shaped_values_raise(self, value):
        with pytest.raises(apply_base.OutOfRepoPath):
            apply_base.reject_path_traversal(value, label="basename")


class _RecordingRunGit:
    """A fake `run_git` callable recording every invocation — proves
    `scoped_commit` never shells out itself and always resolves `cwd` from
    the caller-supplied `repo_root`."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, args: list[str], cwd: Path):
        self.calls.append((args, cwd))
        if args[0] == "add":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:2] == ["diff", "--cached"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")  # "changed"
        if args[0] == "commit":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args == ["rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout="deadbeef" * 5 + "\n", stderr="")
        raise AssertionError(f"unexpected git invocation: {args}")


class TestScopedCommit:
    def test_stages_and_commits_via_explicit_pathspec_only(self, tmp_path):
        run_git = _RecordingRunGit()
        sha = apply_base.scoped_commit(tmp_path, "state/h1.md", "apply: d1", run_git)

        assert sha == "deadbeef" * 5
        add_args = run_git.calls[0][0]
        assert add_args[0] == "add"
        assert add_args[-2:] == ["--", str((tmp_path / "state/h1.md").resolve())]
        commit_args = run_git.calls[2][0]
        assert commit_args[:3] == ["commit", "-m", "apply: d1"]
        assert commit_args[-2:] == ["--", str((tmp_path / "state/h1.md").resolve())]
        for args, cwd in run_git.calls:
            assert cwd == tmp_path

    def test_empty_artifact_path_is_a_noop(self, tmp_path):
        run_git = _RecordingRunGit()
        assert apply_base.scoped_commit(tmp_path, "", "apply: d1", run_git) is None
        assert run_git.calls == []

    def test_nothing_staged_after_add_returns_none(self, tmp_path):
        class _NoChangeRunGit(_RecordingRunGit):
            def __call__(self, args, cwd):
                if args[:2] == ["diff", "--cached"]:
                    self.calls.append((args, cwd))
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                return super().__call__(args, cwd)

        run_git = _NoChangeRunGit()
        assert apply_base.scoped_commit(tmp_path, "state/h1.md", "apply: d1", run_git) is None
        assert not any(c[0][0] == "commit" for c in run_git.calls)

    def test_out_of_repo_artifact_path_raises_before_any_git_call(self, tmp_path):
        run_git = _RecordingRunGit()
        with pytest.raises(apply_base.OutOfRepoPath):
            apply_base.scoped_commit(tmp_path, "../../etc/passwd", "apply: d1", run_git)
        assert run_git.calls == []

    def test_add_failure_raises_runtime_error(self, tmp_path):
        def _failing_add(args, cwd):
            if args[0] == "add":
                return SimpleNamespace(returncode=1, stdout="", stderr="disk full")
            raise AssertionError(f"unexpected call after add failure: {args}")

        with pytest.raises(RuntimeError, match="git add"):
            apply_base.scoped_commit(tmp_path, "state/h1.md", "apply: d1", _failing_add)

    def test_non_lock_add_failure_fails_fast_with_zero_retries(self, tmp_path):
        # "disk full" is not lock-shaped stderr -- the AssertionError on any
        # call past the first `add` proves no retry was attempted.
        calls: list[list[str]] = []

        def _failing_add(args, cwd):
            calls.append(args)
            if args[0] == "add":
                return SimpleNamespace(returncode=1, stdout="", stderr="disk full")
            raise AssertionError(f"unexpected call after add failure: {args}")

        with pytest.raises(RuntimeError, match="git add"):
            apply_base.scoped_commit(tmp_path, "state/h1.md", "apply: d1", _failing_add)

        assert len(calls) == 1

    def test_held_lock_on_add_retries_and_completes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("coordinator_core.git_lock_retry.time.sleep", lambda *_a, **_k: None)
        add_calls: list[list[str]] = []

        def _flaky_run_git(args, cwd):
            if args[0] == "add":
                add_calls.append(args)
                if len(add_calls) == 1:
                    return SimpleNamespace(
                        returncode=128, stdout="",
                        stderr="fatal: Unable to create '.git/index.lock': File exists.",
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if args[:2] == ["diff", "--cached"]:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            if args[0] == "commit":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if args == ["rev-parse", "HEAD"]:
                return SimpleNamespace(returncode=0, stdout="deadbeef" * 5 + "\n", stderr="")
            raise AssertionError(f"unexpected git invocation: {args}")

        sha = apply_base.scoped_commit(tmp_path, "state/h1.md", "apply: d1", _flaky_run_git)

        assert sha == "deadbeef" * 5
        assert len(add_calls) == 2

    def test_held_lock_on_commit_retries_and_completes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("coordinator_core.git_lock_retry.time.sleep", lambda *_a, **_k: None)
        commit_calls: list[list[str]] = []

        def _flaky_run_git(args, cwd):
            if args[0] == "add":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if args[:2] == ["diff", "--cached"]:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            if args[0] == "commit":
                commit_calls.append(args)
                if len(commit_calls) == 1:
                    return SimpleNamespace(
                        returncode=128, stdout="",
                        stderr="fatal: Unable to create '.git/index.lock': File exists.",
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if args == ["rev-parse", "HEAD"]:
                return SimpleNamespace(returncode=0, stdout="deadbeef" * 5 + "\n", stderr="")
            raise AssertionError(f"unexpected git invocation: {args}")

        sha = apply_base.scoped_commit(tmp_path, "state/h1.md", "apply: d1", _flaky_run_git)

        assert sha == "deadbeef" * 5
        assert len(commit_calls) == 2

    def test_exhausted_lock_contention_on_add_still_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr("coordinator_core.git_lock_retry.time.sleep", lambda *_a, **_k: None)
        add_calls: list[list[str]] = []

        def _always_locked(args, cwd):
            if args[0] == "add":
                add_calls.append(args)
                return SimpleNamespace(
                    returncode=128, stdout="",
                    stderr="fatal: Unable to create '.git/index.lock': File exists.",
                )
            raise AssertionError(f"unexpected call: {args}")

        with pytest.raises(RuntimeError, match="git add"):
            apply_base.scoped_commit(tmp_path, "state/h1.md", "apply: d1", _always_locked)

        from coordinator_core.git_lock_retry import DEFAULT_MAX_ATTEMPTS

        assert len(add_calls) == DEFAULT_MAX_ATTEMPTS
