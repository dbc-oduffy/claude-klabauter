"""
coordinator_core.workstream_complete.test_apply — dispatch-mechanics tests
for the `workstream-complete` computed-skill engine's apply half (C4).

Purpose: exercises `apply.py`'s directive-execution seam directly against
small synthetic fake CLI modules (never the real `coordinator/bin/wsc-*`
scripts, which would touch a real git tree) so these tests stay fast,
deterministic, and independent of the invoking repo's state. Covers: the
closed-dispatch-table membership bound to `CONSUMES_MANIFEST`, the
unrecognized-cli raise, the zero-arg-`main()` `sys.argv` splice, and the
halt contract's exit-code ladder — asserted to be the SAME `IntEnum`
values `ceremony_common.apply_halt.build_ceremony_halt_exit_codes` hands
`workday_complete.apply`/`workweek_complete.apply`, never a locally
re-derived numbering (see `apply.py`'s Negative-spec).

Run scoped only:
    python3 -m pytest coordinator_core/workstream_complete/test_apply.py -q
Spec backlink: docs/plans/2026-07-26-workstream-complete-computed-frontage.md, chunk C4
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Optional

import pytest

from coordinator_core.ceremony_common.apply_halt import UnrecognizedDirective
from coordinator_core.workstream_complete import CONSUMES_MANIFEST, TransportFailure

# Real git spawn is load-bearing: the no-commit-row guard's commit-coverage
# oracle (close_out_and_stamp._determine_shipped, reused not reimplemented)
# reads real git commit history to decide coverage — no mock stands in for
# git's own log here. Per-test repos stay per-test: each case's commit
# history must not leak across tests.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]
from coordinator_core.workstream_complete import apply as ws_apply
from coordinator_core.workstream_complete import directives_session_hygiene


def _fake_module(main_fn: Callable[..., Any], name: str = "fake_cli") -> ModuleType:
    mod = ModuleType(name)
    mod.main = main_fn
    return mod


def _directive(
    id: str,
    cli: str,
    *,
    args: Optional[list[str]] = None,
    depends_on: Optional[str | list[str]] = None,
    already_satisfied: bool = False,
) -> dict[str, Any]:
    """Mirrors `__init__.py`'s `_directive` output shape without importing
    it — this file tests the apply-side consumer of that shape, not the
    assembler that builds it (that's `test_workstream_complete_contract.
    py`'s job). `depends_on` accepts `str | list[str]` to match what
    `_normalize_depends_on` (`ceremony_common/apply_halt.py`) actually
    accepts. Review: coordinator:code-reviewer — Finding 2."""
    return {
        "id": id,
        "cli": cli,
        "args": args if args is not None else [],
        "depends_on": depends_on,
        "already_satisfied": already_satisfied,
    }


# ---------------------------------------------------------------------------
# Closed dispatch table — bound to CONSUMES_MANIFEST (C1's single oracle)
# ---------------------------------------------------------------------------


def test_cli_dispatch_keys_exactly_match_consumes_manifest() -> None:
    """The dispatch table is closed over the FULL manifest — not the four
    "legacy" names the C4 plan text pinned before C3 landed the other 16
    (see `apply.py`'s module docstring, deviation 2)."""
    assert set(ws_apply._CLI_DISPATCH) == set(CONSUMES_MANIFEST)
    assert len(ws_apply._CLI_DISPATCH) == len(CONSUMES_MANIFEST)


def test_legacy_convert2_names_are_a_subset_of_the_manifest() -> None:
    """The four CLI names the plan body pins by hand are still individually
    correct `CONSUMES_MANIFEST` members — just not an exhaustive dispatch
    boundary any more."""
    legacy = {
        "wsc-coverage-gate-runner",
        "check-workstream-complete-deletion-blocks",
        "wsc-close",
        "wsc-tail",
    }
    assert legacy <= set(CONSUMES_MANIFEST)
    assert legacy <= set(ws_apply._CLI_DISPATCH)


def test_dispatch_table_values_are_path_objects_under_coordinator_bin() -> None:
    for path in ws_apply._CLI_DISPATCH.values():
        assert isinstance(path, Path)
        assert path.parent.name == "bin"


def test_resolve_cli_raises_unrecognized_directive_for_unknown_name() -> None:
    with pytest.raises(UnrecognizedDirective):
        ws_apply._resolve_cli("not-a-real-cli")


def test_dispatch_directive_never_dispatches_before_raising_on_unrecognized_cli() -> None:
    """An unrecognized `cli` must raise (via `_load_cli_module` ->
    `_resolve_cli`) before any real script is ever loaded/executed — this
    exercises the real (unpatched) `_load_cli_module`, not a stub, so the
    raise is confirmed to happen at the actual dispatch seam."""
    assert "not-a-real-cli" not in ws_apply._LOADED_MODULES
    with pytest.raises(UnrecognizedDirective):
        ws_apply._dispatch_directive(_directive("d_bad", "not-a-real-cli"))
    assert "not-a-real-cli" not in ws_apply._LOADED_MODULES


# ---------------------------------------------------------------------------
# _invoke_cli_main — argv-taking vs zero-arg main() splice
# ---------------------------------------------------------------------------


def test_invoke_cli_main_calls_argv_taking_main_with_args() -> None:
    seen: dict[str, list[str]] = {}

    def main_fn(argv: list[str]) -> int:
        seen["argv"] = argv
        return 0

    exit_code, stdout_text, stderr_text = ws_apply._invoke_cli_main(_fake_module(main_fn), ["--flag", "value"])
    assert exit_code == 0
    assert stdout_text == ""
    assert stderr_text == ""
    assert seen["argv"] == ["--flag", "value"]


def test_invoke_cli_main_splices_args_into_sys_argv_for_zero_arg_main() -> None:
    """Zero-arg `main()` trampolines parse `sys.argv` themselves (census § B
    item 5's real, previously-hit bug class) — `args` must land on
    `sys.argv[1:]`, and `sys.argv` must be restored afterward."""
    sentinel_argv = list(sys.argv)
    seen: dict[str, list[str]] = {}

    def main_fn() -> int:
        seen["argv"] = list(sys.argv)
        return 0

    exit_code, stdout_text, stderr_text = ws_apply._invoke_cli_main(_fake_module(main_fn), ["--mode", "pending"])
    assert exit_code == 0
    assert stdout_text == ""
    assert stderr_text == ""
    assert seen["argv"][1:] == ["--mode", "pending"]
    assert sys.argv == sentinel_argv


def test_invoke_cli_main_restores_sys_argv_after_exception() -> None:
    sentinel_argv = list(sys.argv)

    def main_fn() -> int:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        ws_apply._invoke_cli_main(_fake_module(main_fn), ["--x"])
    assert sys.argv == sentinel_argv


def test_invoke_cli_main_resolves_system_exit_int_code() -> None:
    def main_fn(argv: list[str]) -> None:
        raise SystemExit(7)

    exit_code, _stdout_text, _stderr_text = ws_apply._invoke_cli_main(_fake_module(main_fn), [])
    assert exit_code == 7


def test_invoke_cli_main_captures_stdout() -> None:
    def main_fn(argv: list[str]) -> int:
        print("hello-from-cli")
        return 0

    exit_code, stdout_text, stderr_text = ws_apply._invoke_cli_main(_fake_module(main_fn), [])
    assert exit_code == 0
    assert stdout_text == "hello-from-cli\n"
    assert stderr_text == ""


def test_invoke_cli_main_captures_stderr() -> None:
    """2026-07-27 finding: a non-zero directive's diagnostic text (e.g.
    `wsc-tail.py`'s exit-2 diagnostics block, printed unconditionally to
    `sys.stderr`) must be captured the same way stdout already is — prior
    to this fix, `_invoke_cli_main` only redirected stdout, so this text
    was neither captured nor threaded into `_dispatch_directive`'s result
    dict at all."""
    import sys as _sys

    def main_fn(argv: list[str]) -> int:
        print("diagnostic detail", file=_sys.stderr)
        return 2

    exit_code, stdout_text, stderr_text = ws_apply._invoke_cli_main(_fake_module(main_fn), [])
    assert exit_code == 2
    assert stdout_text == ""
    assert stderr_text == "diagnostic detail\n"


def test_invoke_cli_main_no_main_raises_unrecognized_directive() -> None:
    mod = ModuleType("no_main_cli")
    with pytest.raises(UnrecognizedDirective):
        ws_apply._invoke_cli_main(mod, [])


# ---------------------------------------------------------------------------
# _execute_directives — the halt contract (composed from ceremony_common)
# ---------------------------------------------------------------------------


def test_execute_directives_all_landed_is_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def ok_main(argv: list[str]) -> int:
        return 0

    modules = {"fake-a": _fake_module(ok_main, "fake_a"), "fake-b": _fake_module(ok_main, "fake_b")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_a", "fake-a"), _directive("d_b", "fake-b")]
    exit_code, report = ws_apply._execute_directives(directives, [], {})

    assert report["landed"] == ["d_a", "d_b"]
    assert report["blocked"] == []
    assert report["failed"] == []
    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.SUCCESS)


def test_execute_directives_already_satisfied_lands_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_load(cli_name: str) -> ModuleType:
        raise AssertionError("an already_satisfied directive must never dispatch")

    monkeypatch.setattr(ws_apply, "_load_cli_module", fake_load)
    directives = [_directive("d_done", "fake-a", already_satisfied=True)]
    exit_code, report = ws_apply._execute_directives(directives, [], {})

    assert report["landed"] == ["d_done"]
    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.SUCCESS)


def test_execute_directives_blocked_judgment_point_never_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_load(cli_name: str) -> ModuleType:
        raise AssertionError("a blocked directive must never dispatch")

    monkeypatch.setattr(ws_apply, "_load_cli_module", fake_load)
    judgment_points = [{"id": "jp_gate", "dispositions": [{"value": "go", "resolves": ["d_gated"]}]}]
    directives = [_directive("d_gated", "fake-a", depends_on="jp_gate")]
    # No decision supplied for jp_gate -> gate stays closed.
    exit_code, report = ws_apply._execute_directives(directives, judgment_points, {})

    assert report["blocked"] == ["d_gated"]
    assert report["landed"] == []
    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.HALTED_AT_JUDGMENT)


def test_execute_directives_blocked_directive_gets_a_blocked_remedy_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4 (docs/plans/2026-07-29-workstream-complete-the-envelope-names-t.md):
    a blocked directive's id appears in `report["blocked_remedy"]`, naming
    the gating judgment-point id and the disposition values whose OWN
    `resolves` names the blocked directive — derived entirely from the
    `judgment_points[]` already passed in, no new computation."""

    def fake_load(cli_name: str) -> ModuleType:
        raise AssertionError("a blocked directive must never dispatch")

    monkeypatch.setattr(ws_apply, "_load_cli_module", fake_load)
    judgment_points = [
        {
            "id": "jp_gate",
            "dispositions": [
                {"value": "go", "resolves": ["d_gated"]},
                {"value": "wait", "resolves": []},
            ],
        }
    ]
    directives = [_directive("d_gated", "fake-a", depends_on="jp_gate")]
    exit_code, report = ws_apply._execute_directives(directives, judgment_points, {})

    assert report["blocked"] == ["d_gated"]
    assert report["blocked_remedy"] == {
        "d_gated": {"judgment_point_id": "jp_gate", "dispositions": ["go"]}
    }
    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.HALTED_AT_JUDGMENT)


def test_execute_directives_blocked_remedy_names_the_disposition_that_missed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chosen-but-non-resolving disposition (the gate re-checks VALUE, not
    merely presence) still surfaces the correct remedy — the full set of
    dispositions that WOULD resolve this directive, not just the one the
    caller already (wrongly) picked."""

    def fake_load(cli_name: str) -> ModuleType:
        raise AssertionError("a blocked directive must never dispatch")

    monkeypatch.setattr(ws_apply, "_load_cli_module", fake_load)
    judgment_points = [
        {
            "id": "jp_gate",
            "dispositions": [
                {"value": "go", "resolves": ["d_gated"]},
                {"value": "wait", "resolves": []},
            ],
        }
    ]
    directives = [_directive("d_gated", "fake-a", depends_on="jp_gate")]
    decisions = {"jp_gate": {"disposition": "wait"}}
    exit_code, report = ws_apply._execute_directives(directives, judgment_points, decisions)

    assert report["blocked"] == ["d_gated"]
    assert report["blocked_remedy"]["d_gated"] == {
        "judgment_point_id": "jp_gate",
        "dispositions": ["go"],
    }


def test_execute_directives_landed_directive_has_no_blocked_remedy_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def ok_main(argv: list[str]) -> int:
        return 0

    modules = {"fake-a": _fake_module(ok_main, "fake_a")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_ok", "fake-a")]
    exit_code, report = ws_apply._execute_directives(directives, [], {})

    assert report["landed"] == ["d_ok"]
    assert report["blocked_remedy"] == {}


def test_render_blocked_remedy_lines_matches_the_fixed_ac5_grammar() -> None:
    """AC5: `BLOCKED <directive-id> — set decisions["<jp-id>"].disposition
    to one of: <values>`."""
    blocked_remedy = {
        "d_gated": {"judgment_point_id": "jp_gate", "dispositions": ["go", "wait-longer"]},
    }
    lines = ws_apply.render_blocked_remedy_lines(blocked_remedy)
    assert lines == [
        'BLOCKED d_gated — set decisions["jp_gate"].disposition to one of: go, wait-longer'
    ]


def test_render_blocked_remedy_lines_skips_entries_with_no_nameable_judgment_point() -> None:
    blocked_remedy = {"d_gated": {"judgment_point_id": None, "dispositions": []}}
    assert ws_apply.render_blocked_remedy_lines(blocked_remedy) == []


def test_main_prints_a_blocked_remedy_line_for_each_blocked_directive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC5, end to end through `main()`'s stdout: exit-code behavior is
    unchanged (`HALTED_AT_JUDGMENT` still 1) while the human-readable
    remedy line is ALSO printed."""
    judgment_points = [
        {"id": "jp_gate", "dispositions": [{"value": "go", "resolves": ["d_gated"]}]}
    ]
    directives = [_directive("d_gated", "fake-a", depends_on="jp_gate")]
    envelope = {
        "artifact": {},
        "preflight": {},
        "gates": {},
        "directives": directives,
        "judgment_points": judgment_points,
        "decisions": {},
        "narration": "",
        "next_move": "",
    }
    monkeypatch.setattr(ws_apply, "brief", lambda decisions=None: envelope)

    exit_code = ws_apply.main([])

    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.HALTED_AT_JUDGMENT)
    captured = capsys.readouterr()
    assert 'BLOCKED d_gated — set decisions["jp_gate"].disposition to one of: go' in captured.out


def test_execute_directives_resolved_disposition_opens_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def ok_main(argv: list[str]) -> int:
        return 0

    modules = {"fake-a": _fake_module(ok_main, "fake_a")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    judgment_points = [{"id": "jp_gate", "dispositions": [{"value": "go", "resolves": ["d_gated"]}]}]
    directives = [_directive("d_gated", "fake-a", depends_on="jp_gate")]
    decisions = {"jp_gate": {"disposition": "go"}}
    exit_code, report = ws_apply._execute_directives(directives, judgment_points, decisions)

    assert report["landed"] == ["d_gated"]
    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.SUCCESS)


def test_execute_directives_nonzero_exit_is_failed_not_landed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directive whose CLI exits non-zero (an argparse usage error, a
    gate's business-fail, etc.) must never read as success merely because
    dispatch did not raise a Python exception."""

    def failing_main(argv: list[str]) -> int:
        return 2

    modules = {"fake-a": _fake_module(failing_main, "fake_a")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_fail", "fake-a")]
    exit_code, report = ws_apply._execute_directives(directives, [], {})

    assert report["landed"] == []
    assert [entry["id"] for entry in report["failed"]] == ["d_fail"]
    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.DIRECTIVE_FAILED)


def test_harvest_deferrals_nonzero_exit_reaches_the_ceremony_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The harvest's fail-loud must survive WSC, not be rendered and dropped.

    `coordinator-harvest-deferrals` exits 1 when it skips a `pm_approved`
    row it cannot route (2026-07-29). WSC is the belt-and-suspenders
    call-site and the ONLY one that runs for a row which flipped to
    deferred after Phase 1.6 already harvested — so if this exit were
    swallowed here, the fail-loud would be inert on exactly the path it
    was built for.

    The generic non-zero case is covered above; this test pins the harvest
    directive specifically, because `directives_lessons_plan.
    build_deferral_harvest_directives` describes the sweep as "advisory"
    and that word was read off-repo as "failure is cosmetic". The claim in
    that docstring is now executable rather than prose: advisory means the
    ceremony does not ABORT, and the exit code still propagates.
    """

    def harvest_skipping_pm_approved_row(argv: list[str]) -> int:
        return 1

    modules = {
        "coordinator-harvest-deferrals": _fake_module(
            harvest_skipping_pm_approved_row, "fake_harvest"
        )
    }
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d-harvest-deferrals-1", "coordinator-harvest-deferrals")]
    exit_code, report = ws_apply._execute_directives(directives, [], {})

    assert report["landed"] == []
    assert [entry["id"] for entry in report["failed"]] == ["d-harvest-deferrals-1"]
    assert exit_code != int(ws_apply.WorkstreamApplyExitCode.SUCCESS)
    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.DIRECTIVE_FAILED)


def test_harvest_deferrals_failure_does_not_abort_sibling_directives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"Advisory" means per-directive halt — a failed harvest must not stop
    siblings dispatching, while the run still exits non-zero. This is the
    other half of the docstring claim, and the reason the ceremony reports
    PARTIAL_MUTATION rather than DIRECTIVE_FAILED when work did land."""

    def failing_harvest(argv: list[str]) -> int:
        return 1

    def landing_sibling(argv: list[str]) -> int:
        return 0

    modules = {
        "coordinator-harvest-deferrals": _fake_module(failing_harvest, "fake_harvest"),
        "coordinator-lesson-add": _fake_module(landing_sibling, "fake_lesson_add"),
    }
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [
        _directive("d-harvest-deferrals-1", "coordinator-harvest-deferrals"),
        _directive("d-lesson-add-1", "coordinator-lesson-add"),
    ]
    exit_code, report = ws_apply._execute_directives(directives, [], {})

    assert report["landed"] == ["d-lesson-add-1"]
    assert [entry["id"] for entry in report["failed"]] == ["d-harvest-deferrals-1"]
    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.PARTIAL_MUTATION)


def test_execute_directives_raising_dispatch_is_failed_not_landed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raising_load(cli_name: str) -> ModuleType:
        raise FileNotFoundError("no such script")

    monkeypatch.setattr(ws_apply, "_load_cli_module", raising_load)
    directives = [_directive("d_missing", "scan_unresolved_ubt_records")]
    exit_code, report = ws_apply._execute_directives(directives, [], {})

    assert report["landed"] == []
    assert [entry["id"] for entry in report["failed"]] == ["d_missing"]
    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.DIRECTIVE_FAILED)


def test_execute_directives_partial_mutation_when_some_land_some_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def ok_main(argv: list[str]) -> int:
        return 0

    def failing_main(argv: list[str]) -> int:
        return 1

    modules = {"fake-ok": _fake_module(ok_main, "fake_ok"), "fake-fail": _fake_module(failing_main, "fake_fail")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_ok", "fake-ok"), _directive("d_fail", "fake-fail")]
    exit_code, report = ws_apply._execute_directives(directives, [], {})

    assert report["landed"] == ["d_ok"]
    assert [entry["id"] for entry in report["failed"]] == ["d_fail"]
    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.PARTIAL_MUTATION)


def test_execute_directives_one_failure_does_not_block_other_ready_directives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The halt is PER-DIRECTIVE — a failing entry must not stop later ready
    directives in the same pass from dispatching."""
    order: list[str] = []

    def failing_main(argv: list[str]) -> int:
        order.append("fail")
        return 1

    def ok_main(argv: list[str]) -> int:
        order.append("ok")
        return 0

    modules = {"fake-fail": _fake_module(failing_main, "fake_fail"), "fake-ok": _fake_module(ok_main, "fake_ok")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_fail", "fake-fail"), _directive("d_ok", "fake-ok")]
    ws_apply._execute_directives(directives, [], {})

    assert order == ["fail", "ok"]


# ---------------------------------------------------------------------------
# jp-consumed-handoff-completeness — AC5/AC6/AC7 (2026-08-05-session-shape-
# attribution-structural-gate, C3's redirect). Pinned here, not by asserting
# `depends_on` field values in `test_workstream_complete.py` — that only
# proves the edges EXIST, not that they actually block anything (or that the
# arg-token consumer `d-reconcile-completion-commits` doesn't get stranded
# into `report["failed"]` when its own gate AND its producer's gate are both
# closed). Mirrors `build_consumed_handoff_completeness_judgment_point`'s own
# `resolves` shape exactly, rather than a hand-invented stand-in.
# ---------------------------------------------------------------------------

_CONSUMED_HANDOFF_COMPLETENESS_RESOLVED_IDS = (
    "d-run-wsc-tail",
    "d-claim-plan-execution-lock",
    "d-stamp-plan-implemented",
    "d-harvest-deferrals-1",
    "d-complete-entry",
    "d-reconcile-completion-commits",
)


def _consumed_handoff_completeness_fixture() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The six gated directives plus one unaffected sibling (`d-coverage-gate`,
    which stays independently keyed to `jp-session-shape` — AC6's negative
    half) and the real `jp-consumed-handoff-completeness` judgment point
    shape (`build_consumed_handoff_completeness_judgment_point`'s own
    `resolves` lists, copied verbatim rather than re-derived)."""
    jp = {
        "id": "jp-consumed-handoff-completeness",
        "dispositions": [
            {
                "value": "override-known-in-flight",
                "resolves": list(_CONSUMED_HANDOFF_COMPLETENESS_RESOLVED_IDS),
            },
            {"value": "stop-and-handoff", "resolves": []},
        ],
    }
    directives = [
        _directive(
            "d-claim-plan-execution-lock",
            "wsc-coverage-gate-runner",
            depends_on="jp-consumed-handoff-completeness",
        ),
        _directive(
            "d-stamp-plan-implemented",
            "wsc-coverage-gate-runner",
            depends_on="jp-consumed-handoff-completeness",
        ),
        _directive(
            "d-harvest-deferrals-1",
            "coordinator-harvest-deferrals",
            depends_on="jp-consumed-handoff-completeness",
        ),
        _directive(
            "d-complete-entry",
            "coordinator-complete-entry",
            depends_on="jp-consumed-handoff-completeness",
        ),
        _directive(
            "d-reconcile-completion-commits",
            "reconcile-completion-commits",
            args=["--append", "{d-complete-entry.entry_path}"],
            depends_on=["jp-consumed-handoff-completeness", "d-complete-entry"],
        ),
        _directive(
            "d-run-wsc-tail",
            "wsc-tail",
            depends_on="jp-consumed-handoff-completeness",
        ),
        # Deliberately a DIFFERENT (fake) cli name than the two gated
        # `wsc-coverage-gate-runner` directives above, purely so the two
        # halves of this fixture can be dispatched (or not) independently in
        # the tests below without one `_load_cli_module` fake having to
        # discriminate by directive id.
        _directive("d-coverage-gate", "wsc-coverage-gate-runner-standalone"),
    ]
    return directives, [jp]


@pytest.mark.parametrize("decisions", [{}, {"jp-consumed-handoff-completeness": {"disposition": "stop-and-handoff"}}])
def test_consumed_handoff_completeness_default_blocks_all_six_and_fails_none(
    monkeypatch: pytest.MonkeyPatch, decisions: dict[str, Any]
) -> None:
    """AC5: by default (unresolved OR resolved stop-and-handoff), all six
    directives stay blocked, the run halts at judgment, and — the assertion
    that catches the arg-token failure mode d-reconcile-completion-commits's
    `{d-complete-entry.entry_path}` token could otherwise produce — NONE of
    the six lands in `report["failed"]`."""

    def fake_load(cli_name: str) -> ModuleType:
        if cli_name == "wsc-coverage-gate-runner-standalone":
            return _fake_module(lambda argv: 0, "fake_coverage_gate_runner_standalone")
        raise AssertionError(f"a blocked directive must never dispatch: {cli_name}")

    monkeypatch.setattr(ws_apply, "_load_cli_module", fake_load)
    directives, judgment_points = _consumed_handoff_completeness_fixture()

    exit_code, report = ws_apply._execute_directives(directives, judgment_points, decisions)

    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.HALTED_AT_JUDGMENT)
    for directive_id in _CONSUMED_HANDOFF_COMPLETENESS_RESOLVED_IDS:
        assert directive_id in report["blocked"], directive_id
    failed_ids = {entry["id"] for entry in report["failed"]}
    assert not (failed_ids & set(_CONSUMED_HANDOFF_COMPLETENESS_RESOLVED_IDS)), report["failed"]


def test_consumed_handoff_completeness_override_known_in_flight_clears_all_six(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC6: selecting `override-known-in-flight` clears all six directives."""

    def producer_main(argv: list[str]) -> int:
        print("archive/completed/2026-07/entry.md")
        return 0

    modules = {
        "wsc-coverage-gate-runner": _fake_module(lambda argv: 0, "fake_coverage_gate_runner"),
        "wsc-coverage-gate-runner-standalone": _fake_module(lambda argv: 0, "fake_coverage_gate_runner_standalone"),
        "coordinator-harvest-deferrals": _fake_module(lambda argv: 0, "fake_harvest"),
        "coordinator-complete-entry": _fake_module(producer_main, "fake_complete_entry"),
        "reconcile-completion-commits": _fake_module(lambda argv: 0, "fake_reconcile"),
        "wsc-tail": _fake_module(lambda argv: 0, "fake_tail"),
    }
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])
    directives, judgment_points = _consumed_handoff_completeness_fixture()
    decisions = {"jp-consumed-handoff-completeness": {"disposition": "override-known-in-flight"}}

    exit_code, report = ws_apply._execute_directives(directives, judgment_points, decisions)

    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.SUCCESS)
    for directive_id in _CONSUMED_HANDOFF_COMPLETENESS_RESOLVED_IDS:
        assert directive_id in report["landed"], directive_id
    assert report["blocked"] == []
    assert report["failed"] == []


def test_consumed_handoff_completeness_does_not_gate_coverage_gate_directive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC6's negative half: `d-coverage-gate` carries no `depends_on` edge
    onto `jp-consumed-handoff-completeness` — it stays independently keyed
    to `jp-session-shape` exactly as before this plan. Proven here by
    landing it with NO decision supplied for the completeness point at all,
    which would block every other directive in the fixture."""

    def fake_load(cli_name: str) -> ModuleType:
        if cli_name != "wsc-coverage-gate-runner-standalone":
            raise AssertionError(f"only d-coverage-gate's cli may dispatch: {cli_name}")
        return _fake_module(lambda argv: 0, "fake_coverage_gate_runner_standalone")

    monkeypatch.setattr(ws_apply, "_load_cli_module", fake_load)
    directives, judgment_points = _consumed_handoff_completeness_fixture()

    exit_code, report = ws_apply._execute_directives(directives, judgment_points, {})

    assert "d-coverage-gate" in report["landed"]
    assert "d-coverage-gate" not in report["blocked"]


# ---------------------------------------------------------------------------
# Exit-code ladder — composed from ceremony_common.apply_halt, never
# re-derived locally (shared numbering across workday/workweek/workstream).
# ---------------------------------------------------------------------------


def test_exit_code_ladder_matches_the_shared_ceremony_halt_numbering() -> None:
    from coordinator_core.workday_complete.apply import WorkdayApplyExitCode
    from coordinator_core.workweek_complete.apply import WorkweekApplyExitCode

    for member in ("SUCCESS", "HALTED_AT_JUDGMENT", "DIRECTIVE_FAILED", "TRANSPORT_FAIL", "PARTIAL_MUTATION"):
        assert int(getattr(ws_apply.WorkstreamApplyExitCode, member)) == int(
            getattr(WorkdayApplyExitCode, member)
        )
        assert int(getattr(ws_apply.WorkstreamApplyExitCode, member)) == int(
            getattr(WorkweekApplyExitCode, member)
        )


# ---------------------------------------------------------------------------
# apply() — the brief()-adapting entrypoint (deviation 1: TransportFailure,
# not a returned exit code, per apply.py's module docstring)
# ---------------------------------------------------------------------------


def test_apply_degrades_transport_failure_to_transport_fail_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raising_brief(decisions: Optional[dict[str, Any]] = None) -> Any:
        raise TransportFailure("could not resolve a git worktree root")

    monkeypatch.setattr(ws_apply, "brief", raising_brief)
    exit_code, report = ws_apply.apply(decisions=None)

    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.TRANSPORT_FAIL)
    assert report["landed"] == []
    assert "could not resolve a git worktree root" in report["error"]


def test_apply_executes_directives_from_a_successful_brief(monkeypatch: pytest.MonkeyPatch) -> None:
    def ok_main(argv: list[str]) -> int:
        return 0

    modules = {"fake-a": _fake_module(ok_main, "fake_a")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    def fake_brief(decisions: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return {
            "directives": [_directive("d_a", "fake-a")],
            "judgment_points": [],
            "decisions": decisions or {},
        }

    monkeypatch.setattr(ws_apply, "brief", fake_brief)
    exit_code, report = ws_apply.apply(decisions={"jp_x": {"disposition": "go"}})

    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.SUCCESS)
    assert report["landed"] == ["d_a"]


# ---------------------------------------------------------------------------
# jp-completion-entry-scaffold halt (state/bug-backlog/2026-07-28-
# workstream-complete-apply-re-scaffolds-t-e925d597e0af.yaml) — apply must
# HALT before d-run-wsc-tail (never dispatch it) while the completion entry
# is still a scaffold, via the existing HALTED_AT_JUDGMENT contract. The two
# hand-built-envelope tests below feed `apply()` the EXACT shape
# `workstream_complete.__init__.brief()` produces (a `d-run-wsc-tail`
# directive whose `depends_on` names the synthetic judgment point, plus that
# judgment point itself, structurally unresolvable via `decisions`) — that
# pins `apply()`'s halt MECHANISM given the shape, but neither drives the
# real `brief()` that PRODUCES the shape; `test_apply_halts_before_wsc_
# tail_via_real_brief_against_a_stood_down_chain_entry` below closes that
# gap end-to-end.
# ---------------------------------------------------------------------------


def test_apply_halts_before_wsc_tail_while_completion_entry_still_scaffolded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import coordinator_core.workstream_complete as wsc

    def dispatched_main(argv: list[str]) -> int:
        raise AssertionError(f"d-run-wsc-tail must never dispatch while blocked; got argv={argv!r}")

    modules = {"wsc-close": _fake_module(lambda argv: 0, "wsc_close")}
    monkeypatch.setattr(
        ws_apply,
        "_load_cli_module",
        lambda cli_name: modules.get(cli_name) or _fake_module(dispatched_main, cli_name),
    )

    scaffold_jp = wsc.build_completion_entry_scaffold_judgment_point(
        "archive/completed/2026-07/2026-07-28-adhoc-abcdef.md", ("title", "nature", "prose")
    )
    close_tail_args = _directive("d-close-tail-args", "wsc-close", args=["tail-args"])
    wsc_tail = _directive(
        "d-run-wsc-tail", "wsc-tail", args=["--sid", "abcdef"], depends_on=["d-close-tail-args", "jp-completion-entry-scaffold"]
    )

    def fake_brief(decisions: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return {
            "directives": [close_tail_args, wsc_tail],
            "judgment_points": [scaffold_jp],
            "decisions": decisions or {},
        }

    monkeypatch.setattr(ws_apply, "brief", fake_brief)
    exit_code, report = ws_apply.apply(decisions={})

    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.HALTED_AT_JUDGMENT)
    assert "d-run-wsc-tail" in report["blocked"]
    assert "d-run-wsc-tail" not in report["landed"]
    assert "d-close-tail-args" in report["landed"]


def test_apply_fires_wsc_tail_once_completion_entry_is_fully_authored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mirror case: once `directives_completion.compute_completion_
    entry_scaffold_gate` stops firing (entry fully authored), `brief()`
    no longer emits `jp-completion-entry-scaffold` at all and `d-run-wsc-
    tail`'s `depends_on` reverts to just the pre-existing ordering member —
    apply proceeds to dispatch it."""
    landed_ids: list[str] = []

    def ok_main(argv: list[str]) -> int:
        landed_ids.append(tuple(argv))
        return 0

    modules = {"wsc-close": _fake_module(ok_main, "wsc_close"), "wsc-tail": _fake_module(ok_main, "wsc_tail")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    close_tail_args = _directive("d-close-tail-args", "wsc-close", args=["tail-args"])
    wsc_tail = _directive("d-run-wsc-tail", "wsc-tail", args=["--sid", "abcdef", "--subject", "a subject"], depends_on="d-close-tail-args")

    def fake_brief(decisions: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return {
            "directives": [close_tail_args, wsc_tail],
            "judgment_points": [],
            "decisions": decisions or {},
        }

    monkeypatch.setattr(ws_apply, "brief", fake_brief)
    exit_code, report = ws_apply.apply(decisions={})

    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.SUCCESS)
    assert report["landed"] == ["d-close-tail-args", "d-run-wsc-tail"]
    assert len(landed_ids) == 2


def test_apply_halts_before_wsc_tail_via_real_brief_against_a_stood_down_chain_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: drives the REAL `workstream_complete.brief()` (not a
    hand-built envelope) against a realistic `archive/completed/` fixture,
    through `apply()`'s real halt path. The two hand-built-envelope tests
    above pin `apply()`'s halt MECHANISM given a shape someone else already
    produced; this test pins the WIRING from `brief()`'s scaffold-gate
    output into `apply()`'s halt, against the exact chain-terminal
    stand-down fixture shape (a completion entry already authored from a
    prior day/session) that state/bug-backlog/2026-07-28-workstream-
    complete-apply-re-scaffolds-t-e925d597e0af.yaml's stubbed tests never
    exercised — closing the gap that let that regression ship unguarded."""
    import coordinator_core.workstream_complete as wsc

    # single-session, not chain-terminal: chain-terminal pulls in a raft of
    # unrelated untrusted-gate judgment points (coverage gate, review
    # dispatch, ...) that would also halt apply() and swamp what this test
    # targets. The scaffold-gate/stand-down mechanism under test keys only
    # on `chain_slug` truthiness, never on disposition — a chain slug can be
    # (and here is) supplied on a single-session close.
    (tmp_path / "archive").mkdir()
    monkeypatch.setattr(
        wsc,
        "compute_session_shape_gate",
        lambda root: wsc.SessionShapeGate(
            sid="testsid123",
            disposition="single-session",
            consumed_handoff="",
            diagnostics=[],
            consumed_handoff_paths=(),
        ),
    )

    chain_slug = "some-plan"
    prior_entry = tmp_path / "archive" / "completed" / "2026-06" / "2026-06-01-some-plan-abc123.md"
    prior_entry.parent.mkdir(parents=True, exist_ok=True)
    prior_entry.write_text(
        "---\n"
        'title: "Did the thing"\n'
        "created: 2026-06-01\n"
        "nature: bugfix\n"
        "nature_inferred: false\n"
        "commits: []\n"
        'chain: "some-plan"\n'
        "status: pending-release\n"
        "chain_terminal: true\n"
        'authored_by: "abc123"\n'
        "loe:\n"
        "  agent_dispatches: null\n"
        "  opus_dispatches: null\n"
        "  em_tokens: null\n"
        "  tshirt: null\n"
        "---\n\nDid the thing, verified.\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(ws_apply, "brief", lambda decisions=None: wsc.brief(decisions=decisions, repo_root=tmp_path))

    landed_ids: list[tuple[str, ...]] = []

    def ok_main(argv: list[str]) -> int:
        landed_ids.append(tuple(argv))
        # Every fake CLI prints a harmless stdout line so
        # d-reconcile-completion-commits's {d-complete-entry.entry_path}
        # arg-token has something to resolve from — that threading
        # mechanism is exercised elsewhere (test_resolve_arg_tokens_*,
        # test_execute_directives_resolves_and_dispatches_an_entry_path_
        # token) and is not what this test is targeting.
        print(str(prior_entry))
        return 0

    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: _fake_module(ok_main, cli_name))

    exit_code, report = ws_apply.apply(
        decisions={
            "subject": "a commit subject",
            "governing_plan_slug": chain_slug,
            "stage_paths": ["state/handoff-tracker.md"],
        }
    )

    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.SUCCESS)
    assert "d-run-wsc-tail" not in report["blocked"]
    assert "d-run-wsc-tail" in report["landed"]
    # Every fake CLI (including d-close-tail-args's own wsc-close stand-in)
    # prints `str(prior_entry)` — that single non-blank line is what the
    # trailing `{d-close-tail-args.argv}` token on d-run-wsc-tail's args
    # expands to, appended after the build-time-known flags.
    assert (
        "--sid",
        "testsid123",
        "--subject",
        "a commit subject",
        "--stage-paths",
        "state/handoff-tracker.md",
        "--governing-plan-slug",
        chain_slug,
        str(prior_entry),
    ) in landed_ids


