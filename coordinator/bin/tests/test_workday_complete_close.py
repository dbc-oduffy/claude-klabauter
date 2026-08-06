"""test_workday_complete_close.py — regression suite for
workday-complete-close.py's five ported bash fences (/workday-complete
Step 4d observer-sidecar stitch, Step 9's changelog-append dispatch gate,
Step 10.5's post-ceremony command hook, Step 10.6's emission-cadence rc
dispatch, Step 3.5 Phase B's per-row backfill dispatch loop).

Covers: stitch-sidecar path construction from compute_machine/local_day and
its hard-fail-on-nonzero contract; step9-dispatch's --only-mode skip (no
subprocess invocation) vs the full forward-flags + RC_VALIDATE/RC_PLUGIN_SUITE
env-default path, returning the sibling's own exit code verbatim;
ceremony-hook's --only-mode skip, stdout re-emission, and always-0-even-on-
WARN contract; emit-cadence's exit-code classification ladder (0 silent /
1,3 informational / 4 escalated) and its always-0 contract regardless of the
sibling's exit code; backfill-dispatch-rows' stdin gap-row parsing (malformed
rows skipped with a WARN), --commit-span flag-building only when both base
and tip are present, --only-mode per-row skip, scope-summary forwarding only
on the --for-date-matched row, the not-detected INFO note, and aggregate
non-zero-on-any-row-failure behavior.

All cases stub the module's `_run` helper (and, for step9, `subprocess.run`
directly, matching how cmd_step9_dispatch invokes it) rather than spawning
the real sibling CLIs — no real git repo, CLAUDE_KLABAUTER_ROOT resolution, or network
access is needed.

Spec backlink: coordinator/bin/workday-complete-close.py
Spec backlink: example-doctrine-repo coordinator/commands/workday-complete.md
    § Step 4d / Step 9 / Step 10.5 / Step 10.6
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout

import pytest

_REPO_ROOT = subprocess.run(
    ["git", "rev-parse", "--show-toplevel"],
    cwd=os.path.dirname(os.path.abspath(__file__)),
    capture_output=True,
    text=True,
    check=True,
).stdout.strip()
_TARGET = os.path.join(_REPO_ROOT, "coordinator", "bin", "workday-complete-close.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("workday_complete_close", _TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


class _FakeResult:
    def __init__(self, returncode: int, stdout: str = ""):
        self.returncode = returncode
        self.stdout = stdout


# --- stitch-sidecar (Step 4d) ---------------------------------------------


def test_stitch_sidecar_success(mod, monkeypatch):
    monkeypatch.setattr(mod, "compute_machine", lambda: "machineA")
    monkeypatch.setattr(mod, "local_day", lambda: "2026-07-23")

    captured = {}

    def fake_run(cli_path, args, capture_stdout=False):
        captured["cli_path"] = cli_path
        captured["args"] = args
        return _FakeResult(0)

    monkeypatch.setattr(mod, "_run", fake_run)

    args = argparse.Namespace(today=None)
    rc = mod.cmd_stitch_sidecar(args)

    assert rc == 0
    assert captured["cli_path"] == mod._STITCH_SIDECAR_CLI
    assert captured["args"] == [
        "archive/daily-summaries/2026-07-23-machineA.md",
        "archive/daily-summaries/2026-07-23-machineA.observer.md",
    ]


def test_stitch_sidecar_today_override(mod, monkeypatch):
    monkeypatch.setattr(mod, "compute_machine", lambda: "machineB")
    monkeypatch.setattr(mod, "local_day", lambda: "2026-07-23")

    captured = {}

    def fake_run(cli_path, args, capture_stdout=False):
        captured["args"] = args
        return _FakeResult(0)

    monkeypatch.setattr(mod, "_run", fake_run)

    args = argparse.Namespace(today="2026-07-01")
    rc = mod.cmd_stitch_sidecar(args)

    assert rc == 0
    assert captured["args"] == [
        "archive/daily-summaries/2026-07-01-machineB.md",
        "archive/daily-summaries/2026-07-01-machineB.observer.md",
    ]


def test_stitch_sidecar_hard_fail_on_nonzero(mod, monkeypatch):
    monkeypatch.setattr(mod, "compute_machine", lambda: "machineA")
    monkeypatch.setattr(mod, "local_day", lambda: "2026-07-23")
    monkeypatch.setattr(mod, "_run", lambda *a, **k: _FakeResult(1))

    args = argparse.Namespace(today=None)
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        rc = mod.cmd_stitch_sidecar(args)

    assert rc == 1
    assert "do NOT re-run this step blind" in stderr.getvalue()


# --- step9-dispatch (Step 9) -----------------------------------------------


def test_step9_dispatch_only_mode_skips_subprocess(mod, monkeypatch):
    def fail_run(*a, **k):
        raise AssertionError("subprocess.run must not be called under --only-mode")

    monkeypatch.setattr(mod.subprocess, "run", fail_run)

    args = argparse.Namespace(
        only_mode=True, no_push=False, dry_run=False,
        commit_span=None, for_date=None, scope_summary=None,
    )
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        rc = mod.cmd_step9_dispatch(args)

    assert rc == 0
    assert "--only set" in stderr.getvalue()
    assert "Step 3.5 Phase B" in stderr.getvalue()


def test_step9_dispatch_forwards_flags_and_env_defaults(mod, monkeypatch):
    captured = {}

    def fake_run(argv, env=None, creationflags=0):
        captured["argv"] = argv
        captured["env"] = env
        return _FakeResult(3)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.delenv("RC_VALIDATE", raising=False)
    monkeypatch.delenv("RC_PLUGIN_SUITE", raising=False)

    args = argparse.Namespace(
        only_mode=False, no_push=True, dry_run=True,
        commit_span="abc..def", for_date="2026-07-01",
        scope_summary="fixed the thing",
    )
    rc = mod.cmd_step9_dispatch(args)

    assert rc == 3
    assert captured["argv"][0] == sys.executable
    assert captured["argv"][1] == str(mod._STEP9_CLI)
    forward = captured["argv"][2:]
    assert "--no-push" in forward
    assert "--dry-run" in forward
    assert forward[forward.index("--commit-span") + 1] == "abc..def"
    assert forward[forward.index("--for-date") + 1] == "2026-07-01"
    assert forward[-1] == "fixed the thing"
    assert captured["env"]["RC_VALIDATE"] == "not-run"
    assert captured["env"]["RC_PLUGIN_SUITE"] == "n/a"


def test_step9_dispatch_preserves_caller_env(mod, monkeypatch):
    captured = {}

    def fake_run(argv, env=None, creationflags=0):
        captured["env"] = env
        return _FakeResult(0)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setenv("RC_VALIDATE", "ok")
    monkeypatch.setenv("RC_PLUGIN_SUITE", "3")

    args = argparse.Namespace(
        only_mode=False, no_push=False, dry_run=False,
        commit_span=None, for_date=None, scope_summary=None,
    )
    rc = mod.cmd_step9_dispatch(args)

    assert rc == 0
    assert captured["env"]["RC_VALIDATE"] == "ok"
    assert captured["env"]["RC_PLUGIN_SUITE"] == "3"


# --- ceremony-hook (Step 10.5) ---------------------------------------------


def test_ceremony_hook_only_mode_skips(mod, monkeypatch):
    def fail_run(*a, **k):
        raise AssertionError("_run must not be called under --only-mode")

    monkeypatch.setattr(mod, "_run", fail_run)

    args = argparse.Namespace(only_mode=True)
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        rc = mod.cmd_ceremony_hook(args)

    assert rc == 0
    assert "skipping post-ceremony command hook" in stderr.getvalue()


def test_ceremony_hook_success_reemits_stdout(mod, monkeypatch):
    monkeypatch.setattr(mod, "_run", lambda *a, **k: _FakeResult(0, stdout="cadence: ok\n"))

    args = argparse.Namespace(only_mode=False)
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        rc = mod.cmd_ceremony_hook(args)

    assert rc == 0
    assert stdout.getvalue().strip() == "cadence: ok"


def test_ceremony_hook_nonzero_is_nonblocking_warn(mod, monkeypatch):
    monkeypatch.setattr(mod, "_run", lambda *a, **k: _FakeResult(1, stdout=""))

    args = argparse.Namespace(only_mode=False)
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        rc = mod.cmd_ceremony_hook(args)

    assert rc == 0
    assert "WARN: ceremony-hook exited non-zero (non-blocking)" in stderr.getvalue()


# --- emit-cadence (Step 10.6) ----------------------------------------------


def test_emit_cadence_success_is_silent(mod, monkeypatch):
    monkeypatch.setattr(mod, "_run", lambda *a, **k: _FakeResult(0))

    args = argparse.Namespace()
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        rc = mod.cmd_emit_cadence(args)

    assert rc == 0
    assert stderr.getvalue() == ""


@pytest.mark.parametrize("rc_from_sibling", [1, 3])
def test_emit_cadence_besteffort_codes_are_informational(mod, monkeypatch, rc_from_sibling):
    monkeypatch.setattr(mod, "_run", lambda *a, **k: _FakeResult(rc_from_sibling))

    args = argparse.Namespace()
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        rc = mod.cmd_emit_cadence(args)

    assert rc == 0
    assert f"exit {rc_from_sibling}" in stderr.getvalue()
    assert "note: emission cadence skipped" in stderr.getvalue()


def test_emit_cadence_exit_4_is_escalated_but_nonblocking(mod, monkeypatch):
    monkeypatch.setattr(mod, "_run", lambda *a, **k: _FakeResult(4))

    args = argparse.Namespace()
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        rc = mod.cmd_emit_cadence(args)

    assert rc == 0
    assert "structural contract-pin failure" in stderr.getvalue()
    assert "will NOT self-heal" in stderr.getvalue()


# --- backfill-dispatch-rows (Step 3.5 Phase B) ------------------------------


def _backfill_args(**overrides):
    defaults = dict(for_date=None, only_mode=False, scope_summary=None, no_push=False, dry_run=False)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _run_backfill(mod, monkeypatch, stdin_text, args, dispatch_rcs=None):
    """Run cmd_backfill_dispatch_rows with stdin stubbed and each
    subprocess.run(step9) call returning the next rc from dispatch_rcs (0 if
    exhausted). Returns (rc, calls, stdout, stderr) where calls is the list
    of forwarded argv lists."""
    dispatch_rcs = list(dispatch_rcs or [])
    calls = []

    def fake_run(argv, env=None, creationflags=0):
        calls.append(argv)
        rc = dispatch_rcs.pop(0) if dispatch_rcs else 0
        return _FakeResult(rc)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mod.sys, "stdin", io.StringIO(stdin_text))

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = mod.cmd_backfill_dispatch_rows(args)
    return rc, calls, out.getvalue(), err.getvalue()


def test_backfill_dispatch_rows_basic_two_rows(mod, monkeypatch):
    stdin_text = "2026-07-01\t3\tabc123\tdef456\n2026-07-02\t1\t\t\n"
    rc, calls, out, err = _run_backfill(mod, monkeypatch, stdin_text, _backfill_args())

    assert rc == 0
    assert err == ""
    assert len(calls) == 2

    row1 = calls[0]
    assert row1[0] == sys.executable
    assert row1[1] == str(mod._STEP9_CLI)
    forward1 = row1[2:]
    assert forward1[forward1.index("--for-date") + 1] == "2026-07-01"
    assert forward1[forward1.index("--commit-span") + 1] == "abc123..def456"

    row2 = calls[1]
    forward2 = row2[2:]
    assert forward2[forward2.index("--for-date") + 1] == "2026-07-02"
    assert "--commit-span" not in forward2


def test_backfill_dispatch_rows_skips_malformed_row(mod, monkeypatch):
    stdin_text = "2026-07-01\t3\tabc\tdef\nnot-enough-fields\n\n2026-07-02\t1\t\t\n"
    rc, calls, out, err = _run_backfill(mod, monkeypatch, stdin_text, _backfill_args())

    assert rc == 0
    assert len(calls) == 2
    assert "WARN" in err
    assert "malformed gap row" in err


def test_backfill_dispatch_rows_only_mode_skips_non_target_rows(mod, monkeypatch):
    stdin_text = "2026-07-01\t3\tabc\tdef\n2026-07-02\t1\tghi\tjkl\n"
    args = _backfill_args(only_mode=True, for_date="2026-07-02")
    rc, calls, out, err = _run_backfill(mod, monkeypatch, stdin_text, args)

    assert rc == 0
    assert len(calls) == 1
    forward = calls[0][2:]
    assert forward[forward.index("--for-date") + 1] == "2026-07-02"


def test_backfill_dispatch_rows_scope_summary_only_on_matched_row(mod, monkeypatch):
    stdin_text = "2026-07-01\t3\tabc\tdef\n2026-07-02\t1\tghi\tjkl\n"
    args = _backfill_args(for_date="2026-07-02", scope_summary="fixed the thing")
    rc, calls, out, err = _run_backfill(mod, monkeypatch, stdin_text, args)

    assert rc == 0
    assert len(calls) == 2
    assert "fixed the thing" not in calls[0]
    assert calls[1][-1] == "fixed the thing"


def test_backfill_dispatch_rows_not_detected_info_note(mod, monkeypatch):
    stdin_text = "2026-07-01\t3\tabc\tdef\n"
    args = _backfill_args(for_date="2026-07-09")
    rc, calls, out, err = _run_backfill(mod, monkeypatch, stdin_text, args)

    assert rc == 0
    assert len(calls) == 1  # only_mode is False, so the one row still dispatches
    assert "INFO: --for-date 2026-07-09 not detected as a gap" in out


def test_backfill_dispatch_rows_forwards_no_push_and_dry_run(mod, monkeypatch):
    stdin_text = "2026-07-01\t3\tabc\tdef\n"
    args = _backfill_args(no_push=True, dry_run=True)
    rc, calls, out, err = _run_backfill(mod, monkeypatch, stdin_text, args)

    assert rc == 0
    forward = calls[0][2:]
    assert "--no-push" in forward
    assert "--dry-run" in forward


def test_backfill_dispatch_rows_aggregates_failure_but_continues(mod, monkeypatch):
    stdin_text = "2026-07-01\t3\tabc\tdef\n2026-07-02\t1\tghi\tjkl\n"
    rc, calls, out, err = _run_backfill(
        mod, monkeypatch, stdin_text, _backfill_args(), dispatch_rcs=[1, 0]
    )

    assert rc == 1
    assert len(calls) == 2  # second row still dispatched despite first failing
    assert "step9-dispatch failed for 2026-07-01" in err


def test_backfill_dispatch_rows_empty_stdin_is_noop(mod, monkeypatch):
    rc, calls, out, err = _run_backfill(mod, monkeypatch, "\n\n", _backfill_args())

    assert rc == 0
    assert calls == []
    assert out == ""
    assert err == ""
