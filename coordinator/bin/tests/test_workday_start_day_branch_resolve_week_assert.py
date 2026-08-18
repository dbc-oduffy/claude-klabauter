"""test_workday_start_day_branch_resolve_week_assert.py — coverage for
workday-start-day-branch-resolve.py's `day-branch-assert` subcommand (C6,
AC-6 of DoE-claude docs/plans/2026-08-18-enforce-day-branch-cut-tree-invariant.md).

Pins three things:
    1. The subcommand is actually wired into the CLI dispatch table — a
       correct `cmd_day_branch_assert` that main() never routes to is the
       GUARD-WIRING-SILENT-SKIP shape this whole workstream exists to close.
    2. `cmd_day_branch_assert` calls the SAME
       `coordinator_core.hooks.day_branch_assert.assert_day_branch` C4b's
       SessionStart shim calls — not a re-implementation.
    3. Its warn/refusal text is `day_branch_assert.banner()`'s own output,
       not a second renderer printing similar-but-different text — asserted
       by monkeypatching `assert_day_branch` to return a WARN result whose
       message came from a real `banner()` call, and checking the CLI prints
       that exact string unchanged.

Spec backlink: DoE-claude docs/plans/2026-08-18-enforce-day-branch-cut-tree-invariant.md
    § C6
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys

import pytest

pytestmark = pytest.mark.cadence

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
_REPO_ROOT = os.path.dirname(os.path.dirname(_BIN_DIR))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_SCRIPT_PATH = os.path.join(_BIN_DIR, "workday-start-day-branch-resolve.py")
_spec = importlib.util.spec_from_file_location("workday_start_day_branch_resolve_week_assert_module", _SCRIPT_PATH)
_wsdbr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_wsdbr)  # type: ignore[union-attr]

from coordinator_core.hooks.day_branch_assert import (  # noqa: E402
    ADOPTED,
    CUT,
    DayBranchAssertResult,
    FAILED,
    INHERITED,
    WARN,
    banner,
)


def test_main_routes_day_branch_assert_subcommand(monkeypatch):
    """Wiring pin: `main()` must actually reach `cmd_day_branch_assert` for
    the subcommand name — a subparser that exists but is never dispatched
    is the silent-skip shape."""
    called = {"hit": False}

    def fake_cmd(args):
        called["hit"] = True
        assert args.subcommand == "day-branch-assert"
        return 0

    monkeypatch.setattr(_wsdbr, "cmd_day_branch_assert", fake_cmd)

    rc = _wsdbr.main(["day-branch-assert"])

    assert rc == 0
    assert called["hit"] is True


def test_day_branch_assert_calls_the_shared_engine_dispatch(monkeypatch, tmp_path):
    """`cmd_day_branch_assert` must call `assert_day_branch` itself (the same
    function C4b's SessionStart shim imports and calls) rather than
    re-implementing any part of the boot dispatch."""
    calls = []

    def fake_assert(repo_root, machine, today, **kwargs):
        calls.append((repo_root, machine, today))
        return DayBranchAssertResult(CUT, "work/host/2026-08-18", "day-branch: cut work/host/2026-08-18")

    monkeypatch.setattr(
        "coordinator_core.hooks.day_branch_assert.assert_day_branch", fake_assert
    )
    monkeypatch.setattr(
        "coordinator_core.machine_resolver.compute_machine", lambda: "host"
    )
    monkeypatch.setattr("coordinator_core.daily_day.local_day", lambda: "2026-08-18")

    rc = _wsdbr.cmd_day_branch_assert(argparse.Namespace(repo_root=str(tmp_path)))

    assert rc == 0
    assert calls == [(str(tmp_path), "host", "2026-08-18")]


@pytest.mark.parametrize("outcome,branch", [(ADOPTED, "work/host/2026-08-18"), (INHERITED, "work/host/2026-08-18")])
def test_day_branch_assert_settled_outcomes_exit_zero(monkeypatch, tmp_path, outcome, branch):
    monkeypatch.setattr(
        "coordinator_core.hooks.day_branch_assert.assert_day_branch",
        lambda *a, **k: DayBranchAssertResult(outcome, branch, f"day-branch: {outcome.lower()} {branch}"),
    )
    monkeypatch.setattr("coordinator_core.machine_resolver.compute_machine", lambda: "host")
    monkeypatch.setattr("coordinator_core.daily_day.local_day", lambda: "2026-08-18")

    rc = _wsdbr.cmd_day_branch_assert(argparse.Namespace(repo_root=str(tmp_path)))

    assert rc == 0


def test_day_branch_assert_failed_outcome_exits_nonzero(monkeypatch, tmp_path, capsys):
    fail_message = banner(
        headline="day-branch NOT cut — tree is still on main",
        detail="the cut was attempted and failed",
        since=None,
    )
    monkeypatch.setattr(
        "coordinator_core.hooks.day_branch_assert.assert_day_branch",
        lambda *a, **k: DayBranchAssertResult(FAILED, "main", fail_message),
    )
    monkeypatch.setattr("coordinator_core.machine_resolver.compute_machine", lambda: "host")
    monkeypatch.setattr("coordinator_core.daily_day.local_day", lambda: "2026-08-18")

    rc = _wsdbr.cmd_day_branch_assert(argparse.Namespace(repo_root=str(tmp_path)))

    assert rc == 1
    out = capsys.readouterr().out
    # The CLI must print banner()'s own rendered text VERBATIM -- not a
    # second, similar-but-different renderer (AC-1 constraint for this
    # mid-session path).
    assert fail_message in out
    assert "day-branch NOT cut" in out


def test_day_branch_assert_warn_message_is_bannerrendered_verbatim(monkeypatch, tmp_path, capsys):
    warn_message = banner(
        headline="detached HEAD",
        detail="auto-push cannot run and crash insurance is NOT in force",
        since=None,
    )
    monkeypatch.setattr(
        "coordinator_core.hooks.day_branch_assert.assert_day_branch",
        lambda *a, **k: DayBranchAssertResult(WARN, "", warn_message),
    )
    monkeypatch.setattr("coordinator_core.machine_resolver.compute_machine", lambda: "host")
    monkeypatch.setattr("coordinator_core.daily_day.local_day", lambda: "2026-08-18")

    rc = _wsdbr.cmd_day_branch_assert(argparse.Namespace(repo_root=str(tmp_path)))

    assert rc == 0  # WARN is reported, not blocking -- same posture as the boot shim
    out = capsys.readouterr().out
    assert warn_message in out


def test_day_branch_assert_defaults_repo_root_to_cwd(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        "coordinator_core.hooks.day_branch_assert.assert_day_branch",
        lambda repo_root, machine, today, **k: (
            calls.append(repo_root),
            DayBranchAssertResult(CUT, "work/host/2026-08-18", ""),
        )[1],
    )
    monkeypatch.setattr("coordinator_core.machine_resolver.compute_machine", lambda: "host")
    monkeypatch.setattr("coordinator_core.daily_day.local_day", lambda: "2026-08-18")
    monkeypatch.chdir(tmp_path)

    _wsdbr.cmd_day_branch_assert(argparse.Namespace(repo_root=None))

    assert calls == [os.getcwd()]
