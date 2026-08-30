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

import json
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Optional

import pytest

from coordinator_core.ceremony_common.apply_halt import UnrecognizedDirective
from coordinator_core.ceremony_common.cli_rejection import CliExitClass
from coordinator_core.win_portability import no_console_creationflags
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
    """Two of the four CLI names the plan body originally pinned by hand are
    still individually correct `CONSUMES_MANIFEST` members — just not an
    exhaustive dispatch boundary any more. (`wsc-tail` was the third to go,
    removed from the manifest in the ceremony.wsc_tail kill, 2026-08-23;
    `wsc-close` the fourth, 2026-08-30, when its last directive emitter went
    with `wsc-close tail-args`.)"""
    legacy = {
        "wsc-coverage-gate-runner",
        "check-workstream-complete-deletion-blocks",
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

    exit_code, stdout_text, stderr_text, _exit_class = ws_apply._invoke_cli_main(
        _fake_module(main_fn), ["--flag", "value"]
    )
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

    exit_code, stdout_text, stderr_text, _exit_class = ws_apply._invoke_cli_main(
        _fake_module(main_fn), ["--mode", "pending"]
    )
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

    exit_code, _stdout_text, _stderr_text, exit_class = ws_apply._invoke_cli_main(_fake_module(main_fn), [])
    assert exit_code == 7
    # code != 2 — never argv_rejected regardless of raise/stderr shape.
    assert exit_class is CliExitClass.RETURNED


def test_invoke_cli_main_captures_stdout() -> None:
    def main_fn(argv: list[str]) -> int:
        print("hello-from-cli")
        return 0

    exit_code, stdout_text, stderr_text, _exit_class = ws_apply._invoke_cli_main(_fake_module(main_fn), [])
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

    exit_code, stdout_text, stderr_text, exit_class = ws_apply._invoke_cli_main(_fake_module(main_fn), [])
    assert exit_code == 2
    assert stdout_text == ""
    assert stderr_text == "diagnostic detail\n"
    # main() RETURNED 2 rather than raising — never argv_rejected.
    assert exit_class is CliExitClass.RETURNED


def test_invoke_cli_main_argparse_rejection_classifies_argv_rejected() -> None:
    """A callee that raises `SystemExit(2)` with argparse-shaped stderr
    (`usage: ...` plus `: error: ...`) classifies `ARGV_REJECTED` — the
    argv was rejected before any op-level code ran, distinct from a
    zero-arg trampoline's own raised, semantic exit-2."""

    def main_fn(argv: list[str]) -> None:
        print("usage: fake_cli [-h] --sid SID", file=sys.stderr)
        print("fake_cli: error: unrecognized arguments: --bogus", file=sys.stderr)
        raise SystemExit(2)

    exit_code, _stdout_text, _stderr_text, exit_class = ws_apply._invoke_cli_main(
        _fake_module(main_fn), []
    )
    assert exit_code == 2
    assert exit_class is CliExitClass.ARGV_REJECTED


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

    modules = {"archive-stamp-cli": _fake_module(ok_main, "fake_a"), "classify-dispatch-shape": _fake_module(ok_main, "fake_b")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_a", "archive-stamp-cli"), _directive("d_b", "classify-dispatch-shape")]
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
    directives = [_directive("d_done", "archive-stamp-cli", already_satisfied=True)]
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
    directives = [_directive("d_gated", "archive-stamp-cli", depends_on="jp_gate")]
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
    directives = [_directive("d_gated", "archive-stamp-cli", depends_on="jp_gate")]
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
    directives = [_directive("d_gated", "archive-stamp-cli", depends_on="jp_gate")]
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

    modules = {"archive-stamp-cli": _fake_module(ok_main, "fake_a")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_ok", "archive-stamp-cli")]
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
    directives = [_directive("d_gated", "archive-stamp-cli", depends_on="jp_gate")]
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

    modules = {"archive-stamp-cli": _fake_module(ok_main, "fake_a")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    judgment_points = [{"id": "jp_gate", "dispositions": [{"value": "go", "resolves": ["d_gated"]}]}]
    directives = [_directive("d_gated", "archive-stamp-cli", depends_on="jp_gate")]
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

    modules = {"archive-stamp-cli": _fake_module(failing_main, "fake_a")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_fail", "archive-stamp-cli")]
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
    # Any live `_CLI_DISPATCH` member works here: the fixture exercises the
    # LOADER raising (monkeypatched above), which is reachable only once
    # `_resolve_cli` admission has passed. A name absent from the manifest
    # raises `UnrecognizedDirective` at admission instead and never reaches
    # the loader at all — a different path than this test is pinning.
    directives = [_directive("d_missing", "freeze-review-diff")]
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

    modules = {"wsc-coverage-gate-runner": _fake_module(ok_main, "fake_ok"), "check-machine-local-regeneratability": _fake_module(failing_main, "fake_fail")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_ok", "wsc-coverage-gate-runner"), _directive("d_fail", "check-machine-local-regeneratability")]
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

    modules = {"check-machine-local-regeneratability": _fake_module(failing_main, "fake_fail"), "wsc-coverage-gate-runner": _fake_module(ok_main, "fake_ok")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_fail", "check-machine-local-regeneratability"), _directive("d_ok", "wsc-coverage-gate-runner")]
    ws_apply._execute_directives(directives, [], {})

    assert order == ["fail", "ok"]


# ---------------------------------------------------------------------------
# jp-consumed-handoff-completeness — AC5/AC6/AC7 (2026-08-05-session-shape-
# attribution-structural-gate, C3's redirect). Pinned here, not by asserting
# `depends_on` field values in `test_workstream_complete.py` — that only
# proves the edges EXIST, not that they actually block anything. Mirrors
# `build_consumed_handoff_completeness_judgment_point`'s own `resolves`
# shape exactly, rather than a hand-invented stand-in. (Originally six
# gated directives; `d-run-wsc-tail` and `d-reconcile-completion-commits`
# dropped from the fixture in the ceremony.wsc_tail /
# completion.reconcile_commits kills, 2026-08-23, matching the real
# judgment point's own now-four-member `resolves`.)
# ---------------------------------------------------------------------------

_CONSUMED_HANDOFF_COMPLETENESS_RESOLVED_IDS = (
    "d-claim-plan-execution-lock",
    "d-stamp-plan-implemented",
    "d-harvest-deferrals-1",
    "d-complete-entry",
)


def _consumed_handoff_completeness_fixture() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The four gated directives plus one unaffected sibling (`d-coverage-gate`,
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
        # Deliberately a DIFFERENT (fake) cli name than the two gated
        # `wsc-coverage-gate-runner` directives above, purely so the two
        # halves of this fixture can be dispatched (or not) independently in
        # the tests below without one `_load_cli_module` fake having to
        # discriminate by directive id.
        _directive("d-coverage-gate", "coordinator-fold-execution-record"),
    ]
    return directives, [jp]


@pytest.mark.parametrize("decisions", [{}, {"jp-consumed-handoff-completeness": {"disposition": "stop-and-handoff"}}])
def test_consumed_handoff_completeness_default_blocks_all_four_and_fails_none(
    monkeypatch: pytest.MonkeyPatch, decisions: dict[str, Any]
) -> None:
    """AC5: by default (unresolved OR resolved stop-and-handoff), all four
    directives stay blocked, the run halts at judgment, and NONE of
    the four lands in `report["failed"]`."""

    def fake_load(cli_name: str) -> ModuleType:
        if cli_name == "coordinator-fold-execution-record":
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


def test_consumed_handoff_completeness_override_known_in_flight_clears_all_four(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC6: selecting `override-known-in-flight` clears all four directives."""

    def producer_main(argv: list[str]) -> int:
        print("archive/completed/2026-07/entry.md")
        return 0

    modules = {
        "wsc-coverage-gate-runner": _fake_module(lambda argv: 0, "fake_coverage_gate_runner"),
        "coordinator-fold-execution-record": _fake_module(lambda argv: 0, "fake_coverage_gate_runner_standalone"),
        "coordinator-harvest-deferrals": _fake_module(lambda argv: 0, "fake_harvest"),
        "coordinator-complete-entry": _fake_module(producer_main, "fake_complete_entry"),
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
        if cli_name != "coordinator-fold-execution-record":
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

    modules = {"archive-stamp-cli": _fake_module(ok_main, "fake_a")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    def fake_brief(decisions: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return {
            "directives": [_directive("d_a", "archive-stamp-cli")],
            "judgment_points": [],
            "decisions": decisions or {},
        }

    monkeypatch.setattr(ws_apply, "brief", fake_brief)
    exit_code, report = ws_apply.apply(decisions={"jp_x": {"disposition": "go"}})

    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.SUCCESS)
    assert report["landed"] == ["d_a"]


# ---------------------------------------------------------------------------
# No-commit row guard (C13, DoE-claude docs/plans/2026-07-29-pm-approved-
# provenance-write-time-closure-gate.md) — a task-spine row this session's
# commit-coverage oracle (close_out_and_stamp._determine_shipped, reused not
# reimplemented) found no covering commit for must not resolve to a silent
# "it's deferred" default; `apply()` halts before dispatching ANY directive
# until it is named in decisions["no_commit_row_dispositions"].
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags())


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
    """C3 (2026-08-21, 'the close ceremony stops paying for the join'): the
    commit-subject/Deliverable-Id join `_determine_shipped` once used to
    auto-detect a shipped row is deleted -- the only remaining evidence a
    `## Tasks` spine row can carry is its own verified `disposition_ref`.
    This fixture now seeds `disposition: coded` with a `disposition_ref`
    pointing at a real, HEAD-ancestor commit, instead of relying on a
    commit subject the deleted join would have parsed."""
    _init_repo(tmp_path)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    plan_file = _seed_no_commit_row_plan(tmp_path, "docs/plans/myplan.md")

    with plan_file.open("a", encoding="utf-8") as fh:
        fh.write("\n<!-- C1 landed -->\n")
    _git(["add", "docs/plans/myplan.md"], tmp_path)
    _git(["commit", "-q", "-m", "land the work C1 needed"], tmp_path)
    covering_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(tmp_path), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()

    plan_file.write_text(
        _NO_COMMIT_ROW_PLAN_TEXT.replace(
            "  disposition: open",
            f"  disposition: coded\n  disposition_ref: {covering_sha}",
        ),
        encoding="utf-8",
    )
    _git(["add", "docs/plans/myplan.md"], tmp_path)
    _git(["commit", "-q", "-m", "resolve C1"], tmp_path)

    jp = ws_apply._no_commit_row_judgment({"governing_plan_slug": "myplan"}, tmp_path)

    assert jp is None


# C3 (2026-08-21, "the close ceremony stops paying for the join") removed
# the two `no_join_key`/`key_mismatch` unjoinable-key tests that used to
# live here: `_no_commit_row_judgment` no longer threads a `join_provenance`
# value through to `judgments.build_no_commit_row_disposition_judgment_point`
# at all (that builder's own default `join_provenance="joined"` wording
# applies unconditionally now), since `_determine_shipped` no longer
# classifies a commit-subject/Deliverable-Id join outcome -- there is no
# "unattributable key" distinction left for this guard to surface.


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
            "directives": [_directive("d_a", "archive-stamp-cli")],
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

    modules = {"archive-stamp-cli": _fake_module(ok_main, "fake_a")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    def fake_brief(decisions: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return {
            "artifact": {"path": str(tmp_path)},
            "directives": [_directive("d_a", "archive-stamp-cli")],
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

    modules = {"archive-stamp-cli": _fake_module(ok_main, "fake_a")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    def fake_brief(decisions: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return {
            "directives": [_directive("d_a", "archive-stamp-cli")],
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
# Residual backstop is NAME-shaped, not BRACE-shaped (2026-08-13
# doe-claude-em-wsc-review-list-collides-with-token-syntax) —
# `directives_commit_tail.build_close_tail_args_directive` (removed in the
# ceremony.wsc_tail kill, 2026-08-23) used to serialize a
# per-slice review entry with `json.dumps(payload, sort_keys=True)` into
# `--review-slice <json>`. That flat JSON object is brace-delimited but
# contains quotes, colons, and spaces — never a token candidate — and must
# pass `_resolve_arg_tokens` unchanged rather than tripping the fail-loud
# backstop meant for a genuinely unresolved/unrecognized `{name}` token.
# ---------------------------------------------------------------------------


def test_resolve_arg_tokens_serialized_json_review_slice_passes_through_unchanged() -> None:
    payload = {
        "diff_loc": 42,
        "reviewer": "coordinator:code-reviewer",
        "reviewer_evidence": "state/subagent-share/abc/review.md",
        "scope": ["coordinator_core/workstream_complete/apply.py"],
        "sha_range": "abc123..def456",
        "verdict": "approve",
    }
    serialized = json.dumps(payload, sort_keys=True)
    resolved, error = ws_apply._resolve_arg_tokens(
        ["--review-slice", serialized], {}
    )
    assert error is None
    assert resolved == ["--review-slice", serialized]


def test_resolve_arg_tokens_serialized_json_review_slice_passes_through_via_argv_expansion() -> None:
    payload = {
        "diff_loc": 7,
        "reviewer": "coordinator:code-reviewer",
        "reviewer_evidence": "state/subagent-share/xyz/review.md",
        "scope": ["coordinator_core/workstream_complete/apply.py"],
        "sha_range": "111111..222222",
        "verdict": "approve",
    }
    serialized = json.dumps(payload, sort_keys=True)
    stdout_by_id = {"d-close-tail-args": f"--review-slice\n{serialized}\n"}
    resolved, error = ws_apply._resolve_arg_tokens(
        ["{d-close-tail-args.argv}"], stdout_by_id
    )
    assert error is None
    assert resolved == ["--review-slice", serialized]


def test_resolve_arg_tokens_nested_json_review_slice_also_passes_through_unchanged() -> None:
    """A nested payload was inferred to slip through the OLD brace-shaped
    regex where a flat one failed, making the bug look payload-dependent.
    It did not: `{[^{}]+}` requires only that the MATCHED span hold no
    braces, so it matched the inner object of a nested payload just as
    readily. Both shapes failed before and both pass now — pinned here so
    neither regresses on the assumption that one of them was ever safe."""
    payload = {
        "diff_loc": 3,
        "reviewer": "coordinator:code-reviewer",
        "reviewer_evidence": "state/subagent-share/nested/review.md",
        "scope": ["coordinator_core/workstream_complete/apply.py"],
        "sha_range": "333333..444444",
        "verdict": "approve",
        "meta": {"nested_key": "nested_value", "count": 2},
    }
    serialized = json.dumps(payload, sort_keys=True)
    resolved, error = ws_apply._resolve_arg_tokens(
        ["--review-slice", serialized], {}
    )
    assert error is None
    assert resolved == ["--review-slice", serialized]


# ---------------------------------------------------------------------------
# `.landed` token — the ordering-only field (no value threaded) that
# `directives_commit_tail.build_emit_cadence_directive`/`build_release_
# plan_claim_directive` (both removed in the ceremony.wsc_tail kill,
# 2026-08-23) used to express a genuine producer dependency on
# `d-run-wsc-tail` through the arg-token seam, since `ceremony_common.
# apply_halt._directive_gate_open` deliberately never gates a directive-id
# `depends_on` member (see that function's docstring). The generic
# `.landed` mechanism itself remains live infrastructure.
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
# (formerly `directives_commit_tail.build_wsc_tail_directive`'s
# `"{d-close-tail-args.argv}"`, removed in the ceremony.wsc_tail kill,
# 2026-08-23) that closed the 2026-08-03
# doe-claude-em-wsc-tail-review-metadata-dropped hole: `d-close-tail-args`
# (`wsc-close tail-args`) printed one argv token per line on stdout, and this
# field is what spliced those tokens into `d-run-wsc-tail`'s own
# argv -- `depends_on` alone only orders the two directives. The generic
# `.argv` mechanism itself remains live infrastructure.
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
        "coordinator-complete-entry": _fake_module(producer_main, "fake_producer"),
        "coordinator-fold-execution-record": _fake_module(consumer_main, "fake_consumer"),
    }
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [
        _directive("d-close-tail-args", "coordinator-complete-entry"),
        _directive(
            "d-run-wsc-tail",
            "coordinator-fold-execution-record",
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
            "coordinator-fold-execution-record",
            args=["--sid", "abcdef", "{d-close-tail-args.argv}"],
        ),
    ]
    exit_code, report = ws_apply._execute_directives(directives, [], {})

    assert report["landed"] == []
    assert [entry["id"] for entry in report["failed"]] == ["d-run-wsc-tail"]
    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.DIRECTIVE_FAILED)


# ---------------------------------------------------------------------------
# jp-open-spine-rows-block-stamp's own apply-level HALT coverage -- the
# claim "this blocks" must be proven at the wire, not inferred from the
# envelope shape alone. Uses the real builder, not a hand-authored jp dict.
# ---------------------------------------------------------------------------


def test_open_spine_rows_block_stamp_withholds_the_implemented_stamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coordinator_core.workstream_complete import build_open_spine_rows_block_stamp_judgment_point
    from coordinator_core.workstream_complete import directives_spine_worklist

    def dispatched_main(argv: list[str]) -> int:
        raise AssertionError(
            f"d-stamp-plan-implemented must never dispatch while "
            f"jp-open-spine-rows-block-stamp is open; got argv={argv!r}"
        )

    modules = {"archive-stamp-cli": _fake_module(dispatched_main, "archive_stamp_cli")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    gate = directives_spine_worklist.OpenSpineRowGate(
        applies=True,
        rows=(directives_spine_worklist.SpineRowItem(id="C2", title="Still open", waived=False),),
        open_count=1,
        warn_text="WARN [open-spine-row-worklist]: 1 plan-spine row(s) still open on some-plan.",
        summary_line="Open spine rows: 1 still open on some-plan -- WARN emitted",
        verdict="applicable",
    )
    jp = build_open_spine_rows_block_stamp_judgment_point(gate)
    stamp = _directive(
        "d-stamp-plan-implemented", "archive-stamp-cli", depends_on="jp-open-spine-rows-block-stamp"
    )
    # Selecting the point's one disposition is the load-bearing half of this
    # assertion: it proves the one option an EM can pick does not open the
    # gate, since its `resolves` is deliberately empty.
    decisions = {"jp-open-spine-rows-block-stamp": {"disposition": jp["dispositions"][0]["value"]}}

    exit_code, report = ws_apply._execute_directives([stamp], [jp], decisions)

    assert report["blocked"] == ["d-stamp-plan-implemented"]
    assert "d-stamp-plan-implemented" not in report["landed"]
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
        "coordinator-complete-entry": _fake_module(producer_main, "fake_producer"),
        "coordinator-fold-execution-record": _fake_module(consumer_main, "fake_consumer"),
    }
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [
        _directive("d-complete-entry", "coordinator-complete-entry"),
        _directive(
            "d-reconcile-completion-commits",
            "coordinator-fold-execution-record",
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
            "coordinator-fold-execution-record",
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
    addition) rather than silently going stale against a copy.

    Scoped by `_RESIDUAL_TOKEN_RE`'s name shape: an arg carrying a
    serialized JSON payload (`--review-slice`) is brace-delimited but not
    token-shaped, and is deliberately NOT flagged here — see that
    regex's own comment block."""
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
    directives = [_directive("d-append-orientation-pinboard", "archive-stamp-cli", args=["same note"])]

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
            ("d-fold-execution-observations", False),
        ],
        # directives_commit_tail.py's row was removed here (ceremony.wsc_tail
        # kill, 2026-08-23): every directive it used to build
        # (d-release-plan-claim, d-close-tail-args, d-run-wsc-tail,
        # d-emit-cadence) is gone, and the module no longer exposes any
        # `build_*_directive` function at all.
        "directives_memo_lifecycle.py": [
            ("d-flip-memo-status", True),
            # d-emit-deletion-blocks removed 2026-08-30 with `wsc-close
            # tail-args` (251ff57703); its builder is gone from the module,
            # so the table must stop naming it.
        ],
        "directives_review.py": [
            ("d-run-review-brightline-gate", False),
            ("d-freeze-and-dispatch-review-partition-", True),
            ("d-freeze-and-dispatch-review-partition-integrator", False),
            ("d-write-review-trail", False),
            # d-run-ubt-pending-check removed with review_trail.scan_unresolved_ubt
            # (DR-374 follow-on deletion): its builder is gone from
            # directives_review.py, so the table must stop naming it.
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

    modules = {"coordinator-fold-execution-record": _fake_module(failing_main, "fake_degraded")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directive = _directive("d_degraded", "coordinator-fold-execution-record")
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
        "wsc-coverage-gate-runner": _fake_module(ok_main, "fake_ok"),
        "coordinator-fold-execution-record": _fake_module(degraded_main, "fake_degraded"),
    }
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    degraded_directive = _directive("d_degraded", "coordinator-fold-execution-record")
    degraded_directive["best_effort"] = True
    directives = [_directive("d_ok", "wsc-coverage-gate-runner"), degraded_directive]
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

    modules = {"check-machine-local-regeneratability": _fake_module(failing_main, "fake_fail")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_fail", "check-machine-local-regeneratability")]
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

    modules = {"check-machine-local-regeneratability": _fake_module(failing_main, "fake_fail")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_fail", "check-machine-local-regeneratability")]
    exit_code, report = ws_apply._execute_directives(directives, [], {})

    assert len(report["failed"]) == 1
    error = report["failed"][0]["error"]
    assert "check-machine-local-regeneratability exited 3" in error
    assert "route_mutation: transport failure detail here" in error


def test_degraded_entry_error_carries_captured_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4 (degraded path): same fold, on the tolerated side."""
    import sys as _sys

    def failing_main(argv: list[str]) -> int:
        print("route_mutation: transport failure detail here", file=_sys.stderr)
        return 3

    modules = {"coordinator-fold-execution-record": _fake_module(failing_main, "fake_degraded")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directive = _directive("d_degraded", "coordinator-fold-execution-record")
    directive["best_effort"] = True
    exit_code, report = ws_apply._execute_directives([directive], [], {})

    assert len(report["degraded"]) == 1
    error = report["degraded"][0]["error"]
    assert "coordinator-fold-execution-record exited 3" in error
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
        "wsc-coverage-gate-runner": _fake_module(ok_main, "fake_ok"),
        "freeze-review-diff": _fake_module(raising_main, "fake_raising"),
    }
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    degraded_directive = _directive("d-emit-cadence", "freeze-review-diff")
    degraded_directive["best_effort"] = True
    directives = [_directive("d-run-wsc-tail", "wsc-coverage-gate-runner"), degraded_directive]
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
        "wsc-coverage-gate-runner": _fake_module(ok_main, "fake_ok"),
        "freeze-review-diff": _fake_module(raising_main, "fake_raising"),
    }
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_ok", "wsc-coverage-gate-runner"), _directive("d_raising", "freeze-review-diff")]
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

    modules = {"check-machine-local-regeneratability": _fake_module(failing_main, "fake_fail")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_fail", "check-machine-local-regeneratability")]
    exit_code, report = ws_apply._execute_directives(directives, [], {})

    assert report["failed"][0]["error"] == "check-machine-local-regeneratability exited 2 (args=[])"


def test_already_satisfied_producer_registers_empty_stdout_for_landed_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC5: an `already_satisfied` producer is registered in `stdout_by_id`
    so a `{<id>.landed}` token naming it resolves rather than reporting the
    producer never landed."""

    def consumer_main(argv: list[str]) -> int:
        return 0

    modules = {"coordinator-fold-execution-record": _fake_module(consumer_main, "fake_consumer")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    producer = _directive("d-run-wsc-tail", "wsc-coverage-gate-runner", already_satisfied=True)
    consumer = _directive(
        "d-emit-cadence",
        "coordinator-fold-execution-record",
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
        "coordinator-fold-execution-record",
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
        "wsc-coverage-gate-runner": _fake_module(ok_tail_main, "fake_tail"),
        "coordinator-fold-execution-record": _fake_module(failing_cadence_main, "fake_cadence"),
    }
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    cadence_directive = _directive(
        "d-emit-cadence",
        "coordinator-fold-execution-record",
        args=["{d-run-wsc-tail.landed}"],
        depends_on="d-run-wsc-tail",
    )
    cadence_directive["best_effort"] = True
    directives = [_directive("d-run-wsc-tail", "wsc-coverage-gate-runner"), cadence_directive]

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


# ---------------------------------------------------------------------------
# Per-directive progress signal (cross-repo memo, example-retrieval-repo-em 2026-08-15,
# "why EMs don't cap the wsc ceremony", Finding 3): `_dispatch_directive`
# captures each CLI's streams for the whole call, so without these lines a
# slow directive is indistinguishable from a hang from outside the process.
# ---------------------------------------------------------------------------


def test_execute_directives_emits_progress_before_and_after_each_dispatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def ok_main(argv: list[str]) -> int:
        return 0

    modules = {"archive-stamp-cli": _fake_module(ok_main, "fake_a"), "classify-dispatch-shape": _fake_module(ok_main, "fake_b")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_a", "archive-stamp-cli"), _directive("d_b", "classify-dispatch-shape")]
    ws_apply._execute_directives(directives, [], {})

    err = capsys.readouterr().err
    # The pre-dispatch line is the whole point: it must be present for a
    # directive that has not returned yet, so assert on it per directive.
    assert "wsc-apply: d_a (archive-stamp-cli)" in err
    assert "wsc-apply: d_b (classify-dispatch-shape)" in err
    assert "wsc-apply: d_a exited 0 in " in err
    assert "wsc-apply: d_b exited 0 in " in err
    # stdout carries the report JSON only -- progress never contaminates it.
    assert "wsc-apply:" not in capsys.readouterr().out


def test_execute_directives_progress_survives_a_directive_writing_its_own_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Negative-spec guard: the progress writes must land OUTSIDE
    `_dispatch_directive`'s `redirect_stderr`, never inside it."""

    def noisy_main(argv: list[str]) -> int:
        sys.stderr.write("cli-internal-noise\n")
        return 0

    monkeypatch.setattr(
        ws_apply, "_load_cli_module", lambda cli_name: _fake_module(noisy_main, "noisy")
    )

    ws_apply._execute_directives([_directive("d_noisy", "archive-stamp-cli")], [], {})

    err = capsys.readouterr().err
    assert err.index("wsc-apply: d_noisy (archive-stamp-cli)") < err.index("cli-internal-noise")
    assert "wsc-apply: d_noisy exited 0 in " in err


# ---------------------------------------------------------------------------
# render_disabled_op_lines — a disabled op is its own class of event
# ---------------------------------------------------------------------------


def test_disabled_op_line_names_the_op_and_the_directive() -> None:
    """At exit 4 the report is sixty lines of JSON whose one load-bearing line is
    an op refusal buried inside a `failed` entry's `error`. This names it.

    The fixture op is drawn from `SUSPENDED_OPS` rather than hardcoded — see
    `test_disabled_op_line_covers_degraded_entries_too`, which already had it
    right. This test and `test_disabled_op_line_names_no_remedy` both pinned
    `review_trail.write`, which LEFT that table when PM ruling 2026-08-23
    readmitted the op, so both went red asserting a disabled-op line for an op
    that is registered and reinstated — the very claim C7/AC11 corrects
    elsewhere in this plan. The table is the authority and it is meant to
    shrink; a fixture that restates a row from it goes stale by construction."""
    from coordinator_core.op_budget_suspension import SUSPENDED_OPS, refusal_message

    op = next(iter(SUSPENDED_OPS))
    report = {
        "failed": [
            {"id": "d_wsc_tail_write", "error": f"wsc-tail.py exited 3 — {refusal_message(op)}"},
            {"id": "d_other", "error": "some-cli exited 1 (args=[])"},
        ],
        "degraded": [],
    }

    lines = ws_apply.render_disabled_op_lines(report)

    assert lines == [
        f"DISABLED OP {op} — off for the op budget bar; "
        "d_wsc_tail_write did not run."
    ]


def test_disabled_op_line_is_silent_on_an_ordinary_failure() -> None:
    """A directive that failed on its own merits is NOT a disabled op, and must
    not acquire a line claiming it was."""
    report = {"failed": [{"id": "d_a", "error": "some-cli exited 1 (args=[])"}], "degraded": []}

    assert ws_apply.render_disabled_op_lines(report) == []


def test_disabled_op_line_covers_degraded_entries_too() -> None:
    """A best_effort directive lands its refusal in `degraded`, not `failed`.
    The operator still needs to know an op is off."""
    from coordinator_core.op_budget_suspension import SUSPENDED_OPS, refusal_message

    op = next(iter(SUSPENDED_OPS))
    report = {
        "failed": [],
        "degraded": [{"id": "d_best_effort", "error": refusal_message(op)}],
    }

    lines = ws_apply.render_disabled_op_lines(report)

    assert len(lines) == 1
    assert op in lines[0]
    assert "d_best_effort" in lines[0]


def test_disabled_op_line_names_no_remedy() -> None:
    """Negative-spec: only one row in `SUSPENDED_OPS` carries a sanctioned
    `fallback`, and inventing a plausible one for the rest is the improvisation
    that field exists to prevent. This renderer offers none."""
    from coordinator_core.op_budget_suspension import SUSPENDED_OPS, refusal_message

    op = next(iter(SUSPENDED_OPS))
    report = {"failed": [{"id": "d", "error": refusal_message(op)}], "degraded": []}

    line = ws_apply.render_disabled_op_lines(report)[0]

    for banned in ("instead", "run ", "try ", "override", "--"):
        assert banned not in line.lower(), f"remedy-shaped text in a refusal line: {line!r}"


def test_suspension_marker_still_matches_the_live_refusal_message() -> None:
    """`render_disabled_op_lines` recognises a suspension refusal by op name plus
    one phrase. A reworded refusal must fail a test rather than silently cost the
    operator the line that says a directive failed because an op is OFF, not
    because the directive is broken."""
    from coordinator_core.op_budget_suspension import SUSPENDED_OPS, refusal_message

    for op in SUSPENDED_OPS:
        assert f"{op}{ws_apply._SUSPENSION_MARKER}" in refusal_message(op)


# ---------------------------------------------------------------------------
# Close-commit tail wiring (C13, docs/plans/2026-08-25-the-close-ceremony-
# rebuilt-from-the-requirement.md) -- regression coverage for the seam C13
# wired only into throwaway scratch scripts. See this section's own tests'
# docstrings for the three ruling-shaped properties each one pins.
# ---------------------------------------------------------------------------


class _FakeCommitTailResult:
    """Stand-in for `run_close_commit`'s `PipelineResult` -- only the fields
    `_run_close_commit_tail` itself reads are populated; the rest of that
    shape is `directives_commit_tail`'s own concern, not this seam's."""

    def __init__(
        self,
        *,
        commit_failed: bool = False,
        committed_sha: Optional[str] = "deadbeef",
        pushed: bool = True,
        integrity_breach: bool = False,
        diagnostics: tuple = (),
    ) -> None:
        self.commit_failed = commit_failed
        self.committed_sha = committed_sha
        self.pushed = pushed
        self.integrity_breach = integrity_breach
        self.diagnostics = diagnostics


def test_apply_close_commit_tail_fires_and_folds_into_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A normal apply reaches `run_close_commit_and_release_claims` -- never
    bare `run_close_commit`, which releases no claim -- and its result lands
    in `report["close_commit"]`."""
    calls: list[tuple[Any, dict[str, Any]]] = []

    def fake_release_call(worktree_root: Any, **kwargs: Any) -> _FakeCommitTailResult:
        calls.append((worktree_root, kwargs))
        return _FakeCommitTailResult()

    monkeypatch.setattr(
        ws_apply.directives_commit_tail,
        "run_close_commit_and_release_claims",
        fake_release_call,
    )
    monkeypatch.setattr(
        ws_apply.directives_commit_tail,
        "run_close_commit",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("bare run_close_commit must never be called directly")
        ),
    )
    monkeypatch.setattr(ws_apply, "_no_commit_row_judgment", lambda decisions, root: None)

    def ok_main(argv: list[str]) -> int:
        return 0

    modules = {"archive-stamp-cli": _fake_module(ok_main, "fake_a")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    def fake_brief(decisions: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return {
            "artifact": {"path": str(tmp_path)},
            "directives": [_directive("d_a", "archive-stamp-cli")],
            "judgment_points": [],
            "decisions": decisions or {},
            "preflight": {"session_shape": {"sid": "sess-1"}},
        }

    monkeypatch.setattr(ws_apply, "brief", fake_brief)

    exit_code, report = ws_apply.apply(decisions={"subject": "a close commit"})

    assert len(calls) == 1
    assert calls[0][0] == str(tmp_path)
    assert calls[0][1]["session_id"] == "sess-1"
    assert calls[0][1]["subject"] == "a close commit"
    assert report["close_commit"]["attempted"] is True
    assert report["close_commit"]["commit_failed"] is False
    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.SUCCESS)


def test_apply_close_commit_tail_fires_even_when_a_directive_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DR-358's unconditional-release ruling: the tail must fire regardless
    of the directive pass's own exit code -- NOT gated on
    `exit_code == SUCCESS`, which is exactly the "tidying up" that would
    silently reintroduce the invisible-loss failure mode DR-358 closes."""
    calls: list[Any] = []

    def fake_release_call(worktree_root: Any, **kwargs: Any) -> _FakeCommitTailResult:
        calls.append(worktree_root)
        return _FakeCommitTailResult()

    monkeypatch.setattr(
        ws_apply.directives_commit_tail,
        "run_close_commit_and_release_claims",
        fake_release_call,
    )
    monkeypatch.setattr(ws_apply, "_no_commit_row_judgment", lambda decisions, root: None)

    def failing_main(argv: list[str]) -> int:
        return 1

    modules = {"archive-stamp-cli": _fake_module(failing_main, "fake_a")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    def fake_brief(decisions: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return {
            "artifact": {"path": str(tmp_path)},
            "directives": [_directive("d_a", "archive-stamp-cli")],
            "judgment_points": [],
            "decisions": decisions or {},
            "preflight": {"session_shape": {"sid": "sess-1"}},
        }

    monkeypatch.setattr(ws_apply, "brief", fake_brief)

    exit_code, report = ws_apply.apply(decisions={"subject": "a close commit"})

    assert any(f.get("id") == "d_a" for f in report.get("failed", []))
    assert len(calls) == 1, "the tail must fire even though a directive in this pass failed"
    assert "close_commit" in report
    assert exit_code != int(ws_apply.WorkstreamApplyExitCode.SUCCESS)


@pytest.mark.parametrize(
    "decisions,sid",
    [
        ({}, "sess-1"),
        ({"subject": "a close commit"}, None),
    ],
    ids=["subject-missing", "sid-missing"],
)
def test_apply_close_commit_tail_does_not_fire_without_subject_and_sid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    decisions: dict[str, Any],
    sid: Optional[str],
) -> None:
    """Mirrors the removed `jp-commit-subject-missing` gate: no subject or no
    resolved session id means nothing to commit against, so the tail must
    not call `run_close_commit_and_release_claims` at all."""
    calls: list[Any] = []

    def fake_release_call(worktree_root: Any, **kwargs: Any) -> _FakeCommitTailResult:
        calls.append(worktree_root)
        return _FakeCommitTailResult()

    monkeypatch.setattr(
        ws_apply.directives_commit_tail,
        "run_close_commit_and_release_claims",
        fake_release_call,
    )
    monkeypatch.setattr(ws_apply, "_no_commit_row_judgment", lambda d, root: None)

    def ok_main(argv: list[str]) -> int:
        return 0

    modules = {"archive-stamp-cli": _fake_module(ok_main, "fake_a")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    def fake_brief(decisions: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return {
            "artifact": {"path": str(tmp_path)},
            "directives": [_directive("d_a", "archive-stamp-cli")],
            "judgment_points": [],
            "decisions": decisions or {},
            "preflight": {"session_shape": {"sid": sid}},
        }

    monkeypatch.setattr(ws_apply, "brief", fake_brief)

    exit_code, report = ws_apply.apply(decisions=decisions)

    assert calls == []
    # CONTRACT CHANGED 2026-08-27: a declined close commit is REPORTED, not
    # silent. This previously asserted `"close_commit" not in report` -- the
    # skip left no trace, so a session that closed with no subject was
    # indistinguishable from one whose commit ran and found nothing. That
    # invisibility was one of the two mechanisms behind fleet reports of "the
    # close commit isn't running". The skip itself is unchanged and still
    # correct; only its silence was the defect.
    assert report["close_commit"]["attempted"] is False
    assert report["close_commit"]["skipped"].startswith("close-commit:no-")
    # A DECLINED COMMIT IS NOT A FAILED ONE: no `commit_failed` key, so the
    # exit-code arm reads it falsy and SUCCESS stands.
    assert "commit_failed" not in report["close_commit"]
    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.SUCCESS)


def test_apply_close_commit_failure_escalates_success_to_partial_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A commit failure on an otherwise-SUCCESS directive pass escalates the
    exit code to `PARTIAL_MUTATION` without masking an already-worse code."""
    monkeypatch.setattr(
        ws_apply.directives_commit_tail,
        "run_close_commit_and_release_claims",
        lambda worktree_root, **kwargs: _FakeCommitTailResult(commit_failed=True),
    )
    monkeypatch.setattr(ws_apply, "_no_commit_row_judgment", lambda decisions, root: None)

    def ok_main(argv: list[str]) -> int:
        return 0

    modules = {"archive-stamp-cli": _fake_module(ok_main, "fake_a")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    def fake_brief(decisions: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return {
            "artifact": {"path": str(tmp_path)},
            "directives": [_directive("d_a", "archive-stamp-cli")],
            "judgment_points": [],
            "decisions": decisions or {},
            "preflight": {"session_shape": {"sid": "sess-1"}},
        }

    monkeypatch.setattr(ws_apply, "brief", fake_brief)

    exit_code, report = ws_apply.apply(decisions={"subject": "a close commit"})

    assert report["close_commit"]["commit_failed"] is True
    assert exit_code == int(ws_apply.WorkstreamApplyExitCode.PARTIAL_MUTATION)


# ---------------------------------------------------------------------------
# Completion-entry commit-ledger fold wiring (2026-08-30). The fold itself
# lives in `ops/ceremony/post_commit_tail.py` and is tested there; what these
# tests pin is the WIRE, which is the half that was missing — the fold shipped
# taking a caller-supplied entry path that no caller supplied, so it never ran
# once and every completion entry's `commits:` list stayed empty. The seam is
# this module: `d-complete-entry` prints the entry path here, and the close
# commit produces the sha here.
# ---------------------------------------------------------------------------


_ENTRY_REL = "archive/completed/2026-08/2026-08-30-entry.md"


def _brief_with_complete_entry(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    """A one-directive brief whose CLI stands in for `coordinator-complete-
    entry`, honouring the same contract the real one does: the entry path on
    the first line of stdout."""

    def entry_main(argv: list[str]) -> int:
        print(stdout, end="")
        return 0

    modules = {"coordinator-complete-entry": _fake_module(entry_main, "fake_entry")}
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: modules[cli_name])
    monkeypatch.setattr(
        ws_apply,
        "brief",
        lambda decisions=None: {
            "directives": [_directive("d-complete-entry", "coordinator-complete-entry")],
            "judgment_points": [],
            "decisions": decisions or {},
            "artifact": {"path": "X:/nonexistent-worktree"},
        },
    )


def _capture_fold(monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    """Patches the FOLD ITSELF (`post_commit_tail`'s public seam), never
    `_run_completion_entry_fold` — the gate this module owns lives inside
    that wrapper, and patching over it would leave these tests asserting
    against their own stub."""
    from coordinator_core.ops.ceremony import post_commit_tail

    monkeypatch.setattr(
        post_commit_tail,
        "fold_completion_entry_commit",
        lambda root, entry_path, sha, **kw: calls.append((entry_path, sha))
        or {"acted": [entry_path], "skipped": [], "failed": []},
    )
    monkeypatch.setattr(ws_apply, "_run_push_outstanding_tail", lambda root: {})


def test_execute_directives_surfaces_the_completion_entry_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The path is a runtime value — today's date plus a chain-slug
    idempotency guard that can resolve to an entry filed under a different
    date entirely — so it is read off the CLI's own stdout, never re-derived.
    Only the first line: the CLI's contract is `print(entry_path)`, and
    anything after it is prose."""
    _brief_with_complete_entry(monkeypatch, f"{_ENTRY_REL}\nResidue: some prose\n")
    monkeypatch.setattr(ws_apply, "_run_close_commit_tail", lambda *a, **kw: None)
    monkeypatch.setattr(ws_apply, "_run_push_outstanding_tail", lambda root: {})

    _, report = ws_apply.apply(decisions={})

    assert report["completion_entry_path"] == _ENTRY_REL


def test_apply_folds_the_landed_close_commit_sha_into_the_completion_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wire, end to end through `apply()`: the entry path `d-complete-
    entry` printed and the sha the close commit landed reach the fold
    together."""
    _brief_with_complete_entry(monkeypatch, f"{_ENTRY_REL}\n")
    monkeypatch.setattr(
        ws_apply,
        "_run_close_commit_tail",
        lambda *a, **kw: {"attempted": True, "commit_failed": False, "committed_sha": "abc1234"},
    )
    calls: list = []
    _capture_fold(monkeypatch, calls)

    _, report = ws_apply.apply(decisions={})

    assert calls == [(_ENTRY_REL, "abc1234")]
    assert report["completion_entry_fold"]["acted"] == [_ENTRY_REL]


def test_apply_does_not_fold_when_the_close_commit_landed_no_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing to fold, and folding anyway would write a `commits:` list
    naming a commit that does not exist — so the step reports nothing rather
    than an empty attempt."""
    _brief_with_complete_entry(monkeypatch, f"{_ENTRY_REL}\n")
    monkeypatch.setattr(
        ws_apply,
        "_run_close_commit_tail",
        lambda *a, **kw: {"attempted": False, "skipped": "close-commit:no-subject"},
    )
    calls: list = []
    _capture_fold(monkeypatch, calls)

    _, report = ws_apply.apply(decisions={})

    assert calls == []
    assert "completion_entry_fold" not in report


def test_completion_entry_fold_is_a_no_op_without_a_path_or_a_sha() -> None:
    """The gate itself, directly: both halves are required, and a missing
    one is `None` (step did not run), never an attempted fold."""
    assert ws_apply._run_completion_entry_fold("X:/nonexistent", None, "abc1234") is None
    assert ws_apply._run_completion_entry_fold("X:/nonexistent", _ENTRY_REL, None) is None
