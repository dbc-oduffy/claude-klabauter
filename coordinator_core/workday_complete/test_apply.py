"""
coordinator_core.workday_complete.test_apply — dispatch-mechanics tests for
the `workday-complete` computed-skill engine's apply half.

Purpose: the 2026-07-26 backfill-leg handoff
(`state/handoffs/2026-07-26-workday-complete-step-3-5-backfill-leg-s.md`)
found the Step 3.5 anchor/Phase-B directives silently no-op under `apply`
because the in-process dispatch seam (`apply._dispatch_directive` /
`_invoke_cli_main` / `_execute_directives`) had no stdin wiring at all, so
the multi-stage scan -> anchor -> Phase-B pipeline could never actually
carry the scan's gap-row TSV between stages. This file covers the fix:
`directives[].stdin_from` (see `brief._directive`'s docstring) names a
PRODUCING directive whose captured stdout `_execute_directives` feeds to the
CONSUMING directive's `sys.stdin`, general to any directive declaring it —
never special-cased to the two backfill directive ids.

Exercises `apply.py`'s directive-execution seam directly against small
synthetic fake CLI modules (never the real `coordinator/bin/workday-
complete-*` scripts, which would touch a real git tree) so these tests stay
fast, deterministic, and independent of the invoking repo's state.

Run scoped only:
    python3 -m pytest coordinator_core/workday_complete/test_apply.py -q
Spec backlink: state/handoffs/2026-07-26-workday-complete-step-3-5-backfill-leg-s.md
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any, Callable, Optional

import pytest

from coordinator_core.ceremony_common.cli_rejection import CliExitClass
from coordinator_core.workday_complete import apply as wc_apply


def _fake_module(main_fn: Callable[..., Any], name: str = "fake_cli") -> ModuleType:
    mod = ModuleType(name)
    mod.main = main_fn
    return mod


def _directive(
    id: str,
    cli: str,
    *,
    depends_on: Optional[str] = None,
    stdin_from: Optional[str] = None,
    already_satisfied: bool = False,
) -> dict[str, Any]:
    return {
        "id": id,
        "cli": cli,
        "args": [],
        "depends_on": depends_on,
        "already_satisfied": already_satisfied,
        "stdin_from": stdin_from,
    }


def test_invoke_cli_main_feeds_declared_stdin_to_argv_taking_main() -> None:
    seen: dict[str, str] = {}

    def main_fn(argv: list[str]) -> int:
        seen["stdin"] = sys.stdin.read()
        return 0

    exit_code, stdout_text, stderr_text, _exit_class = wc_apply._invoke_cli_main(
        _fake_module(main_fn), [], stdin_text="row1\trow2\n"
    )
    assert exit_code == 0
    assert seen["stdin"] == "row1\trow2\n"
    assert stdout_text == ""
    assert stderr_text == ""


def test_invoke_cli_main_feeds_declared_stdin_to_zero_arg_main() -> None:
    seen: dict[str, str] = {}

    def main_fn() -> None:
        seen["stdin"] = sys.stdin.read()

    exit_code, _, _, _exit_class = wc_apply._invoke_cli_main(
        _fake_module(main_fn), ["--flag"], stdin_text="gap-row\n"
    )
    assert exit_code == 0
    assert seen["stdin"] == "gap-row\n"


def test_invoke_cli_main_leaves_stdin_untouched_when_none_declared() -> None:
    sentinel = sys.stdin
    seen: dict[str, Any] = {}

    def main_fn(argv: list[str]) -> int:
        seen["stdin_during_call"] = sys.stdin
        return 0

    exit_code, _, _, _exit_class = wc_apply._invoke_cli_main(_fake_module(main_fn), [], stdin_text=None)
    assert exit_code == 0
    assert seen["stdin_during_call"] is sentinel
    assert sys.stdin is sentinel


def test_invoke_cli_main_captures_stdout_for_downstream_consumption() -> None:
    def main_fn(argv: list[str]) -> int:
        print("2026-07-19\t3\tbase\ttip")
        return 0

    exit_code, stdout_text, stderr_text, _exit_class = wc_apply._invoke_cli_main(_fake_module(main_fn), [])
    assert exit_code == 0
    assert stdout_text == "2026-07-19\t3\tbase\ttip\n"
    assert stderr_text == ""


def test_invoke_cli_main_captures_stderr() -> None:

    def main_fn(argv: list[str]) -> int:
        print("diagnostic detail", file=sys.stderr)
        return 2

    exit_code, stdout_text, stderr_text, exit_class = wc_apply._invoke_cli_main(_fake_module(main_fn), [])
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

    exit_code, _stdout_text, _stderr_text, exit_class = wc_apply._invoke_cli_main(
        _fake_module(main_fn), []
    )
    assert exit_code == 2
    assert exit_class is CliExitClass.ARGV_REJECTED


def test_invoke_cli_main_restores_stdin_after_exception() -> None:
    sentinel = sys.stdin

    def main_fn(argv: list[str]) -> int:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        wc_apply._invoke_cli_main(_fake_module(main_fn), [], stdin_text="x\n")
    assert sys.stdin is sentinel


def test_execute_directives_pipes_producer_stdout_into_consumer_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def producer_main(argv: list[str]) -> int:
        print("2026-07-19\t3\tbase\ttip")
        return 0

    def consumer_main(argv: list[str]) -> int:
        captured["stdin"] = sys.stdin.read()
        return 0

    modules = {
        "workday-complete-backfill-scan": _fake_module(producer_main, "fake_scan"),
        "workday-complete-backfill-anchor": _fake_module(consumer_main, "fake_anchor"),
    }
    monkeypatch.setattr(wc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [
        _directive("d_scan", "workday-complete-backfill-scan"),
        _directive("d_anchor", "workday-complete-backfill-anchor", stdin_from="d_scan"),
    ]
    exit_code, report = wc_apply._execute_directives(directives, [], {})

    assert captured["stdin"] == "2026-07-19\t3\tbase\ttip\n"
    assert report["landed"] == ["d_scan", "d_anchor"]
    assert report["failed"] == []
    assert exit_code == int(wc_apply.WorkdayApplyExitCode.SUCCESS)


def test_execute_directives_directive_without_stdin_from_unaffected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_stdin: dict[str, Any] = {}

    def producer_main(argv: list[str]) -> int:
        print("some output that must not leak anywhere")
        return 0

    def unrelated_main(argv: list[str]) -> int:
        seen_stdin["value"] = sys.stdin
        return 0

    sentinel = sys.stdin
    modules = {
        "workday-complete-backfill-scan": _fake_module(producer_main, "fake_scan"),
        "query-completions": _fake_module(unrelated_main, "fake_unrelated"),
    }
    monkeypatch.setattr(wc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [
        _directive("d_scan", "workday-complete-backfill-scan"),
        _directive("d_unrelated", "query-completions"),
    ]
    exit_code, report = wc_apply._execute_directives(directives, [], {})

    assert seen_stdin["value"] is sentinel
    assert report["landed"] == ["d_scan", "d_unrelated"]
    assert exit_code == int(wc_apply.WorkdayApplyExitCode.SUCCESS)


def test_execute_directives_consumer_refuses_dispatch_when_producer_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer_called: dict[str, bool] = {"called": False}

    def failing_producer_main(argv: list[str]) -> int:
        return 1

    def consumer_main(argv: list[str]) -> int:
        consumer_called["called"] = True
        return 0

    modules = {
        "workday-complete-backfill-scan": _fake_module(failing_producer_main, "fake_scan"),
        "workday-complete-backfill-anchor": _fake_module(consumer_main, "fake_anchor"),
    }
    monkeypatch.setattr(wc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [
        _directive("d_scan", "workday-complete-backfill-scan"),
        _directive("d_anchor", "workday-complete-backfill-anchor", stdin_from="d_scan"),
    ]
    exit_code, report = wc_apply._execute_directives(directives, [], {})

    assert consumer_called["called"] is False
    assert report["landed"] == []
    failed_ids = {entry["id"] for entry in report["failed"]}
    assert failed_ids == {"d_scan", "d_anchor"}
    assert exit_code == int(wc_apply.WorkdayApplyExitCode.DIRECTIVE_FAILED)


def test_execute_directives_consumer_refuses_dispatch_when_producer_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    def consumer_main(argv: list[str]) -> int:
        raise AssertionError("must never dispatch: its stdin producer never landed")

    modules = {"workday-complete-backfill-anchor": _fake_module(consumer_main, "fake_anchor")}
    monkeypatch.setattr(wc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    judgment_points = [
        {
            "id": "jp_gate",
            "dispositions": [{"value": "go", "resolves": ["d_scan"]}],
        }
    ]
    directives = [
        _directive("d_scan", "workday-complete-backfill-scan", depends_on="jp_gate"),
        _directive("d_anchor", "workday-complete-backfill-anchor", stdin_from="d_scan"),
    ]
    exit_code, report = wc_apply._execute_directives(directives, judgment_points, {})

    assert report["blocked"] == ["d_scan"]
    failed_ids = {entry["id"] for entry in report["failed"]}
    assert failed_ids == {"d_anchor"}
    assert report["landed"] == []
    assert exit_code == int(wc_apply.WorkdayApplyExitCode.DIRECTIVE_FAILED)


def test_execute_directives_failing_directive_stderr_survives_into_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    def failing_main(argv: list[str]) -> int:
        print("diagnostic detail", file=sys.stderr)
        return 2

    modules = {"standup": _fake_module(failing_main, "fake_cli")}
    monkeypatch.setattr(wc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_plain", "standup")]
    exit_code, report = wc_apply._execute_directives(directives, [], {})

    assert [entry["id"] for entry in report["failed"]] == ["d_plain"]
    result = next(r for r in report["results"] if r["id"] == "d_plain")
    assert result["stderr"] == "diagnostic detail\n"


def test_execute_directives_failing_directive_still_reported_as_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    def failing_main(argv: list[str]) -> int:
        return 2

    modules = {"standup": _fake_module(failing_main, "fake_cli")}
    monkeypatch.setattr(wc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_plain", "standup")]
    exit_code, report = wc_apply._execute_directives(directives, [], {})

    assert report["landed"] == []
    assert [entry["id"] for entry in report["failed"]] == ["d_plain"]
    assert exit_code == int(wc_apply.WorkdayApplyExitCode.DIRECTIVE_FAILED)


def test_dispatch_exception_error_string_names_the_exception_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raising_loader(cli_name: str) -> ModuleType:
        raise RuntimeError("boom detail")

    monkeypatch.setattr(wc_apply, "_load_cli_module", _raising_loader)

    directives = [_directive("d_plain", "standup")]
    _, report = wc_apply._execute_directives(directives, [], {})

    (entry,) = report["failed"]
    assert entry["error"] == "RuntimeError: boom detail"


def test_gate_evaluation_error_string_names_the_exception_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _bad_gate(directive: Any, jp_by_id: Any, decisions: Any) -> bool:
        raise ValueError("malformed envelope")

    monkeypatch.setattr(wc_apply, "_directive_gate_open", _bad_gate)

    directives = [_directive("d_plain", "standup", depends_on="jp_x")]
    judgment_points = [{"id": "jp_x", "dispositions": []}]
    _, report = wc_apply._execute_directives(directives, judgment_points, {})

    (entry,) = report["failed"]
    assert entry["error"] == "gate evaluation error: ValueError: malformed envelope"


def test_directive_subprocess_via_run_forwarding_reaches_capture_buffer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from coordinator_core.win_portability import run_forwarding

    child_script = tmp_path / "child.py"
    child_script.write_text(
        "import sys\nsys.stderr.write('child ran\\n')\nsys.exit(0)\n"
    )

    def main_fn(argv: list[str]) -> int:
        proc = run_forwarding(
            [sys.executable, str(child_script)], stdout=sys.stderr, stderr=sys.stderr,
        )
        return proc.returncode

    modules = {"standup": _fake_module(main_fn, "fake_cli")}
    monkeypatch.setattr(wc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_plain", "standup")]
    exit_code, report = wc_apply._execute_directives(directives, [], {})

    assert report["landed"] == ["d_plain"]
    assert report["failed"] == []
    result = next(r for r in report["results"] if r["id"] == "d_plain")
    assert "child ran" in result["stderr"]
    assert exit_code == int(wc_apply.WorkdayApplyExitCode.SUCCESS)


def test_best_effort_directive_failure_lands_in_degraded_not_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_main(argv: list[str]) -> int:
        return 3

    modules = {"standup": _fake_module(failing_main, "fake_cli")}
    monkeypatch.setattr(wc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directive = _directive("d_best_effort", "standup")
    directive["best_effort"] = True
    exit_code, report = wc_apply._execute_directives([directive], [], {})

    assert report["failed"] == []
    assert [entry["id"] for entry in report["degraded"]] == ["d_best_effort"]
    assert report["landed"] == []


def test_best_effort_directive_failure_alone_still_reaches_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2: with every other directive clean, a run whose only non-zero exit
    is on a `best_effort` directive must return SUCCESS, not
    PARTIAL_MUTATION — asserted against the real `_execute_directives`."""

    def failing_main(argv: list[str]) -> int:
        return 3

    def clean_main(argv: list[str]) -> int:
        return 0

    modules = {
        "coordinator-ceremony-hook": _fake_module(failing_main, "fake_best_effort"),
        "workday-complete-step2_5-dirty-tree": _fake_module(clean_main, "fake_clean"),
    }
    monkeypatch.setattr(wc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    cadence = _directive("d_best_effort", "coordinator-ceremony-hook")
    cadence["best_effort"] = True
    directives = [_directive("d_clean", "workday-complete-step2_5-dirty-tree"), cadence]
    exit_code, report = wc_apply._execute_directives(directives, [], {})

    assert report["failed"] == []
    assert [entry["id"] for entry in report["degraded"]] == ["d_best_effort"]
    assert report["landed"] == ["d_clean"]
    assert exit_code == int(wc_apply.WorkdayApplyExitCode.SUCCESS)


def test_non_best_effort_directive_failure_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:

    def failing_main(argv: list[str]) -> int:
        return 3

    modules = {"standup": _fake_module(failing_main, "fake_cli")}
    monkeypatch.setattr(wc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_plain", "standup")]
    exit_code, report = wc_apply._execute_directives(directives, [], {})

    assert report["degraded"] == []
    assert [entry["id"] for entry in report["failed"]] == ["d_plain"]
    assert exit_code == int(wc_apply.WorkdayApplyExitCode.DIRECTIVE_FAILED)


def test_failed_entry_error_string_carries_captured_stderr(monkeypatch: pytest.MonkeyPatch) -> None:

    def failing_main(argv: list[str]) -> int:
        print("op timed out after 30.0s", file=sys.stderr)
        return 3

    modules = {"standup": _fake_module(failing_main, "fake_cli")}
    monkeypatch.setattr(wc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_plain", "standup")]
    _, report = wc_apply._execute_directives(directives, [], {})

    (entry,) = report["failed"]
    assert "op timed out after 30.0s" in entry["error"]


def test_degraded_entry_error_string_carries_captured_stderr(monkeypatch: pytest.MonkeyPatch) -> None:

    def failing_main(argv: list[str]) -> int:
        print("op timed out after 30.0s", file=sys.stderr)
        return 3

    modules = {"standup": _fake_module(failing_main, "fake_cli")}
    monkeypatch.setattr(wc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directive = _directive("d_best_effort", "standup")
    directive["best_effort"] = True
    _, report = wc_apply._execute_directives([directive], [], {})

    (entry,) = report["degraded"]
    assert "op timed out after 30.0s" in entry["error"]


def test_failed_entry_error_string_has_no_stray_block_on_clean_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    def failing_main(argv: list[str]) -> int:
        return 3

    modules = {"standup": _fake_module(failing_main, "fake_cli")}
    monkeypatch.setattr(wc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_plain", "standup")]
    _, report = wc_apply._execute_directives(directives, [], {})

    (entry,) = report["failed"]
    assert entry["error"] == "standup exited 3 (args=[])"


def test_stdin_from_naming_already_satisfied_producer_still_refuses_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def consumer_main(argv: list[str]) -> int:
        raise AssertionError("must never dispatch: producer gave no stdout this pass")

    modules = {"workday-complete-backfill-anchor": _fake_module(consumer_main, "fake_anchor")}
    monkeypatch.setattr(wc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [
        _directive("d_scan", "workday-complete-backfill-scan", already_satisfied=True),
        _directive("d_anchor", "workday-complete-backfill-anchor", stdin_from="d_scan"),
    ]
    exit_code, report = wc_apply._execute_directives(directives, [], {})

    assert report["landed"] == ["d_scan"]
    failed_ids = {entry["id"] for entry in report["failed"]}
    assert failed_ids == {"d_anchor"}


def test_stdin_from_already_satisfied_message_distinguishes_from_never_landed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    def consumer_main(argv: list[str]) -> int:
        raise AssertionError("must never dispatch")

    modules = {"workday-complete-backfill-anchor": _fake_module(consumer_main, "fake_anchor")}
    monkeypatch.setattr(wc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [
        _directive("d_scan", "workday-complete-backfill-scan", already_satisfied=True),
        _directive("d_anchor", "workday-complete-backfill-anchor", stdin_from="d_scan"),
    ]
    _, report = wc_apply._execute_directives(directives, [], {})

    (entry,) = [e for e in report["failed"] if e["id"] == "d_anchor"]
    assert "did not land" not in entry["error"]
    assert "already-satisfied" in entry["error"]


# MUTATING half needed the identical kwargs threaded through independently


def _empty_envelope() -> dict[str, Any]:
    return {"directives": [], "judgment_points": [], "decisions": {}}


def test_apply_default_call_threads_none_and_false_into_brief(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _fake_brief(*, decisions=None, env=None, for_date=None, only_mode=False):
        captured["for_date"] = for_date
        captured["only_mode"] = only_mode
        return 0, _empty_envelope()

    monkeypatch.setattr(wc_apply, "brief", _fake_brief)
    exit_code, report = wc_apply.apply()

    assert captured == {"for_date": None, "only_mode": False}
    assert exit_code == int(wc_apply.WorkdayApplyExitCode.SUCCESS)
    assert report["landed"] == []


def test_apply_for_date_alone_threads_for_date_into_brief(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _fake_brief(*, decisions=None, env=None, for_date=None, only_mode=False):
        captured["for_date"] = for_date
        captured["only_mode"] = only_mode
        return 0, _empty_envelope()

    monkeypatch.setattr(wc_apply, "brief", _fake_brief)
    wc_apply.apply(for_date="2026-07-20")

    assert captured == {"for_date": "2026-07-20", "only_mode": False}


def test_apply_for_date_and_only_mode_threads_both_into_brief(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _fake_brief(*, decisions=None, env=None, for_date=None, only_mode=False):
        captured["for_date"] = for_date
        captured["only_mode"] = only_mode
        return 0, _empty_envelope()

    monkeypatch.setattr(wc_apply, "brief", _fake_brief)
    wc_apply.apply(for_date="2026-07-20", only_mode=True)

    assert captured == {"for_date": "2026-07-20", "only_mode": True}


def test_apply_only_mode_without_for_date_is_a_noop_at_the_brief_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _fake_brief(*, decisions=None, env=None, for_date=None, only_mode=False):
        captured["for_date"] = for_date
        captured["only_mode"] = only_mode
        return 0, _empty_envelope()

    monkeypatch.setattr(wc_apply, "brief", _fake_brief)
    wc_apply.apply(only_mode=True)

    assert captured == {"for_date": None, "only_mode": True}


def test_apply_propagates_brief_usage_error_as_transport_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed `for_date` is validated inside `brief()`, never here —
    when `brief()` returns a non-zero (`WorkdayExitCode.USAGE`) exit code
    with an `error` key, `apply()`'s existing non-zero-brief branch must
    map it to `WorkdayApplyExitCode.TRANSPORT_FAIL` carrying that same
    message, exactly as it already does for any other brief-side failure."""

    def _fake_brief(*, decisions=None, env=None, for_date=None, only_mode=False):
        return 2, {"error": f"--for-date must be YYYY-MM-DD (got '{for_date}')"}

    monkeypatch.setattr(wc_apply, "brief", _fake_brief)
    exit_code, report = wc_apply.apply(for_date="not-a-date")

    assert exit_code == int(wc_apply.WorkdayApplyExitCode.TRANSPORT_FAIL)
    assert report["error"] == "--for-date must be YYYY-MM-DD (got 'not-a-date')"
    assert report["landed"] == []


def test_main_threads_for_date_and_only_flags_into_apply(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, Any] = {}

    def _fake_apply(*, decisions=None, for_date=None, only_mode=False):
        captured["decisions"] = decisions
        captured["for_date"] = for_date
        captured["only_mode"] = only_mode
        return 0, {"landed": []}

    monkeypatch.setattr(wc_apply, "apply", _fake_apply)
    rc = wc_apply.main(["--for-date", "2026-07-20", "--only"])

    assert rc == 0
    assert captured == {"decisions": None, "for_date": "2026-07-20", "only_mode": True}
    capsys.readouterr()


def test_main_no_flags_calls_apply_with_defaults(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, Any] = {}

    def _fake_apply(*, decisions=None, for_date=None, only_mode=False):
        captured["decisions"] = decisions
        captured["for_date"] = for_date
        captured["only_mode"] = only_mode
        return 0, {"landed": []}

    monkeypatch.setattr(wc_apply, "apply", _fake_apply)
    rc = wc_apply.main(["--decisions", "{}"])

    assert rc == 0
    assert captured == {"decisions": {}, "for_date": None, "only_mode": False}
    capsys.readouterr()


def test_main_for_date_missing_value_is_transport_fail(capsys: pytest.CaptureFixture[str]) -> None:
    """`--for-date` with no trailing value follows the same shape as the
    existing `--decisions`-missing-value branch: a stderr message and
    `WorkdayApplyExitCode.TRANSPORT_FAIL`, never a crash or a silent
    `None`."""
    rc = wc_apply.main(["--for-date"])
    assert rc == int(wc_apply.WorkdayApplyExitCode.TRANSPORT_FAIL)
    captured = capsys.readouterr()
    assert "--for-date requires a value" in captured.err


def test_main_unknown_argument_still_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    rc = wc_apply.main(["--bogus"])
    assert rc == int(wc_apply.WorkdayApplyExitCode.TRANSPORT_FAIL)
    captured = capsys.readouterr()
    assert "unrecognized argument" in captured.err