# ---------------------------------------------------------------------------
# No-commit row guard (C13, example-doctrine-repo docs/plans/2026-07-29-pm-approved-
# provenance-write-time-closure-gate.md) — a task-spine row this session's
# commit-coverage oracle (close_out_and_stamp._determine_shipped, reused not
# reimplemented) found no covering commit for must not resolve to a silent
# "it's deferred" default; `apply()` halts before dispatching ANY directive
# until it is named in decisions["no_commit_row_dispositions"].
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def _init_repo(root: Path) -> None:
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@t"], root)
    _git(["config", "user.name", "test"], root)


_NO_COMMIT_ROW_PLAN_TEXT = """---
title: "fixture plan"
created: 2026-07-29
deliverable_id: "dlv-fixture-no-commit-row-000001"
status: draft
---

# fixture plan

## Tasks

```yaml plan-tasks
- id: C1
  title: a row with no covering commit
  disposition: open
```
"""


def _seed_no_commit_row_plan(root: Path, dest_name: str = "myplan.md") -> Path:
    """Seeds a minimal governing-plan fixture (one commit-required `C1` row,
    `disposition: open`) with a non-chunk-id-shaped commit subject — `C1`
    never lands a covering commit in this fixture, mirroring
    `close_out_and_stamp`'s own `_seed_plan` test helper (that module's own
    test file, not reused directly here to avoid a cross-package test
    import — see this chunk's file scope, `apply.py`+`judgments.py` only)."""
    dest = root / dest_name
    dest.write_text(_NO_COMMIT_ROW_PLAN_TEXT, encoding="utf-8")
    _git(["add", dest_name], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return dest


def test_no_commit_row_judgment_returns_none_when_no_governing_plan_resolves(tmp_path: Path) -> None:
    assert ws_apply._no_commit_row_judgment({}, tmp_path) is None


def test_no_commit_row_judgment_surfaces_a_row_with_no_covering_commit(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    _seed_no_commit_row_plan(tmp_path, "docs/plans/myplan.md")

    jp = ws_apply._no_commit_row_judgment({"governing_plan_slug": "myplan"}, tmp_path)

    assert jp is not None
    assert jp["id"] == "jp-no-commit-row-disposition"
    assert "C1" in jp["evidence"]
    assert "C1" in jp["question"]


def test_no_commit_row_judgment_returns_none_once_row_already_resolved(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    _seed_no_commit_row_plan(tmp_path, "docs/plans/myplan.md")

    jp = ws_apply._no_commit_row_judgment(
        {
            "governing_plan_slug": "myplan",
            "no_commit_row_dispositions": {"C1": "carried-forward"},
        },
        tmp_path,
    )

    assert jp is None


def test_no_commit_row_judgment_returns_none_once_the_row_ships(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    plan_file = _seed_no_commit_row_plan(tmp_path, "docs/plans/myplan.md")

    with plan_file.open("a", encoding="utf-8") as fh:
        fh.write("\n<!-- C1 landed -->\n")
    _git(["add", "docs/plans/myplan.md"], tmp_path)
    _git(
        ["commit", "-q", "-m", "C1: land chunk", "-m", "Deliverable-Id: dlv-fixture-no-commit-row-000001"],
        tmp_path,
    )

    jp = ws_apply._no_commit_row_judgment({"governing_plan_slug": "myplan"}, tmp_path)

    assert jp is None


_NO_DELIVERABLE_ID_PLAN_TEXT = """---
title: "fixture plan, no deliverable_id"
created: 2026-08-03
status: draft
---

# fixture plan, no deliverable_id

## Tasks

```yaml plan-tasks
- id: C1
  title: a row with no covering commit
  disposition: open
```
"""


def _seed_no_deliverable_id_plan(root: Path, dest_name: str = "myplan.md") -> Path:
    """Seeds a governing-plan fixture carrying NO `deliverable_id:`
    frontmatter field at all -- the commit-coverage join is never
    ATTEMPTED for this plan (`_determine_shipped`'s `"no_join_key"`
    join-provenance state), which is a distinct fact from "the join ran
    and genuinely found nothing" -- see close_out_and_stamp.py's own
    join-provenance widening for why the two must not be conflated in the
    judgment this guard surfaces."""
    dest = root / dest_name
    dest.write_text(_NO_DELIVERABLE_ID_PLAN_TEXT, encoding="utf-8")
    _git(["add", dest_name], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return dest


def test_no_commit_row_judgment_names_unjoinable_key_not_unshipped_work_no_join_key(
    tmp_path: Path,
) -> None:
    """AC-level coverage at the `workstream_complete` boundary (cross-repo
    memo fix): a plan with no `deliverable_id:` field at all must still
    surface the judgment (the guard never silently suppresses on an
    unjoinable key), but its `evidence` text must name the key as
    unattributable rather than asserting the row is unshipped."""
    _init_repo(tmp_path)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    _seed_no_deliverable_id_plan(tmp_path, "docs/plans/myplan.md")

    jp = ws_apply._no_commit_row_judgment({"governing_plan_slug": "myplan"}, tmp_path)

    assert jp is not None
    assert jp["id"] == "jp-no-commit-row-disposition"
    assert "C1" in jp["evidence"]
    assert "no_join_key" in jp["evidence"]
    assert "UNATTRIBUTABLE" in jp["evidence"]
    # The judgment still fires with all five named exits -- no sixth,
    # silent "unjoinable, skip it" disposition was introduced.
    disposition_names = {d["value"] for d in jp["dispositions"]}
    assert disposition_names == {
        "shipped",
        "spun-off",
        "backlogged",
        "wont-do",
        "carried-forward",
    }


def test_no_commit_row_judgment_names_unjoinable_key_not_unshipped_work_key_mismatch(
    tmp_path: Path,
) -> None:
    """The `key_mismatch` sibling of the test above: the governing plan's
    own `deliverable_id:` is present, and a commit in range DOES carry a
    `Deliverable-Id` trailer -- just for a different plan entirely. The
    join is genuinely unattributable for this plan, not evidence C1 is
    unshipped."""
    _init_repo(tmp_path)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    plan_file = _seed_no_commit_row_plan(tmp_path, "docs/plans/myplan.md")

    with plan_file.open("a", encoding="utf-8") as fh:
        fh.write("\n<!-- C1 landed (wrong deliverable) -->\n")
    _git(["add", "docs/plans/myplan.md"], tmp_path)
    _git(
        [
            "commit",
            "-q",
            "-m",
            "C1: land chunk",
            "-m",
            "Deliverable-Id: dlv-some-other-plan-000099",
        ],
        tmp_path,
    )

    jp = ws_apply._no_commit_row_judgment({"governing_plan_slug": "myplan"}, tmp_path)

    assert jp is not None
    assert jp["id"] == "jp-no-commit-row-disposition"
    assert "C1" in jp["evidence"]
    assert "key_mismatch" in jp["evidence"]
    assert "UNATTRIBUTABLE" in jp["evidence"]
    disposition_names = {d["value"] for d in jp["dispositions"]}
    assert disposition_names == {
        "shipped",
        "spun-off",
        "backlogged",
        "wont-do",
        "carried-forward",
    }


def test_apply_halts_before_any_directive_when_no_commit_row_guard_fires(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def dispatched_main(argv: list[str]) -> int:
        raise AssertionError(
            f"no directive may dispatch while the no-commit-row guard is unresolved; got argv={argv!r}"
        )

    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: _fake_module(dispatched_main, cli_name))

    pending_jp = {
        "id": "jp-no-commit-row-disposition",
        "question": "Task-spine row(s) with no covering commit this pass -- C1 -- ...",
        "dispositions": [
            {"value": "shipped", "resolves": []},
            {"value": "spun-off", "resolves": []},
            {"value": "backlogged", "resolves": []},
            {"value": "wont-do", "resolves": []},
            {"value": "carried-forward", "resolves": []},
        ],
        "evidence": "row(s) with no covering commit found: C1",
        "reason": "resolving a no-commit row to 'it's deferred' is a scope decision",
        "recommendation": {"disposition": "carried-forward", "rationale": "safe default"},
        "revalidate_at_dispatch": False,
        "round_trip": "terminal",
    }
    monkeypatch.setattr(ws_apply, "_no_commit_row_judgment", lambda decisions, root: pending_jp)

    def fake_brief(decisions: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return {
            "artifact": {"path": str(tmp_path)},
            "directives": [_directive("d_a", "fake-a")],
            "judgment_points": [],
            "decisions": decisions or {},
        }

    monkeypatch.setattr(ws_apply, "brief", fake_brief)
    exit_code, report = ws_apply.apply(decisions={})

    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.HALTED_AT_JUDGMENT)
    assert report["blocked"] == ["jp-no-commit-row-disposition"]
    assert report["landed"] == []
    assert report["judgment_points"] == [pending_jp]


def test_apply_proceeds_when_no_commit_row_guard_is_clear(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ws_apply, "_no_commit_row_judgment", lambda decisions, root: None)

    def ok_main(argv: list[str]) -> int:
        return 0

    modules = {"fake-a": _fake_module(ok_main, "fake_a")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    def fake_brief(decisions: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return {
            "artifact": {"path": str(tmp_path)},
            "directives": [_directive("d_a", "fake-a")],
            "judgment_points": [],
            "decisions": decisions or {},
        }

    monkeypatch.setattr(ws_apply, "brief", fake_brief)
    exit_code, report = ws_apply.apply(decisions={})

    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.SUCCESS)
    assert report["landed"] == ["d_a"]


def test_apply_skips_the_guard_when_brief_reports_no_artifact_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An `envelope` with no `artifact`/`path` key at all (every hand-built
    fake envelope elsewhere in this file) must not crash the guard check —
    it degrades to "nothing to check", the same as `brief.py`'s existing
    fixtures throughout this file already implicitly rely on."""
    called = False

    def spy_no_commit_row_judgment(decisions: dict[str, Any], root: Path) -> None:
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(ws_apply, "_no_commit_row_judgment", spy_no_commit_row_judgment)

    def ok_main(argv: list[str]) -> int:
        return 0

    modules = {"fake-a": _fake_module(ok_main, "fake_a")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    def fake_brief(decisions: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return {
            "directives": [_directive("d_a", "fake-a")],
            "judgment_points": [],
            "decisions": decisions or {},
        }

    monkeypatch.setattr(ws_apply, "brief", fake_brief)
    exit_code, report = ws_apply.apply(decisions={})

    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.SUCCESS)
    assert report["landed"] == ["d_a"]
    assert called is False


# ---------------------------------------------------------------------------
# Inter-directive arg-token threading (module docstring, deviation 3) — the
# `{d-complete-entry.entry_path}` seam a live `workstream-complete-assemble
# brief` dispatch found unresolved (no code path substituted it, so the
# literal 30-character token string was about to be handed to
# `reconcile-completion-commits.py --append` as a live positional argument).
# `_resolve_arg_tokens` is the fix; these tests pin the resolve/fail-loud
# contract directly, then `test_execute_directives_resolves_and_dispatches_
# an_entry_path_token`/`..._never_dispatches_an_unresolved_token` exercise it
# through the real `_execute_directives` seam end-to-end.
# ---------------------------------------------------------------------------


def test_resolve_arg_tokens_substitutes_first_line_of_producer_stdout() -> None:
    stdout_by_id = {"d-complete-entry": "archive/completed/2026-07/entry.md\n"}
    resolved, error = ws_apply._resolve_arg_tokens(
        ["--append", "{d-complete-entry.entry_path}"], stdout_by_id
    )
    assert error is None
    assert resolved == ["--append", "archive/completed/2026-07/entry.md"]


def test_resolve_arg_tokens_uses_only_the_first_line() -> None:
    stdout_by_id = {"d-complete-entry": "entry.md\nResidue: prose\n"}
    resolved, error = ws_apply._resolve_arg_tokens(["{d-complete-entry.entry_path}"], stdout_by_id)
    assert error is None
    assert resolved == ["entry.md"]


def test_resolve_arg_tokens_no_token_passes_args_through_unchanged() -> None:
    resolved, error = ws_apply._resolve_arg_tokens(["--sid", "abc123"], {})
    assert error is None
    assert resolved == ["--sid", "abc123"]


def test_resolve_arg_tokens_missing_producer_fails_loud_not_literal() -> None:
    resolved, error = ws_apply._resolve_arg_tokens(["{d-complete-entry.entry_path}"], {})
    assert resolved is None
    assert error is not None
    assert "d-complete-entry" in error


def test_resolve_arg_tokens_producer_landed_with_empty_stdout_fails_loud() -> None:
    resolved, error = ws_apply._resolve_arg_tokens(
        ["{d-complete-entry.entry_path}"], {"d-complete-entry": ""}
    )
    assert resolved is None
    assert error is not None


def test_resolve_arg_tokens_unrecognized_token_shape_fails_loud() -> None:
    """A `{...}` token this module doesn't recognize (not the `.entry_path`
    shape) must never survive substitution and reach dispatch as a literal
    string — the general backstop, not just the one named producer field."""
    resolved, error = ws_apply._resolve_arg_tokens(["{some-other-token}"], {})
    assert resolved is None
    assert error is not None
    assert "some-other-token" in error


# ---------------------------------------------------------------------------
# `.landed` token — the ordering-only field (no value threaded) that
# `directives_commit_tail.build_emit_cadence_directive`/`build_release_
# plan_claim_directive` use to express a genuine producer dependency on
# `d-run-wsc-tail` through the arg-token seam, since `ceremony_common.
# apply_halt._directive_gate_open` deliberately never gates a directive-id
# `depends_on` member (see that function's docstring).
# ---------------------------------------------------------------------------


def test_resolve_arg_tokens_landed_field_substitutes_empty_string_when_producer_landed() -> None:
    resolved, error = ws_apply._resolve_arg_tokens(
        ["{d-run-wsc-tail.landed}"], {"d-run-wsc-tail": "some captured stdout\n"}
    )
    assert error is None
    assert resolved == [""]


def test_resolve_arg_tokens_landed_field_substitutes_empty_string_even_with_no_stdout() -> None:
    """Unlike `.entry_path`, `.landed` never requires non-empty stdout —
    it only requires the producer to be a `stdout_by_id` key (landed this
    pass, exit 0), never a value from what it printed."""
    resolved, error = ws_apply._resolve_arg_tokens(
        ["release-artifact", "plan", "some-slug", "{d-run-wsc-tail.landed}"],
        {"d-run-wsc-tail": ""},
    )
    assert error is None
    assert resolved == ["release-artifact", "plan", "some-slug", ""]


def test_resolve_arg_tokens_landed_field_fails_loud_when_producer_never_landed() -> None:
    resolved, error = ws_apply._resolve_arg_tokens(["{d-run-wsc-tail.landed}"], {})
    assert resolved is None
    assert error is not None
    assert "d-run-wsc-tail" in error


# ---------------------------------------------------------------------------
# `.argv` token — the whole-arg, list-expanding field
# (`directives_commit_tail.build_wsc_tail_directive`'s
# `"{d-close-tail-args.argv}"`) that closes the 2026-08-03
# example-doctrine-repo-em-wsc-tail-review-metadata-dropped hole: `d-close-tail-args`
# (`wsc-close tail-args`) prints one argv token per line on stdout, and this
# field is what actually splices those tokens into `d-run-wsc-tail`'s own
# argv -- `depends_on` alone only orders the two directives.
# ---------------------------------------------------------------------------


def test_resolve_arg_tokens_argv_field_expands_multiline_stdout_into_separate_args() -> None:
    stdout_by_id = {
        "d-close-tail-args": "--deleted-paths\nstate/old.md\n--review-verdict\napproved\n"
    }
    resolved, error = ws_apply._resolve_arg_tokens(
        ["--sid", "abcdef", "{d-close-tail-args.argv}"], stdout_by_id
    )
    assert error is None
    assert resolved == [
        "--sid",
        "abcdef",
        "--deleted-paths",
        "state/old.md",
        "--review-verdict",
        "approved",
    ]


def test_resolve_arg_tokens_argv_field_empty_producer_stdout_resolves_to_no_extra_args() -> None:
    """Empty stdout is legal, not an error -- `wsc-close tail-args` prints
    nothing when neither optional flag group was supplied, which is the
    ordinary case, not a failure (unlike `.entry_path`, which requires
    non-empty stdout)."""
    resolved, error = ws_apply._resolve_arg_tokens(
        ["--sid", "abcdef", "{d-close-tail-args.argv}"], {"d-close-tail-args": ""}
    )
    assert error is None
    assert resolved == ["--sid", "abcdef"]


def test_resolve_arg_tokens_argv_field_embedded_in_a_larger_string_fails_loud() -> None:
    resolved, error = ws_apply._resolve_arg_tokens(
        ["prefix-{d-close-tail-args.argv}-suffix"], {"d-close-tail-args": "--flag\n"}
    )
    assert resolved is None
    assert error is not None
    assert "d-close-tail-args" in error


def test_resolve_arg_tokens_argv_field_fails_loud_when_producer_never_landed() -> None:
    resolved, error = ws_apply._resolve_arg_tokens(["{d-close-tail-args.argv}"], {})
    assert resolved is None
    assert error is not None
    assert "d-close-tail-args" in error


def test_execute_directives_argv_token_expands_into_consumer_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end through `_execute_directives`: the producer's multi-line
    stdout arrives at the consumer as N separate argv tokens, in order --
    never as one literal joined string."""
    seen_args: dict[str, list[str]] = {}

    def producer_main(argv: list[str]) -> int:
        print("--deleted-paths")
        print("state/old.md")
        return 0

    def consumer_main(argv: list[str]) -> int:
        seen_args["consumer"] = argv
        return 0

    modules = {
        "fake-producer": _fake_module(producer_main, "fake_producer"),
        "fake-consumer": _fake_module(consumer_main, "fake_consumer"),
    }
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [
        _directive("d-close-tail-args", "fake-producer"),
        _directive(
            "d-run-wsc-tail",
            "fake-consumer",
            args=["--sid", "abcdef", "{d-close-tail-args.argv}"],
            depends_on="d-close-tail-args",
        ),
    ]
    exit_code, report = ws_apply._execute_directives(directives, [], {})

    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.SUCCESS)
    assert report["landed"] == ["d-close-tail-args", "d-run-wsc-tail"]
    assert seen_args["consumer"] == ["--sid", "abcdef", "--deleted-paths", "state/old.md"]


def test_execute_directives_argv_token_producer_never_landed_never_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_load(cli_name: str) -> ModuleType:
        raise AssertionError("a directive with an unresolved '.argv' token must never dispatch")

    monkeypatch.setattr(ws_apply, "_load_cli_module", fake_load)

    directives = [
        _directive(
            "d-run-wsc-tail",
            "fake-consumer",
            args=["--sid", "abcdef", "{d-close-tail-args.argv}"],
        ),
    ]
    exit_code, report = ws_apply._execute_directives(directives, [], {})

    assert report["landed"] == []
    assert [entry["id"] for entry in report["failed"]] == ["d-run-wsc-tail"]
    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.DIRECTIVE_FAILED)


# ---------------------------------------------------------------------------
# Regression: state/subagent-share dispatch brief, 2026-07-28 --
# `directives_commit_tail.build_emit_cadence_directive`/`build_release_
# plan_claim_directive` set `cadence["args"] = []` (`d-emit-cadence`) with
# no arg token referencing `d-run-wsc-tail`'s output. Because a directive-id
# `depends_on` member never gates (`_directive_gate_open`'s docstring), and
# because there was no token for `_resolve_arg_tokens` to fail on, both
# directives dispatched and landed even when `d-run-wsc-tail` never ran —
# including when the tail was BLOCKED by a judgment point. Net effect:
# cadence emitted / plan claim released for a ceremony whose commit never
# happened. Both tests below FAIL against the pre-fix `args=[]`/`args=[...,
# slug]` shape (proving the regression) and PASS once the trailing
# `{d-run-wsc-tail.landed}` token is present and `_execute_directives`
# refuses to dispatch it.
# ---------------------------------------------------------------------------


def test_emit_cadence_does_not_dispatch_when_wsc_tail_is_blocked_by_a_judgment_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coordinator_core.workstream_complete import directives_commit_tail

    def dispatched_main(argv: list[str]) -> int:
        raise AssertionError(
            f"d-emit-cadence must never dispatch while its producer d-run-wsc-tail "
            f"is blocked; got argv={argv!r}"
        )

    modules = {"emit-cadence": _fake_module(dispatched_main, "emit_cadence")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    wsc_tail = _directive("d-run-wsc-tail", "wsc-tail", args=["--sid", "abcdef"], depends_on="jp-commit-subject-missing")
    jp = {
        "id": "jp-commit-subject-missing",
        "dispositions": [{"value": "subject-not-yet-supplied", "resolves": []}],
    }
    cadence = directives_commit_tail.build_emit_cadence_directive()

    exit_code, report = ws_apply._execute_directives([wsc_tail, cadence], [jp], {})

    assert "d-run-wsc-tail" in report["blocked"]
    assert "d-emit-cadence" not in report["landed"]
    assert [entry["id"] for entry in report["failed"]] == ["d-emit-cadence"], (
        "d-emit-cadence must fail loud on its unresolved '.landed' token rather "
        "than dispatch, since its producer d-run-wsc-tail is blocked, not landed"
    )
    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.DIRECTIVE_FAILED)


def test_emit_cadence_does_not_dispatch_when_wsc_tail_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coordinator_core.workstream_complete import directives_commit_tail

    def dispatched_main(argv: list[str]) -> int:
        raise AssertionError(
            f"d-emit-cadence must never dispatch when its producer d-run-wsc-tail "
            f"failed; got argv={argv!r}"
        )

    def failing_wsc_tail(argv: list[str]) -> int:
        return 1

    modules = {
        "wsc-tail": _fake_module(failing_wsc_tail, "wsc_tail"),
        "emit-cadence": _fake_module(dispatched_main, "emit_cadence"),
    }
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    wsc_tail = _directive("d-run-wsc-tail", "wsc-tail", args=["--sid", "abcdef"])
    cadence = directives_commit_tail.build_emit_cadence_directive()

    exit_code, report = ws_apply._execute_directives([wsc_tail, cadence], [], {})

    assert [entry["id"] for entry in report["failed"]] == ["d-run-wsc-tail", "d-emit-cadence"]
    assert "d-emit-cadence" not in report["landed"]
    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.DIRECTIVE_FAILED)


def test_release_plan_claim_does_not_dispatch_when_wsc_tail_is_blocked_by_a_judgment_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coordinator_core.workstream_complete import directives_commit_tail

    def dispatched_main(argv: list[str]) -> int:
        raise AssertionError(
            f"d-release-plan-claim must never dispatch while its producer "
            f"d-run-wsc-tail is blocked; got argv={argv!r}"
        )

    modules = {"session-claim-cli": _fake_module(dispatched_main, "session_claim_cli")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    wsc_tail = _directive("d-run-wsc-tail", "wsc-tail", args=["--sid", "abcdef"], depends_on="jp-commit-subject-missing")
    jp = {
        "id": "jp-commit-subject-missing",
        "dispositions": [{"value": "subject-not-yet-supplied", "resolves": []}],
    }
    release = directives_commit_tail.build_release_plan_claim_directive("some-governing-plan")

    exit_code, report = ws_apply._execute_directives([wsc_tail, release], [jp], {})

    assert "d-run-wsc-tail" in report["blocked"]
    assert "d-release-plan-claim" not in report["landed"]
    assert [entry["id"] for entry in report["failed"]] == ["d-release-plan-claim"], (
        "d-release-plan-claim must fail loud on its unresolved '.landed' token "
        "rather than dispatch, since its producer d-run-wsc-tail is blocked, "
        "not landed"
    )
    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.DIRECTIVE_FAILED)


# ---------------------------------------------------------------------------
# jp-stage-paths-missing's own apply-level HALT coverage -- its sibling
# jp-commit-subject-missing is exercised at the `_execute_directives` level
# above (via the emit-cadence/release-plan-claim blocked-producer tests);
# this is the matching direct-HALT coverage for jp-stage-paths-missing
# gating d-run-wsc-tail ITSELF (state/bug-backlog/2026-07-29-workstream-
# complete-silently-under-commi-33e5cdf24112.yaml). Both judgment points
# share `build_untrusted_gate_judgment_point`'s "structurally unresolvable,
# resolves=[]" shape -- see `judgments.build_stage_paths_missing_judgment_
# point`'s own docstring.
# ---------------------------------------------------------------------------


def test_wsc_tail_does_not_dispatch_when_stage_paths_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coordinator_core.workstream_complete import judgments as _judgments

    def dispatched_main(argv: list[str]) -> int:
        raise AssertionError(
            f"d-run-wsc-tail must never dispatch while jp-stage-paths-missing "
            f"is unresolved (decisions['stage_paths'] absent); got argv={argv!r}"
        )

    modules = {"wsc-tail": _fake_module(dispatched_main, "wsc_tail")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    wsc_tail = _directive(
        "d-run-wsc-tail", "wsc-tail", args=["--sid", "abcdef"], depends_on="jp-stage-paths-missing"
    )
    jp = _judgments.build_stage_paths_missing_judgment_point(candidate_paths=[])

    exit_code, report = ws_apply._execute_directives([wsc_tail], [jp], {})

    assert report["blocked"] == ["d-run-wsc-tail"]
    assert report["landed"] == []
    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.HALTED_AT_JUDGMENT)


def test_execute_directives_resolves_and_dispatches_an_entry_path_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end through the real `_execute_directives` seam: a producer
    directive's captured stdout threads into a consumer's `args` via the
    `{<id>.entry_path}` token, and the consumer is dispatched with the REAL
    resolved value, never the literal token string."""
    seen_args: dict[str, list[str]] = {}

    def producer_main(argv: list[str]) -> int:
        print("archive/completed/2026-07/entry.md")
        return 0

    def consumer_main(argv: list[str]) -> int:
        seen_args["consumer"] = argv
        return 0

    modules = {
        "fake-producer": _fake_module(producer_main, "fake_producer"),
        "fake-consumer": _fake_module(consumer_main, "fake_consumer"),
    }
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [
        _directive("d-complete-entry", "fake-producer"),
        _directive(
            "d-reconcile-completion-commits",
            "fake-consumer",
            args=["--append", "{d-complete-entry.entry_path}"],
            depends_on="d-complete-entry",
        ),
    ]
    exit_code, report = ws_apply._execute_directives(directives, [], {})

    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.SUCCESS)
    assert report["landed"] == ["d-complete-entry", "d-reconcile-completion-commits"]
    assert seen_args["consumer"] == ["--append", "archive/completed/2026-07/entry.md"]


def test_execute_directives_never_dispatches_an_unresolved_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression this whole seam exists for: if the producer never
    lands (here, simulated by omitting it from `directives` entirely — the
    exact shape a stale/misordered brief would produce), the consumer must
    be recorded as `failed` WITHOUT ever dispatching — never handed the
    literal, unresolved `{...}` token as a live CLI argument."""

    def fake_load(cli_name: str) -> ModuleType:
        raise AssertionError(
            "a directive with an unresolved inter-directive token must never dispatch"
        )

    monkeypatch.setattr(ws_apply, "_load_cli_module", fake_load)

    directives = [
        _directive(
            "d-reconcile-completion-commits",
            "fake-consumer",
            args=["--append", "{d-complete-entry.entry_path}"],
        ),
    ]
    exit_code, report = ws_apply._execute_directives(directives, [], {})

    assert report["landed"] == []
    assert [entry["id"] for entry in report["failed"]] == ["d-reconcile-completion-commits"]
    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.DIRECTIVE_FAILED)


def test_brief_never_emits_a_directive_with_an_unresolved_token_at_dispatch_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The general assertion (not just the one named `d-reconcile-completion-
    commits` case): walks EVERY directive `workstream_complete.brief()`
    actually emits (real compute half, not a fake) and asserts none of them
    carries a `{...}` token that `_resolve_arg_tokens` would refuse to
    resolve given only the OTHER directives' ids as available producers —
    catches the next token someone adds to a NEW directive without wiring
    its resolution, not just a re-check of today's one instance. Uses
    `ws_apply._ARG_TOKEN_RE` itself (not a hand-rolled duplicate) so this
    assertion always tracks whatever token fields `_resolve_arg_tokens`
    actually recognizes (`.entry_path`, `.landed`, and any future
    addition) rather than silently going stale against a copy."""
    from coordinator_core.workstream_complete import brief as real_brief

    try:
        envelope = real_brief(decisions={})
    except TransportFailure:
        pytest.skip("not inside a resolvable git worktree — brief() needs a real repo root")

    directives = envelope.get("directives", [])
    known_ids = {d["id"] for d in directives}
    token_re = ws_apply._ARG_TOKEN_RE

    for directive in directives:
        for arg in directive.get("args", []):
            for producer_id, _field in token_re.findall(arg):
                assert producer_id in known_ids, (
                    f"directive {directive['id']!r} arg {arg!r} names inter-directive "
                    f"token producer {producer_id!r}, which is not a directive id "
                    f"brief() itself emitted this pass ({sorted(known_ids)!r}) — this "
                    "token can never resolve at dispatch time"
                )
            residual = ws_apply._RESIDUAL_TOKEN_RE.search(
                token_re.sub("", arg)
            )
            assert residual is None, (
                f"directive {directive['id']!r} arg {arg!r} carries a "
                f"'{{...}}' token _resolve_arg_tokens's regex doesn't even "
                "recognize — it would reach dispatch unresolved"
            )


# ---------------------------------------------------------------------------
# C3 (AC6) — a second `apply` pass must not mutate anything new
# ---------------------------------------------------------------------------


def test_double_apply_lands_nothing_new_via_genuine_cli_reentrancy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC6: a second `apply` pass over the SAME envelope must be a no-op on
    disk (excluding timestamps). This directive is never `already_
    satisfied` in either pass — satisfying AC6 by asserting the fixture's
    `already_satisfied` would prove nothing about the real defect (see this
    package's own premise: `already_satisfied` has no producer at HEAD).
    Replay safety instead comes from the DISPATCHED CLI's own idempotent
    write — mirroring `d-claim-plan-execution-lock`'s verified shape
    (`directives_lessons_plan.py:297`, confirmed against `wsc-coverage-
    gate-runner.py:189`'s own contract: "0 (claimed/re-entrant/stale-
    takeover)") and `d-append-orientation-pinboard`'s verified
    whole-section-replace shape (`directives_session_hygiene.py`'s
    `build_pinboard_directive`, confirmed against `regenerate_cache.py::
    patch_pinboard_only`'s `_PINBOARD_SECTION_RE.sub(..., count=1)`): a
    directive can be safe to replay without ever claiming
    `already_satisfied`.
    """
    disk: dict[str, str] = {}

    def reentrant_replace_main(argv: list[str]) -> int:
        # Models a whole-value REPLACE (patch_pinboard_only's own shape),
        # never an append — re-running with the same argv converges to the
        # same disk state instead of accumulating a second copy.
        disk["pinboard_line"] = argv[0]
        return 0

    monkeypatch.setattr(
        ws_apply, "_load_cli_module", lambda cli_name: _fake_module(reentrant_replace_main, cli_name)
    )
    directives = [_directive("d-append-orientation-pinboard", "fake-a", args=["same note"])]

    exit_code_1, report_1 = ws_apply._execute_directives(directives, [], {})
    disk_after_first_pass = dict(disk)
    exit_code_2, report_2 = ws_apply._execute_directives(directives, [], {})

    assert exit_code_1 == exit_code_2 == int(ws_apply.WorkstreamApplyExitCode.SUCCESS)
    assert report_1["landed"] == report_2["landed"] == ["d-append-orientation-pinboard"]
    assert report_1["blocked"] == report_2["blocked"] == []
    assert report_1["failed"] == report_2["failed"] == []
    # The second pass dispatched again (never `already_satisfied`) but
    # landed byte-identical disk state — the CLI's own replace semantics,
    # not an envelope short-circuit, is what makes the replay safe.
    assert disk == disk_after_first_pass


# ---------------------------------------------------------------------------
# C3 (AC5) — hardcoded-False sites that gained a real satisfaction check
# ---------------------------------------------------------------------------


def test_build_pinboard_directive_already_satisfied_when_existing_line_matches() -> None:
    """`d-append-orientation-pinboard`'s new `existing_pinboard_line` param
    (`directives_session_hygiene.py`) computes a REAL, disk-derived
    `already_satisfied` — mirroring `baton_assemble`'s `d1_already_
    satisfied` + `already_satisfied_reason` shape — rather than asserting
    it. Matching text -> `already_satisfied=True` with a reason recorded."""
    directive = directives_session_hygiene.build_pinboard_directive(
        orientation_cache_exists=True,
        pinboard_note="finished the thing",
        existing_pinboard_line="finished the thing",
    )
    assert directive is not None
    assert directive["already_satisfied"] is True
    assert "already_satisfied_reason" in directive
    assert "finished the thing" in directive["already_satisfied_reason"]


def test_build_pinboard_directive_not_satisfied_when_existing_line_differs() -> None:
    directive = directives_session_hygiene.build_pinboard_directive(
        orientation_cache_exists=True,
        pinboard_note="finished the thing",
        existing_pinboard_line="a stale prior note",
    )
    assert directive is not None
    assert directive["already_satisfied"] is False
    assert "already_satisfied_reason" not in directive


def test_build_pinboard_directive_not_satisfied_when_existing_line_unverified() -> None:
    """The default (`existing_pinboard_line=None`, every call site until a
    caller threads the new param through) must stay `False` — absence of
    verification is never inferred as satisfaction (this package's own
    negative-spec: never assert `already_satisfied: True` to fake
    idempotence)."""
    directive = directives_session_hygiene.build_pinboard_directive(
        orientation_cache_exists=True,
        pinboard_note="finished the thing",
    )
    assert directive is not None
    assert directive["already_satisfied"] is False
    assert "already_satisfied_reason" not in directive


def test_idempotence_table_directive_ids_are_still_emitted_by_their_builders() -> None:
    """Review: coordinator:code-reviewer (P3, later P0) —
    `directives_session_hygiene.py`'s module-docstring idempotence table
    names directive ids emitted by five sibling `directives_*.py` modules it
    does not own, with no mechanical link back; nothing failed if the table
    drifted. This is that link: a plain source-text scan (not builder
    invocation — several builders need elaborate fixture args out of this
    test's scope) asserting every id the table names is still constructed by
    its owning module's source.

    `d-harvest-deferrals-<n>`, `d-flip-memo-status:<basename>`, and
    `d-freeze-and-dispatch-review-partition-<slice-id>` need PREFIX
    handling, not exact match — an exact-id assumption on the
    ordinal-suffixed `d-harvest-deferrals-<n>` id already caused one real
    defect this session (F6/directives_lessons_plan.py's own id-matching
    docstring warns of the same pitfall). A prefix check still catches a
    rename/removal of the base id; it does not (and cannot, without invoking
    the builder) catch a suffix-only change.

    P0 fix (coordinator:code-reviewer, commit d560db720's follow-up review):
    the prefix branch originally checked `directive_id in source` against the
    WHOLE module text — vacuous, because each of these three prefixes also
    appears in the owning module's own docstring prose (e.g.
    `directives_memo_lifecycle.py`'s docstring says "`d-flip-memo-status`" in
    backtick-quoted prose, independent of the real `f"d-flip-memo-
    status:{basename}"` construction). A changed construction site kept
    passing as long as the prose mention survived. The prefix branch below
    instead requires the needle to appear as the START of an f-string literal
    immediately followed (before the closing quote) by a `{` — i.e. the
    actual interpolation site, not any textual occurrence — so prose can no
    longer satisfy it.
    """
    siblings_dir = Path(directives_session_hygiene.__file__).parent

    # (module basename, [(id-or-prefix, is_prefix), ...])
    table: dict[str, list[tuple[str, bool]]] = {
        "directives_lessons_plan.py": [
            ("d-claim-plan-execution-lock", False),
            ("d-stamp-plan-implemented", False),
            ("d-harvest-deferrals-", True),
        ],
        "directives_completion.py": [
            ("d-complete-entry", False),
            ("d-reconcile-completion-commits", False),
            ("d-fold-execution-observations", False),
        ],
        "directives_commit_tail.py": [
            ("d-release-plan-claim", False),
            ("d-close-tail-args", False),
            ("d-run-wsc-tail", False),
            ("d-emit-cadence", False),
        ],
        "directives_memo_lifecycle.py": [
            ("d-flip-memo-status", True),
            ("d-emit-deletion-blocks", False),
        ],
        "directives_review.py": [
            ("d-run-review-brightline-gate", False),
            ("d-run-chain-plan-brightline-gate", False),
            ("d-freeze-and-dispatch-review-partition-", True),
            ("d-freeze-and-dispatch-review-partition-integrator", False),
            ("d-run-chain-coverage-gate", False),
            ("d-write-review-trail", False),
            ("d-run-ubt-pending-check", False),
            ("d-classify-dispatch-shape", False),
        ],
    }

    for module_basename, ids in table.items():
        source = (siblings_dir / module_basename).read_text(encoding="utf-8")
        for directive_id, is_prefix in ids:
            if is_prefix:
                # Require an f-string literal that OPENS with this prefix and
                # is followed (before its closing quote) by an interpolation
                # `{` — the real construction site, never a bare textual
                # mention (docstring prose, a comment, etc.) of the prefix.
                pattern = re.compile(rf'f"{re.escape(directive_id)}[^"\n]*\{{')
                assert pattern.search(source), (
                    f"idempotence table names prefix {directive_id!r} for "
                    f"{module_basename}, but no f-string construction site "
                    f"(f\"{directive_id}...{{...) was found in that module's "
                    "source — the table has drifted from the builder(s) it "
                    "describes, or the id is now only a textual mention"
                )
            else:
                needle = f'"{directive_id}"'
                assert needle in source, (
                    f"idempotence table names {directive_id!r} for {module_basename}, "
                    f"but {needle!r} is no longer a literal substring of that module's "
                    "source — the table has drifted from the builder(s) it describes"
                )


def test_build_machine_local_regeneratability_directive_stays_false_by_design() -> None:
    """`d-check-machine-local-regeneratability` has no real satisfaction
    check available (the invoked CLI is read-only — no disk artifact
    signals "already done") — `already_satisfied` stays permanently
    `False`, documented as a deliberate design choice, not a gap
    (`directives_session_hygiene.py`'s own docstring)."""
    directive = directives_session_hygiene.build_machine_local_regeneratability_directive()
    assert directive["already_satisfied"] is False


# ---------------------------------------------------------------------------
# best_effort / degraded — docs/plans/2026-08-08-a-best-effort-directive-
# cannot-fail-a-ce.md, chunk C1 (defects A, B, C). Each test fails against
# the unfixed `_execute_directives` — never a mocked runner.
# ---------------------------------------------------------------------------


def test_best_effort_nonzero_exit_lands_in_degraded_not_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1: a `best_effort` directive that exits non-zero must never appear
    in `report["failed"]` — only in `report["degraded"]`."""

    def failing_main(argv: list[str]) -> int:
        return 3

    modules = {"fake-degraded": _fake_module(failing_main, "fake_degraded")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directive = _directive("d_degraded", "fake-degraded")
    directive["best_effort"] = True
    exit_code, report = ws_apply._execute_directives([directive], [], {})

    assert report["failed"] == []
    assert [entry["id"] for entry in report["degraded"]] == ["d_degraded"]
    assert report["landed"] == []


def test_best_effort_directive_alone_reaches_success_not_partial_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2: with every OTHER directive clean, a run whose only non-zero exit
    was a `best_effort` one returns SUCCESS, never PARTIAL_MUTATION — the
    exact regression this plan exists to close (a clean close reporting
    PARTIAL_MUTATION on a tolerated cadence-emission failure)."""

    def ok_main(argv: list[str]) -> int:
        return 0

    def degraded_main(argv: list[str]) -> int:
        return 3

    modules = {
        "fake-ok": _fake_module(ok_main, "fake_ok"),
        "fake-degraded": _fake_module(degraded_main, "fake_degraded"),
    }
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    degraded_directive = _directive("d_degraded", "fake-degraded")
    degraded_directive["best_effort"] = True
    directives = [_directive("d_ok", "fake-ok"), degraded_directive]
    exit_code, report = ws_apply._execute_directives(directives, [], {})

    assert report["landed"] == ["d_ok"]
    assert [entry["id"] for entry in report["degraded"]] == ["d_degraded"]
    assert report["failed"] == []
    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.SUCCESS)


