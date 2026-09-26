
from __future__ import annotations

import contextlib
import io
import sys
from types import ModuleType
from typing import Any, Callable, Optional

import pytest

from coordinator_core.ceremony_common.cli_rejection import CliExitClass
from coordinator_core.workweek_complete import apply as wwc_apply
from coordinator_core.workweek_complete import brief as wwc_brief


def _fake_module(main_fn: Callable[..., Any], name: str = "fake_cli") -> ModuleType:
    mod = ModuleType(name)
    mod.main = main_fn
    return mod


def _directive(
    id: str,
    cli: str,
    *,
    depends_on: Optional[str] = None,
    already_satisfied: bool = False,
    best_effort: bool = False,
) -> dict[str, Any]:
    return {
        "id": id,
        "cli": cli,
        "args": [],
        "depends_on": depends_on,
        "already_satisfied": already_satisfied,
        "best_effort": best_effort,
    }


def test_invoke_cli_main_calls_argv_taking_main_with_args() -> None:
    seen: dict[str, list[str]] = {}

    def main_fn(argv: list[str]) -> int:
        seen["argv"] = argv
        return 0

    exit_code, stderr_text, _exit_class = wwc_apply._invoke_cli_main(
        _fake_module(main_fn), ["--flag", "value"]
    )
    assert exit_code == 0
    assert stderr_text == ""
    assert seen["argv"] == ["--flag", "value"]


def test_invoke_cli_main_splices_args_into_sys_argv_for_zero_arg_main() -> None:
    sentinel_argv = list(sys.argv)
    seen: dict[str, list[str]] = {}

    def main_fn() -> int:
        seen["argv"] = list(sys.argv)
        return 0

    exit_code, stderr_text, _exit_class = wwc_apply._invoke_cli_main(
        _fake_module(main_fn), ["--mode", "pending"]
    )
    assert exit_code == 0
    assert stderr_text == ""
    assert seen["argv"][1:] == ["--mode", "pending"]
    assert sys.argv == sentinel_argv


def test_invoke_cli_main_restores_sys_argv_after_exception() -> None:
    sentinel_argv = list(sys.argv)

    def main_fn() -> int:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        wwc_apply._invoke_cli_main(_fake_module(main_fn), ["--x"])
    assert sys.argv == sentinel_argv


def test_invoke_cli_main_resolves_system_exit_int_code() -> None:
    def main_fn(argv: list[str]) -> None:
        raise SystemExit(7)

    exit_code, _stderr_text, exit_class = wwc_apply._invoke_cli_main(_fake_module(main_fn), [])
    assert exit_code == 7
    assert exit_class is CliExitClass.RETURNED


def test_invoke_cli_main_captures_stderr() -> None:

    def main_fn(argv: list[str]) -> int:
        print("diagnostic detail", file=sys.stderr)
        return 2

    exit_code, stderr_text, exit_class = wwc_apply._invoke_cli_main(_fake_module(main_fn), [])
    assert exit_code == 2
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

    exit_code, stderr_text, exit_class = wwc_apply._invoke_cli_main(_fake_module(main_fn), [])
    assert exit_code == 2
    assert exit_class is CliExitClass.ARGV_REJECTED


def test_invoke_cli_main_no_main_raises_unrecognized_directive() -> None:
    from coordinator_core.ceremony_common.apply_halt import UnrecognizedDirective

    mod = ModuleType("no_main_cli")
    with pytest.raises(UnrecognizedDirective):
        wwc_apply._invoke_cli_main(mod, [])


