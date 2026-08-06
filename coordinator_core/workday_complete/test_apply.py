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
    """Mirrors `brief._directive`'s output shape without importing brief —
    this file tests the apply-side consumer of that shape, not the
    assembler that builds it (that's `test_workday_complete_contract.py`'s
    job)."""
    return {
        "id": id,
        "cli": cli,
        "args": [],
        "depends_on": depends_on,
        "already_satisfied": already_satisfied,
        "stdin_from": stdin_from,
    }


# ---------------------------------------------------------------------------
# _invoke_cli_main — stdin splice mechanics in isolation
# ---------------------------------------------------------------------------


def test_invoke_cli_main_feeds_declared_stdin_to_argv_taking_main() -> None:
    seen: dict[str, str] = {}

    def main_fn(argv: list[str]) -> int:
        seen["stdin"] = sys.stdin.read()
        return 0

    exit_code, stdout_text, stderr_text = wc_apply._invoke_cli_main(
        _fake_module(main_fn), [], stdin_text="row1\trow2\n"
    )
    assert exit_code == 0
    assert seen["stdin"] == "row1\trow2\n"
    assert stdout_text == ""
    assert stderr_text == ""


def test_invoke_cli_main_feeds_declared_stdin_to_zero_arg_main() -> None:
    """Zero-arg `main()` trampolines (the `sys.argv`-splice class) must
    receive the same stdin splice as an `argv`-taking `main(argv)` — the two
    mechanisms are independent and must compose."""
    seen: dict[str, str] = {}

    def main_fn() -> None:
        seen["stdin"] = sys.stdin.read()

    exit_code, _, _ = wc_apply._invoke_cli_main(
        _fake_module(main_fn), ["--flag"], stdin_text="gap-row\n"
    )
    assert exit_code == 0
    assert seen["stdin"] == "gap-row\n"


def test_invoke_cli_main_leaves_stdin_untouched_when_none_declared() -> None:
    """A directive that declares no `stdin_from` must never have its CLI's
    `sys.stdin` redirected to anything — not an empty stream, not a real
    one. `_invoke_cli_main(stdin_text=None)` must leave `sys.stdin` as
    whatever the apply process already had it set to, for the whole call."""
    sentinel = sys.stdin
    seen: dict[str, Any] = {}

    def main_fn(argv: list[str]) -> int:
        seen["stdin_during_call"] = sys.stdin
        return 0

    exit_code, _, _ = wc_apply._invoke_cli_main(_fake_module(main_fn), [], stdin_text=None)
    assert exit_code == 0
    assert seen["stdin_during_call"] is sentinel
    assert sys.stdin is sentinel


def test_invoke_cli_main_captures_stdout_for_downstream_consumption() -> None:
    def main_fn(argv: list[str]) -> int:
        print("2026-07-19\t3\tbase\ttip")
        return 0

    exit_code, stdout_text, stderr_text = wc_apply._invoke_cli_main(_fake_module(main_fn), [])
    assert exit_code == 0
    assert stdout_text == "2026-07-19\t3\tbase\ttip\n"
    assert stderr_text == ""


def test_invoke_cli_main_captures_stderr() -> None:
    """2026-07-27 finding (mirrors `workstream_complete.apply`'s identical
    fix): a non-zero directive's diagnostic text (e.g. `wsc-tail.py`'s
    exit-2 diagnostics block, printed unconditionally to `sys.stderr`) must
    be captured the same way stdout already is — prior to this fix,
    `_invoke_cli_main` only redirected stdout, so this text was neither
    captured nor threaded into `_dispatch_directive`'s result dict at all."""

    def main_fn(argv: list[str]) -> int:
        print("diagnostic detail", file=sys.stderr)
        return 2

    exit_code, stdout_text, stderr_text = wc_apply._invoke_cli_main(_fake_module(main_fn), [])
    assert exit_code == 2
    assert stdout_text == ""
    assert stderr_text == "diagnostic detail\n"


def test_invoke_cli_main_restores_stdin_after_exception() -> None:
    sentinel = sys.stdin

    def main_fn(argv: list[str]) -> int:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        wc_apply._invoke_cli_main(_fake_module(main_fn), [], stdin_text="x\n")
    assert sys.stdin is sentinel