def test_non_best_effort_nonzero_exit_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: a directive with no `best_effort` key (or an explicit `False`)
    keeps today's behaviour — still `failed`, still moves the exit code off
    SUCCESS."""

    def failing_main(argv: list[str]) -> int:
        return 1

    modules = {"fake-fail": _fake_module(failing_main, "fake_fail")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_fail", "fake-fail")]
    exit_code, report = ws_apply._execute_directives(directives, [], {})

    assert report["degraded"] == []
    assert [entry["id"] for entry in report["failed"]] == ["d_fail"]
    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.DIRECTIVE_FAILED)


def test_failed_entry_error_carries_captured_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4 (failed path): the one field an operator reads back must name
    the reason, not just the args — the defect all three cross-repo
    reporters hit."""
    import sys as _sys

    def failing_main(argv: list[str]) -> int:
        print("route_mutation: transport failure detail here", file=_sys.stderr)
        return 3

    modules = {"fake-fail": _fake_module(failing_main, "fake_fail")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_fail", "fake-fail")]
    exit_code, report = ws_apply._execute_directives(directives, [], {})

    assert len(report["failed"]) == 1
    error = report["failed"][0]["error"]
    assert "fake-fail exited 3" in error
    assert "route_mutation: transport failure detail here" in error


def test_degraded_entry_error_carries_captured_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4 (degraded path): same fold, on the tolerated side."""
    import sys as _sys

    def failing_main(argv: list[str]) -> int:
        print("route_mutation: transport failure detail here", file=_sys.stderr)
        return 3

    modules = {"fake-degraded": _fake_module(failing_main, "fake_degraded")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directive = _directive("d_degraded", "fake-degraded")
    directive["best_effort"] = True
    exit_code, report = ws_apply._execute_directives([directive], [], {})

    assert len(report["degraded"]) == 1
    error = report["degraded"][0]["error"]
    assert "fake-degraded exited 3" in error
    assert "route_mutation: transport failure detail here" in error


def test_best_effort_dispatch_exception_lands_in_degraded_not_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (cross-repo/inbox/2026-08-10-example-retrieval-repo-em-emit-cadence-
    30s-timeout.md): `_dispatch_directive` raising — an IPC timeout, a
    transport error, anything that never reaches the `exit_code != 0`
    branch — must route through the SAME `best_effort` gate as a non-zero
    exit. Before this fix, `_execute_directives`'s `except Exception`
    around `_dispatch_directive` unconditionally appended to `failed`,
    so a best-effort directive that raised (rather than returning
    nonzero) still forced PARTIAL_MUTATION on an otherwise clean close."""

    def raising_main(argv: list[str]) -> int:
        raise TransportFailure("op 'emit.cadence' timed out after 30.0s")

    def ok_main(argv: list[str]) -> int:
        return 0

    modules = {
        "fake-ok": _fake_module(ok_main, "fake_ok"),
        "fake-raising": _fake_module(raising_main, "fake_raising"),
    }
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    degraded_directive = _directive("d-emit-cadence", "fake-raising")
    degraded_directive["best_effort"] = True
    directives = [_directive("d-run-wsc-tail", "fake-ok"), degraded_directive]
    exit_code, report = ws_apply._execute_directives(directives, [], {})

    assert report["failed"] == []
    assert [entry["id"] for entry in report["degraded"]] == ["d-emit-cadence"]
    assert report["landed"] == ["d-run-wsc-tail"]
    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.SUCCESS)


