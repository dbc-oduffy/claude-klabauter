"""Oracle for `workday-complete-close.py::cmd_backfill_dispatch_rows::_dispatch_step9_row`.

The register mis-filed this site under `structural-floor`; the load-bearing reasons are stated
in-source and never made it into the ledger: an explicit PER-ROW ISOLATION CONTRACT (a
`TimeoutExpired` must fail THIS row only, never abort the rest of the backfill loop) plus
per-item rc demultiplexing (`overall_rc` aggregation with a per-date error line). A batched form
would also collapse N days of changelog history into ONE commit, which is a behaviour change,
not a batching -- that half of the claim is not re-verified here, only the isolation contract is.

A static AST predicate was considered and rejected for cause (see
`state/audits/2026-08-19-amplification-register-remaining-fourteen-dispositions.md`): "callee's
per-iteration return consumed for per-item control flow" would silence a large fraction of real
amplification, because a batched call can return per-item results too. Only a behavioural test
can tell isolation-on-failure apart from ordinary per-item result consumption.

Bound to the site by `_ORACLE_CLAIMS` in
`coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py`. No subprocess is spawned --
`subprocess.run` is monkeypatched inside the loaded module, so this runs on the fast tier.
"""

from __future__ import annotations

import argparse
import io
import subprocess

import pytest

from coordinator_core.tests.oracles._sibling_cli import load_bin_module

_GAP_ROWS = (
    "2026-01-01\t2\tbaseA\ttipA\n"
    "2026-01-02\t3\tbaseB\ttipB\n"
    "2026-01-03\t1\tbaseC\ttipC\n"
)
_ALL_DATES = {"2026-01-01", "2026-01-02", "2026-01-03"}


def _backfill_args(**overrides) -> argparse.Namespace:
    base = dict(for_date=None, only_mode=False, scope_summary=None, no_push=False, dry_run=True)
    base.update(overrides)
    return argparse.Namespace(**base)


def _date_from_argv(argv: list[str]) -> str:
    return argv[argv.index("--for-date") + 1]


def _run_backfill(module, monkeypatch, fake_run) -> tuple[int, list[str]]:
    """Wire `fake_run` in as `subprocess.run` for the duration of the call and return
    (overall_rc, dates dispatched in call order)."""
    calls: list[str] = []

    def _tracking_run(argv, *args, **kwargs):
        calls.append(_date_from_argv(argv))
        return fake_run(argv, *args, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", _tracking_run)
    monkeypatch.setattr(module.sys, "stdin", io.StringIO(_GAP_ROWS))
    rc = module.cmd_backfill_dispatch_rows(_backfill_args())
    return rc, calls


def test_one_row_failure_does_not_abort_remaining_rows(monkeypatch):
    """A non-zero rc from one row must still leave every other row dispatched, and the
    aggregate rc must reflect the failure (not swallow it)."""
    module = load_bin_module("workday-complete-close.py")

    def fake_run(argv, *args, **kwargs):
        date = _date_from_argv(argv)
        rc = 1 if date == "2026-01-02" else 0
        return subprocess.CompletedProcess(args=argv, returncode=rc)

    overall_rc, calls = _run_backfill(module, monkeypatch, fake_run)

    assert calls == sorted(_ALL_DATES), (
        "one row's non-zero rc stopped the remaining rows from being dispatched -- the "
        "per-row isolation contract has been broken. Expected all three dates dispatched "
        f"in order, got {calls!r}."
    )
    assert overall_rc == 1, (
        "a failing row's rc did not propagate into the aggregate return -- "
        "overall_rc demultiplexing is broken."
    )


def test_one_row_timeout_does_not_abort_remaining_rows(monkeypatch):
    """A `TimeoutExpired` from one row's subprocess call must fail THIS row only -- the
    contract the in-source comment names explicitly -- never abort the loop."""
    module = load_bin_module("workday-complete-close.py")

    def fake_run(argv, *args, **kwargs):
        date = _date_from_argv(argv)
        if date == "2026-01-02":
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 120))
        return subprocess.CompletedProcess(args=argv, returncode=0)

    overall_rc, calls = _run_backfill(module, monkeypatch, fake_run)

    assert calls == sorted(_ALL_DATES), (
        "a TimeoutExpired on one row stopped the remaining rows from being dispatched -- "
        f"expected all three dates dispatched, got {calls!r}."
    )
    assert overall_rc == 1, (
        "a timed-out row did not register as a failure in the aggregate rc."
    )


def test_repeated_for_date_flag_is_last_wins():
    """Secondary oracle: `--for-date` is a plain `store` action on the production parser, so
    two occurrences on one invocation resolve to the SECOND -- last-wins, exactly one day
    processed. Exercises the real `main()` parser (not a re-declared copy) by stubbing the
    dispatch command out before `args.func(args)` runs, so no stdin read or subprocess spawn
    is reached."""
    module = load_bin_module("workday-complete-close.py")
    captured: list[argparse.Namespace] = []

    def _stub_cmd(args: argparse.Namespace) -> int:
        captured.append(args)
        return 0

    original = module.cmd_backfill_dispatch_rows
    module.cmd_backfill_dispatch_rows = _stub_cmd
    try:
        rc = module.main(
            [
                "backfill-dispatch-rows",
                "--for-date",
                "2026-01-01",
                "--for-date",
                "2026-01-02",
                "--dry-run",
            ]
        )
    finally:
        module.cmd_backfill_dispatch_rows = original

    assert rc == 0
    assert len(captured) == 1
    assert captured[0].for_date == "2026-01-02", (
        "a repeated --for-date no longer resolves to the last occurrence -- the "
        "'--only-mode requires --for-date, and processes exactly one day' claim rests on "
        "this being a plain store action, not an append."
    )


def test_oracle_target_remains_importable():
    """The two isolation-contract oracles above are worth nothing if the module they pin
    stops being importable or loses the functions they call."""
    module = load_bin_module("workday-complete-close.py")
    for name in ("cmd_backfill_dispatch_rows", "_dispatch_step9_row", "main"):
        assert callable(getattr(module, name, None)), (
            f"workday-complete-close.py no longer exposes a callable {name}() -- the "
            "isolation-contract oracle pinned to it cannot verify anything until this "
            "is repaired."
        )