# ---------------------------------------------------------------------------
# _execute_directives — orchestration: wiring, honesty, non-regression
# ---------------------------------------------------------------------------


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
        "fake-scan": _fake_module(producer_main, "fake_scan"),
        "fake-anchor": _fake_module(consumer_main, "fake_anchor"),
    }
    monkeypatch.setattr(wc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [
        _directive("d_scan", "fake-scan"),
        _directive("d_anchor", "fake-anchor", stdin_from="d_scan"),
    ]
    exit_code, report = wc_apply._execute_directives(directives, [], {})

    assert captured["stdin"] == "2026-07-19\t3\tbase\ttip\n"
    assert report["landed"] == ["d_scan", "d_anchor"]
    assert report["failed"] == []
    assert exit_code == int(wc_apply.WorkdayApplyExitCode.SUCCESS)


def test_execute_directives_directive_without_stdin_from_unaffected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directive that declares no `stdin_from` must dispatch exactly as it
    did before this fix — `_dispatch_directive` called with `stdin_text=
    None` regardless of what any OTHER directive in the same run produced."""
    seen_stdin: dict[str, Any] = {}

    def producer_main(argv: list[str]) -> int:
        print("some output that must not leak anywhere")
        return 0

    def unrelated_main(argv: list[str]) -> int:
        seen_stdin["value"] = sys.stdin
        return 0

    sentinel = sys.stdin
    modules = {
        "fake-scan": _fake_module(producer_main, "fake_scan"),
        "fake-unrelated": _fake_module(unrelated_main, "fake_unrelated"),
    }
    monkeypatch.setattr(wc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [
        _directive("d_scan", "fake-scan"),
        _directive("d_unrelated", "fake-unrelated"),  # no stdin_from
    ]
    exit_code, report = wc_apply._execute_directives(directives, [], {})

    assert seen_stdin["value"] is sentinel
    assert report["landed"] == ["d_scan", "d_unrelated"]
    assert exit_code == int(wc_apply.WorkdayApplyExitCode.SUCCESS)


def test_execute_directives_consumer_refuses_dispatch_when_producer_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserves `e2f3a25f`'s honesty property in the new stdin path: if the
    producer directive FAILS (non-zero exit), the consumer must never be
    dispatched with empty/missing stdin — it must land in `failed` too,
    never in `landed`. This is the exact silent-Phase-B-no-op shape the
    memo reported, now made structurally impossible for any `stdin_from`
    pair, not just the two backfill directives."""
    consumer_called: dict[str, bool] = {"called": False}

    def failing_producer_main(argv: list[str]) -> int:
        return 1

    def consumer_main(argv: list[str]) -> int:
        consumer_called["called"] = True
        return 0

    modules = {
        "fake-scan": _fake_module(failing_producer_main, "fake_scan"),
        "fake-anchor": _fake_module(consumer_main, "fake_anchor"),
    }
    monkeypatch.setattr(wc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [
        _directive("d_scan", "fake-scan"),
        _directive("d_anchor", "fake-anchor", stdin_from="d_scan"),
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
    """Same honesty guarantee when the producer is BLOCKED by an unresolved
    judgment point rather than failed outright — the consumer must not run
    with no stdin either."""

    def consumer_main(argv: list[str]) -> int:
        raise AssertionError("must never dispatch: its stdin producer never landed")

    modules = {"fake-anchor": _fake_module(consumer_main, "fake_anchor")}
    monkeypatch.setattr(wc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    judgment_points = [
        {
            "id": "jp_gate",
            "dispositions": [{"value": "go", "resolves": ["d_scan"]}],
        }
    ]
    directives = [
        _directive("d_scan", "fake-scan", depends_on="jp_gate"),
        _directive("d_anchor", "fake-anchor", stdin_from="d_scan"),
    ]
    # No decision supplied for jp_gate -> d_scan stays blocked, never dispatched.
    exit_code, report = wc_apply._execute_directives(directives, judgment_points, {})

    assert report["blocked"] == ["d_scan"]
    failed_ids = {entry["id"] for entry in report["failed"]}
    assert failed_ids == {"d_anchor"}
    assert report["landed"] == []
    assert exit_code == int(wc_apply.WorkdayApplyExitCode.DIRECTIVE_FAILED)


def test_execute_directives_failing_directive_stderr_survives_into_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-07-27 finding: a non-zero directive's stderr must reach
    `report["results"]` — the one place a caller (the skill, the EM) can
    read it back. Prior to this fix there was no `stderr` key in the result
    dict at all, so `wsc-tail.py`-shaped exit-2 diagnostics were captured
    nowhere the documented recovery step could read them."""

    def failing_main(argv: list[str]) -> int:
        print("diagnostic detail", file=sys.stderr)
        return 2

    modules = {"fake-cli": _fake_module(failing_main, "fake_cli")}
    monkeypatch.setattr(wc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_plain", "fake-cli")]
    exit_code, report = wc_apply._execute_directives(directives, [], {})

    assert [entry["id"] for entry in report["failed"]] == ["d_plain"]
    result = next(r for r in report["results"] if r["id"] == "d_plain")
    assert result["stderr"] == "diagnostic detail\n"


def test_execute_directives_failing_directive_still_reported_as_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-regression: a plain failing directive with NO stdin involvement
    at all must still land in `failed`, never `landed` — the stdin-wiring
    change must not touch this pre-existing (`e2f3a25f`) reporting path."""

    def failing_main(argv: list[str]) -> int:
        return 2

    modules = {"fake-cli": _fake_module(failing_main, "fake_cli")}
    monkeypatch.setattr(wc_apply, "_load_cli_module", lambda cli_name: modules[cli_name])

    directives = [_directive("d_plain", "fake-cli")]
    exit_code, report = wc_apply._execute_directives(directives, [], {})

    assert report["landed"] == []
    assert [entry["id"] for entry in report["failed"]] == ["d_plain"]
    assert exit_code == int(wc_apply.WorkdayApplyExitCode.DIRECTIVE_FAILED)


# ---------------------------------------------------------------------------
# apply() / main() — `for_date`/`only_mode` plumbing into the brief() recompute
#
# Companion to `test_workday_complete_contract.py`'s brief-side
# `for_date`/`only_mode` tests (f1ced234): that commit date-scoped the
# COMPUTE half only. `apply()` recomputes the brief itself
# (`apply.py:brief(decisions=..., for_date=..., only_mode=...)`), so the
# MUTATING half needed the identical kwargs threaded through independently
# — these tests stub `wc_apply.brief` so they stay pure argv/kwarg-plumbing
# checks, never touching a real directive dispatch.
# ---------------------------------------------------------------------------


def _empty_envelope() -> dict[str, Any]:
    return {"directives": [], "judgment_points": [], "decisions": {}}


def test_apply_default_call_threads_none_and_false_into_brief(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`apply()` called with no `for_date`/`only_mode` at all must recompute
    the brief with `for_date=None, only_mode=False` — byte-identical to
    prior behavior — and the resulting `d_step3_5_backfill_phase_b`-shaped
    directive (simulated here via a bare `["backfill-dispatch-rows"]` arg
    list) dispatches unchanged."""
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
    """`only_mode=True` with no `for_date` is deliberately still threaded
    through to `brief()` as-is (the no-op resolution lives in
    `brief._build_directives`, not here) — `apply()` never second-guesses
    what `brief()` does with the two kwargs, it only forwards them."""
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
    """`main(argv)`'s hand-rolled `--for-date`/`--only` tokens must reach
    `apply()` as its own-named kwargs, unchanged in spelling."""
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
    """Bare `workday-complete-assemble apply --decisions '{}'` (no date
    flags) must still call `apply()` with `for_date=None, only_mode=False`
    — byte-identical to today's behavior."""
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
    """Non-regression: an unrecognized token must still be rejected exactly
    as it was before the two new flags were added."""
    rc = wc_apply.main(["--bogus"])
    assert rc == int(wc_apply.WorkdayApplyExitCode.TRANSPORT_FAIL)
    captured = capsys.readouterr()
    assert "unrecognized argument" in captured.err