def test_non_best_effort_dispatch_exception_still_reports_partial_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine partial mutation must still surface as exit 4: a non-
    best-effort directive that raised, alongside another directive that
    already landed, keeps PARTIAL_MUTATION — the fix must not weaken this
    case while fixing the best-effort one above."""

    def raising_main(argv: list[str]) -> int:
        raise TransportFailure("transport failure")

    def ok_main(argv: list[str]) -> int:
        return 0

    modules = {
        "fake-ok": _fake_module(ok_main, "fake_ok"),
        "fake-raising": _fake_module(raising_main, "fake_raising"),
    }
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_ok", "fake-ok"), _directive("d_raising", "fake-raising")]
    exit_code, report = ws_apply._execute_directives(directives, [], {})

    assert report["degraded"] == []
    assert [entry["id"] for entry in report["failed"]] == ["d_raising"]
    assert report["landed"] == ["d_ok"]
    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.PARTIAL_MUTATION)


def test_error_string_omits_stderr_block_when_stderr_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean-stderr failure must not grow a noisy empty trailing block —
    the existing `"<cli> exited <n> (args=[...])"` prefix stays intact and
    unadorned when there is nothing to append."""

    def failing_main(argv: list[str]) -> int:
        return 2

    modules = {"fake-fail": _fake_module(failing_main, "fake_fail")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_fail", "fake-fail")]
    exit_code, report = ws_apply._execute_directives(directives, [], {})

    assert report["failed"][0]["error"] == "fake-fail exited 2 (args=[])"


def test_already_satisfied_producer_registers_empty_stdout_for_landed_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC5: an `already_satisfied` producer is registered in `stdout_by_id`
    so a `{<id>.landed}` token naming it resolves rather than reporting the
    producer never landed."""

    def consumer_main(argv: list[str]) -> int:
        return 0

    modules = {"fake-consumer": _fake_module(consumer_main, "fake_consumer")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    producer = _directive("d-run-wsc-tail", "wsc-tail", already_satisfied=True)
    consumer = _directive(
        "d-emit-cadence",
        "fake-consumer",
        args=["{d-run-wsc-tail.landed}"],
        depends_on="d-run-wsc-tail",
    )
    exit_code, report = ws_apply._execute_directives([producer, consumer], [], {})

    assert report["failed"] == []
    assert "d-run-wsc-tail" in report["landed"]
    assert "d-emit-cadence" in report["landed"]


def test_already_satisfied_producer_still_fails_an_entry_path_token_honestly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC6: an `{<id>.entry_path}` token naming an `already_satisfied`
    producer must still fail loud — a value that was never produced this
    pass must not be threaded — but with the honest "landed but captured no
    stdout" message, never the dishonest "did not land"."""

    def dispatched_main(argv: list[str]) -> int:
        raise AssertionError("the consumer must never dispatch with an unresolved token")

    monkeypatch.setattr(
        ws_apply, "_load_cli_module", lambda cli_name: _fake_module(dispatched_main, cli_name)
    )

    producer = _directive("d-complete-entry", "coordinator-complete-entry", already_satisfied=True)
    consumer = _directive(
        "d-reconcile-completion-commits",
        "reconcile-completion-commits",
        args=["--append", "{d-complete-entry.entry_path}"],
        depends_on="d-complete-entry",
    )
    exit_code, report = ws_apply._execute_directives([producer, consumer], [], {})

    assert "d-complete-entry" in report["landed"]
    assert [entry["id"] for entry in report["failed"]] == ["d-reconcile-completion-commits"]
    error = report["failed"][0]["error"]
    assert "did not land" not in error
    assert "landed but captured no stdout" in error


def test_execute_directives_repro_from_plan_dispatch_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact repro shape from the dispatch message: `d-run-wsc-tail`
    lands clean, `d-emit-cadence` (its `{d-run-wsc-tail.landed}` consumer)
    exits 3 with a stderr diagnostic. Before this chunk this returned
    DIRECTIVE_FAILED with `failed: [{'id': 'd-emit-cadence', 'error':
    "emit-cadence exited 3 (args=[''])"}]`. With `best_effort: True` on the
    second directive, it must return SUCCESS with that record in `degraded`
    and the stderr text folded into the error string."""
    import sys as _sys

    def ok_tail_main(argv: list[str]) -> int:
        return 0

    def failing_cadence_main(argv: list[str]) -> int:
        print("route_mutation: transport failure detail here", file=_sys.stderr)
        return 3

    modules = {
        "wsc-tail": _fake_module(ok_tail_main, "fake_tail"),
        "emit-cadence": _fake_module(failing_cadence_main, "fake_cadence"),
    }
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    cadence_directive = _directive(
        "d-emit-cadence",
        "emit-cadence",
        args=["{d-run-wsc-tail.landed}"],
        depends_on="d-run-wsc-tail",
    )
    cadence_directive["best_effort"] = True
    directives = [_directive("d-run-wsc-tail", "wsc-tail"), cadence_directive]

    exit_code, report = ws_apply._execute_directives(directives, [], {})

    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.SUCCESS)
    assert report["failed"] == []
    assert [entry["id"] for entry in report["degraded"]] == ["d-emit-cadence"]


# ---------------------------------------------------------------------------
# 2026-08-10 depends_on-repointed-but-never-gating sweep: `d-coverage-gate`
# carries no `depends_on` (see `workstream_complete.__init__._build_legacy_
# coverage_and_trail_directives`'s own docstring for why a `depends_on`
# naming the plan-claim directive would have been decorative, not
# enforcement, and why neither `wsc-coverage-gate-runner` subcommand this
# pair dispatches has a positional slot to carry a real `{<id>.landed}`
# binding token). This is a BEHAVIOURAL pin, not a `depends_on`-field
# assertion (a bare field check is exactly what let the prior inert guard
# ship green) -- it dispatches `d-coverage-gate` through the real
# `_execute_directives` seam with its plan-claim PRODUCER FAILED, and
# asserts the consumer still lands: ordering between this pair and the
# plan-claim directives is incidental (append order in `__init__.py`'s
# `build_directives`), never enforced by the halt gate.
# ---------------------------------------------------------------------------


def test_coverage_gate_directive_carries_no_depends_on() -> None:
    from coordinator_core.workstream_complete import _build_legacy_coverage_and_trail_directives

    gate = type(
        "FakeGate", (), {"disposition": "predecessor-consumed", "consumed_handoff": "state/handoffs/x.md"}
    )()
    directives = _build_legacy_coverage_and_trail_directives(
        gate, decisions={}, plan_claim_directives=[{"id": "d-claim-plan-execution-lock"}]
    )
    ids = {d["id"]: d for d in directives}
    assert ids["d-coverage-gate"]["depends_on"] is None


def test_coverage_gate_directive_still_dispatches_when_plan_claim_producer_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from coordinator_core.workstream_complete import _build_legacy_coverage_and_trail_directives

    def failing_claim_main(argv: list[str]) -> int:
        return 1

    def ok_gate_main(argv: list[str]) -> int:
        return 0

    modules = {
        "wsc-coverage-gate-runner-claim": _fake_module(failing_claim_main, "fake_claim"),
        "wsc-coverage-gate-runner": _fake_module(ok_gate_main, "fake_gate"),
    }
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    gate = type(
        "FakeGate", (), {"disposition": "predecessor-consumed", "consumed_handoff": "state/handoffs/x.md"}
    )()
    plan_claim_directive = _directive("d-claim-plan-execution-lock", "wsc-coverage-gate-runner-claim")
    pair = _build_legacy_coverage_and_trail_directives(
        gate, decisions={}, plan_claim_directives=[plan_claim_directive]
    )
    directives = [plan_claim_directive] + pair

    # Review: code-reviewer (Finding 2) — this dispatches `d-coverage-gate`
    # through the real `_execute_directives` seam with exit_code==0 and
    # `consumed_handoff="state/handoffs/x.md"`, a directive id/verdict-shape
    # `record_gate_verdict_if_passed` DOES memoize. Without `repo_root`
    # pinned to an isolated `tmp_path`, `_lazy_repo_root()` resolves the
    # REAL repo root and writes a fixture memo into live
    # `state/ceremony/wsc-gate-verdict-memo/` — exactly the leaked file this
    # finding traced.
    exit_code, report = ws_apply._execute_directives(directives, [], {}, repo_root=tmp_path)

    assert report["failed"] == [
        {"id": "d-claim-plan-execution-lock", "error": "wsc-coverage-gate-runner-claim exited 1 (args=[])"}
    ]
    assert "d-coverage-gate" in report["landed"], (
        "d-coverage-gate must still dispatch when the plan-claim producer "
        "failed -- depends_on naming a sibling directive id never gates "
        "(apply_halt._directive_gate_open), so this pair's ordering is "
        "incidental, never an enforced block"
    )
    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.PARTIAL_MUTATION)
