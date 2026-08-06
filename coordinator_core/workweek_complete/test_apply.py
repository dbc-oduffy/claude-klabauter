"""
coordinator_core.workweek_complete.test_apply — dispatch-mechanics tests for
the `workweek-complete` computed-skill engine's apply half (C5).

Purpose: exercises `apply.py`'s directive-execution seam directly against
small synthetic fake CLI modules (never the real `coordinator/bin/wwc-*`
scripts) so these tests stay fast, deterministic, and independent of the
invoking repo's state. Covers the 2026-07-27 finding: `_invoke_cli_main` did
not capture a directive's stderr at all (unlike `workday_complete.apply`/
`workstream_complete.apply`, which capture stdout for inter-directive value
threading — this module's directives never consume one another's stdout, so
that capture was never added here), so a non-zero directive's diagnostic
text never reached `report["results"]`/`report["failed"]`.

Run scoped only:
    python3 -m pytest coordinator_core/workweek_complete/test_apply.py -q
Spec backlink: docs/plans/2026-07-24-b1-ceremony-complete-computed-conversion.md, chunk C5
"""

from __future__ import annotations

import contextlib
import io
import sys
from types import ModuleType
from typing import Any, Callable, Optional

import pytest

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
) -> dict[str, Any]:
    """Mirrors `brief._directive`'s output shape without importing brief —
    this file tests the apply-side consumer of that shape, not the
    assembler that builds it (that's `test_workweek_complete_contract.py`'s
    job)."""
    return {
        "id": id,
        "cli": cli,
        "args": [],
        "depends_on": depends_on,
        "already_satisfied": already_satisfied,
    }


# ---------------------------------------------------------------------------
# _invoke_cli_main — stderr capture in isolation
# ---------------------------------------------------------------------------


def test_invoke_cli_main_calls_argv_taking_main_with_args() -> None:
    seen: dict[str, list[str]] = {}

    def main_fn(argv: list[str]) -> int:
        seen["argv"] = argv
        return 0

    exit_code, stderr_text = wwc_apply._invoke_cli_main(_fake_module(main_fn), ["--flag", "value"])
    assert exit_code == 0
    assert stderr_text == ""
    assert seen["argv"] == ["--flag", "value"]


def test_invoke_cli_main_splices_args_into_sys_argv_for_zero_arg_main() -> None:
    sentinel_argv = list(sys.argv)
    seen: dict[str, list[str]] = {}

    def main_fn() -> int:
        seen["argv"] = list(sys.argv)
        return 0

    exit_code, stderr_text = wwc_apply._invoke_cli_main(_fake_module(main_fn), ["--mode", "pending"])
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

    exit_code, _stderr_text = wwc_apply._invoke_cli_main(_fake_module(main_fn), [])
    assert exit_code == 7


def test_invoke_cli_main_captures_stderr() -> None:
    """2026-07-27 finding: a non-zero directive's diagnostic text (e.g.
    `wsc-tail.py`-shaped exit-2 diagnostics, printed unconditionally to
    `sys.stderr`) must be captured — prior to this fix, `_invoke_cli_main`
    captured nothing at all, so this text was neither captured nor threaded
    into `_dispatch_directive`'s result dict."""

    def main_fn(argv: list[str]) -> int:
        print("diagnostic detail", file=sys.stderr)
        return 2

    exit_code, stderr_text = wwc_apply._invoke_cli_main(_fake_module(main_fn), [])
    assert exit_code == 2
    assert stderr_text == "diagnostic detail\n"


def test_invoke_cli_main_no_main_raises_unrecognized_directive() -> None:
    from coordinator_core.ceremony_common.apply_halt import UnrecognizedDirective

    mod = ModuleType("no_main_cli")
    with pytest.raises(UnrecognizedDirective):
        wwc_apply._invoke_cli_main(mod, [])


# ---------------------------------------------------------------------------
# _execute_directives — stderr threading into the result object
# ---------------------------------------------------------------------------