def test_execute_directives_failing_directive_stderr_survives_into_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    def failing_main(argv: list[str]) -> int:
        print("diagnostic detail", file=sys.stderr)
        return 2

    modules = {"query-records": _fake_module(failing_main, "fake_cli")}
    monkeypatch.setattr(wwc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_plain", "query-records")]
    exit_code, report = wwc_apply._execute_directives(directives, [], {})

    assert [entry["id"] for entry in report["failed"]] == ["d_plain"]
    result = next(r for r in report["results"] if r["id"] == "d_plain")
    assert result["stderr"] == "diagnostic detail\n"
    assert exit_code == int(wwc_apply.WorkweekApplyExitCode.DIRECTIVE_FAILED)


def test_execute_directives_landing_directive_reports_empty_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def ok_main(argv: list[str]) -> int:
        return 0

    modules = {"list-week-changelog": _fake_module(ok_main, "fake_ok")}
    monkeypatch.setattr(wwc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_ok", "list-week-changelog")]
    exit_code, report = wwc_apply._execute_directives(directives, [], {})

    assert report["landed"] == ["d_ok"]
    result = next(r for r in report["results"] if r["id"] == "d_ok")
    assert result["stderr"] == ""
    assert exit_code == int(wwc_apply.WorkweekApplyExitCode.SUCCESS)


def test_best_effort_directive_failure_lands_in_degraded_not_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_main(argv: list[str]) -> int:
        return 2

    modules = {"query-records": _fake_module(failing_main, "fake_cli")}
    monkeypatch.setattr(wwc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_best_effort", "query-records", best_effort=True)]
    exit_code, report = wwc_apply._execute_directives(directives, [], {})

    assert report["failed"] == []
    assert [entry["id"] for entry in report["degraded"]] == ["d_best_effort"]
    assert report["landed"] == []


def test_best_effort_directive_failure_alone_still_reports_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2: a run where every non-best_effort directive is clean and only a
    best_effort directive fails must return SUCCESS, not PARTIAL_MUTATION —
    a degraded-only run must never read as "reconcile before re-running"."""

    def failing_main(argv: list[str]) -> int:
        return 2

    def ok_main(argv: list[str]) -> int:
        return 0

    modules = {
        "coordinator-ceremony-hook": _fake_module(failing_main, "fake_best_effort"),
        "list-week-changelog": _fake_module(ok_main, "fake_ok"),
    }
    monkeypatch.setattr(wwc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [
        _directive("d_ok", "list-week-changelog"),
        _directive("d_best_effort", "coordinator-ceremony-hook", best_effort=True),
    ]
    exit_code, report = wwc_apply._execute_directives(directives, [], {})

    assert exit_code == int(wwc_apply.WorkweekApplyExitCode.SUCCESS)
    assert report["landed"] == ["d_ok"]
    assert [entry["id"] for entry in report["degraded"]] == ["d_best_effort"]
    assert report["failed"] == []


def test_non_best_effort_directive_failure_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    def failing_main(argv: list[str]) -> int:
        return 2

    modules = {"query-records": _fake_module(failing_main, "fake_cli")}
    monkeypatch.setattr(wwc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_plain", "query-records")]
    exit_code, report = wwc_apply._execute_directives(directives, [], {})

    assert [entry["id"] for entry in report["failed"]] == ["d_plain"]
    assert report["degraded"] == []
    assert exit_code == int(wwc_apply.WorkweekApplyExitCode.DIRECTIVE_FAILED)


def test_degraded_entry_error_folds_in_captured_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    def failing_main(argv: list[str]) -> int:
        print("ValueError: unrecognized handoff deployment_state 'record'", file=sys.stderr)
        return 3

    modules = {"query-records": _fake_module(failing_main, "fake_cli")}
    monkeypatch.setattr(wwc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_best_effort", "query-records", best_effort=True)]
    _exit_code, report = wwc_apply._execute_directives(directives, [], {})

    entry = report["degraded"][0]
    assert entry["error"].startswith("query-records exited 3 (args=[])")
    assert "unrecognized handoff deployment_state" in entry["error"]


def test_failed_entry_error_still_folds_in_captured_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    def failing_main(argv: list[str]) -> int:
        print("diagnostic detail", file=sys.stderr)
        return 2

    modules = {"query-records": _fake_module(failing_main, "fake_cli")}
    monkeypatch.setattr(wwc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_plain", "query-records")]
    _exit_code, report = wwc_apply._execute_directives(directives, [], {})

    entry = report["failed"][0]
    assert entry["error"].startswith("query-records exited 2 (args=[])")
    assert "diagnostic detail" in entry["error"]


def test_clean_directive_error_has_no_trailing_stderr_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    def failing_main(argv: list[str]) -> int:
        return 2

    modules = {"query-records": _fake_module(failing_main, "fake_cli")}
    monkeypatch.setattr(wwc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_plain", "query-records")]
    _exit_code, report = wwc_apply._execute_directives(directives, [], {})

    entry = report["failed"][0]
    assert entry["error"] == "query-records exited 2 (args=[])"


def test_execute_directives_ignores_hard_block_key_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    def failing_main(argv: list[str]) -> int:
        return 2

    modules = {"query-records": _fake_module(failing_main, "fake_cli")}
    monkeypatch.setattr(wwc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directive = _directive("d_best_effort", "query-records", best_effort=True)
    directive["hard_block"] = True
    _exit_code, report = wwc_apply._execute_directives([directive], [], {})

    assert [entry["id"] for entry in report["degraded"]] == ["d_best_effort"]
    assert report["failed"] == []


def _step2_directive() -> dict[str, Any]:
    return next(
        d
        for d in wwc_brief._build_directives()
        if d["id"] == "d_step2_resolve_validation_cmd"
    )


def test_step2_directive_args_are_recognized_by_the_real_cli_parser() -> None:
    directive = _step2_directive()
    cli_module = wwc_apply._load_cli_module(directive["cli"])

    captured_stderr = io.StringIO()
    with contextlib.redirect_stderr(captured_stderr):
        cli_module.main(list(directive["args"]))
    stderr_text = captured_stderr.getvalue()
    assert "usage:" not in stderr_text, (
        f"directive args {directive['args']!r} did not satisfy the CLI's own "
        f"parser (empty argv usage branch): {stderr_text!r}"
    )
    assert "unknown mode" not in stderr_text, (
        f"directive args {directive['args']!r} named a mode the CLI's own "
        f"parser doesn't recognize: {stderr_text!r}"
    )


def test_step2_directive_routes_to_the_fast_tier_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directive = _step2_directive()
    cli_module = wwc_apply._load_cli_module(directive["cli"])

    calls: list[str] = []

    def _fake_fast(repo_root: Optional[str] = None) -> Any:
        calls.append("fast")
        return cli_module._resolver.ResolveResult("stub-cmd\n", 0, "")

    monkeypatch.setattr(cli_module._resolver, "resolve_fast_test_cmd", _fake_fast)
    from coordinator_core.session.tier_u_gate import TierUGateResult

    monkeypatch.setattr(cli_module, "find_changed_test_files", lambda repo_root=None: [])
    monkeypatch.setattr(
        cli_module,
        "enforce_tier_u_gate",
        lambda cmd, *, repo_root=None: TierUGateResult(proceed=True),
    )
    monkeypatch.setattr(cli_module, "_run_resolved_command", lambda cmd: 0)

    exit_code = cli_module.main(list(directive["args"]))

    assert calls == ["fast"], (
        f"expected d_step2_resolve_validation_cmd's args {directive['args']!r} "
        f"to route to resolve_fast_test_cmd exactly once, got {calls!r}"
    )
    assert exit_code == 0