def test_execute_directives_failing_directive_stderr_survives_into_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-07-27 finding: a non-zero directive's stderr must reach
    `report["results"]` — the one place a caller (the skill, the EM) can
    read it back. Prior to this fix there was no `stderr` key in the result
    dict at all."""

    def failing_main(argv: list[str]) -> int:
        print("diagnostic detail", file=sys.stderr)
        return 2

    modules = {"fake-cli": _fake_module(failing_main, "fake_cli")}
    monkeypatch.setattr(wwc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_plain", "fake-cli")]
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

    modules = {"fake-ok": _fake_module(ok_main, "fake_ok")}
    monkeypatch.setattr(wwc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_ok", "fake-ok")]
    exit_code, report = wwc_apply._execute_directives(directives, [], {})

    assert report["landed"] == ["d_ok"]
    result = next(r for r in report["results"] if r["id"] == "d_ok")
    assert result["stderr"] == ""
    assert exit_code == int(wwc_apply.WorkweekApplyExitCode.SUCCESS)


# ---------------------------------------------------------------------------
# d_step2_resolve_validation_cmd — args must satisfy the real CLI's own
# parser, not a fake stand-in (2026-07-27 arg-mismatch finding: this
# directive shipped with args=[] against a CLI whose main() requires a mode
# flag, so dispatch traded "never reached" for "always exits 1"). Loads the
# real CLI module through apply's own `_load_cli_module` seam so this test
# tracks the CLI's actual argv contract rather than a snapshot of it -- a
# future rename/reshape of the CLI's mode flags fails this test instead of
# silently drifting.
#
# C4a (docs/plans/2026-07-30-diff-scoped-ceremony-gates-elegant.md § Design
# decision 2 / Problem 3) repointed the directive at the validate gate CLI's
# `fast` subcommand (`validate-fast-and-packageability fast`), replacing the
# standalone `coordinator-resolve-validation-cmd --fast` invocation -- the
# gate CLI resolves via its own co-located `_resolver` module attribute
# (`importlib`-loaded, not a top-level `resolve_fast_test_cmd`/
# `resolve_full_test_cmd` pair), and its `fast` subcommand has no `--full`/
# `--read-key` sibling mode to route away from, so the tests below patch
# `cli_module._resolver.resolve_fast_test_cmd` rather than a module-level
# name.
# ---------------------------------------------------------------------------


def _step2_directive() -> dict[str, Any]:
    return next(
        d
        for d in wwc_brief._build_directives()
        if d["id"] == "d_step2_resolve_validation_cmd"
    )


def test_step2_directive_args_are_recognized_by_the_real_cli_parser() -> None:
    """The emitted args must not hit the CLI's own `unknown mode`/usage
    branches -- proof the args are well-formed from the parser's own point
    of view, not just a string we chose to match."""
    directive = _step2_directive()
    cli_module = wwc_apply._load_cli_module(directive["cli"])

    # A CLI hard-failure on the *resolution* itself (e.g. no fast_test_cmd
    # configured for THIS test-runner's own repo, rc=2) is not what this
    # test is checking -- only that argv[0] routes past the "usage: ..." /
    # "unknown mode: ..." branches `main()` prints for a malformed mode.
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
    """Step 2 of workweek-complete.md is titled 'Fast-Tier Validation' and
    its prose says `d_step2_resolve_validation_cmd` resolves `fast_test_cmd`
    -- assert the directive's args actually invoke the gate CLI's `fast`
    subcommand, which resolves via `resolve_fast_test_cmd` (never
    `resolve_full_test_cmd`; the gate CLI's `fast` subcommand has no
    `--full`/`--read-key` sibling to route to)."""
    directive = _step2_directive()
    cli_module = wwc_apply._load_cli_module(directive["cli"])

    calls: list[str] = []

    def _fake_fast(repo_root: Optional[str] = None) -> Any:
        calls.append("fast")
        return cli_module._resolver.ResolveResult("stub-cmd\n", 0, "")

    monkeypatch.setattr(cli_module._resolver, "resolve_fast_test_cmd", _fake_fast)
    # Isolate this test from the diff-scoping/Tier-U/real-execution seams
    # `run_fast` also drives -- this test only asserts *which resolver*
    # the directive's args route to, not the downstream execution path.
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
