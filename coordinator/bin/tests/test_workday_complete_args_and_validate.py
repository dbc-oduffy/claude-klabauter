"""test_workday_complete_args_and_validate.py — regression suite for
workday-complete-args-and-validate.py's three ported subcommands: front-door
arg parsing, cross-machine guard, and Step-1 rc-forwarding.

Spec backlink: coordinator/bin/workday-complete-args-and-validate.py
(port of commands/workday-complete.md § Argument Parsing (Front Door),
§ Step 1: Validate — example-doctrine-repo repo, M3 extirpation wave WDC-1).
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from unittest import mock

import pytest

_REPO_ROOT = subprocess.run(
    ["git", "rev-parse", "--show-toplevel"], cwd=os.path.dirname(os.path.abspath(__file__)),
    capture_output=True, text=True, check=True,
).stdout.strip()
_TARGET = os.path.join(_REPO_ROOT, "coordinator", "bin", "workday-complete-args-and-validate.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("workday_complete_args_and_validate", _TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


# ---------------------------------------------------------------------------
# parse-front-door
# ---------------------------------------------------------------------------


def test_parse_front_door_no_flags_passes_through_as_scope_summary(mod, capsys):
    rc = mod.cmd_parse_front_door("wrapped the auth refactor today")
    out = capsys.readouterr().out
    assert rc == 0
    assert out == (
        "FOR_DATE='' ONLY_MODE=0 ONLY_FLAG='' "
        "SCOPE_SUMMARY='wrapped the auth refactor today'\n"
    )


def test_parse_front_door_extracts_for_date(mod, capsys):
    rc = mod.cmd_parse_front_door("--for-date 2026-07-20 backfill note")
    out = capsys.readouterr().out
    assert rc == 0
    assert out == "FOR_DATE=2026-07-20 ONLY_MODE=0 ONLY_FLAG='' SCOPE_SUMMARY='backfill note'\n"


def test_parse_front_door_extracts_only_alongside_for_date(mod, capsys):
    rc = mod.cmd_parse_front_door("--for-date 2026-07-20 --only")
    out = capsys.readouterr().out
    assert rc == 0
    assert out == "FOR_DATE=2026-07-20 ONLY_MODE=1 ONLY_FLAG=--only SCOPE_SUMMARY=''\n"


def test_parse_front_door_only_without_for_date_fails_loud(mod, capsys):
    rc = mod.cmd_parse_front_door("--only")
    err = capsys.readouterr().err
    assert rc == 1
    assert "--only requires --for-date" in err


def test_parse_front_door_scope_summary_with_embedded_quote_is_shell_safe(mod, capsys):
    rc = mod.cmd_parse_front_door("today's summary")
    out = capsys.readouterr().out
    assert rc == 0
    assert "SCOPE_SUMMARY=" in out
    # shlex.quote must produce a value that round-trips through shlex.split
    # unchanged — the eval-injection-defence contract this CLI exists to keep.
    import shlex

    tokens = shlex.split(out)
    scope_summary = dict(t.split("=", 1) for t in tokens if t.startswith("SCOPE_SUMMARY="))
    assert scope_summary["SCOPE_SUMMARY"] == "today's summary"


def test_parse_front_door_empty_arguments(mod, capsys):
    rc = mod.cmd_parse_front_door("")
    out = capsys.readouterr().out
    assert rc == 0
    assert out == "FOR_DATE='' ONLY_MODE=0 ONLY_FLAG='' SCOPE_SUMMARY=''\n"


def test_parse_front_door_only_flag_is_argv_ready_for_both_states(mod, capsys):
    """ONLY_FLAG is the argv-ready rendering of ONLY_MODE, emitted so a caller
    can splice `$ONLY_FLAG` into a command line with no shell value-test.

    The trap it exists to close: ONLY_MODE is `0`/`1` and never empty, so
    `${ONLY_MODE:+--only}` expands to `--only` in BOTH states — silently
    turning a plain `--for-date` run into a targeted-only wrap that skips
    every other gap day. ONLY_FLAG must therefore be empty when off, and
    exactly `--only` when on.
    """
    import shlex

    def _only_flag(arguments: str) -> str:
        assert mod.cmd_parse_front_door(arguments) == 0
        tokens = shlex.split(capsys.readouterr().out)
        return dict(t.split("=", 1) for t in tokens)["ONLY_FLAG"]

    assert _only_flag("--for-date 2026-07-20") == ""
    assert _only_flag("--for-date 2026-07-20 --only") == "--only"


# ---------------------------------------------------------------------------
# check-cross-machine
# ---------------------------------------------------------------------------


def test_check_cross_machine_no_for_date_is_noop(mod, capsys):
    rc = mod.cmd_check_cross_machine("--machine other-box some prose")
    assert rc == 0
    assert capsys.readouterr().err == ""


def test_check_cross_machine_for_date_without_machine_is_noop(mod, capsys):
    rc = mod.cmd_check_cross_machine("--for-date 2026-07-20")
    assert rc == 0
    assert capsys.readouterr().err == ""


def test_check_cross_machine_matching_machine_passes(mod, capsys, monkeypatch):
    monkeypatch.setattr(mod, "_current_machine", lambda: "the-current-box")
    rc = mod.cmd_check_cross_machine("--for-date 2026-07-20 --machine the-current-box")
    assert rc == 0
    assert capsys.readouterr().err == ""


def test_check_cross_machine_mismatched_machine_fails_loud(mod, capsys, monkeypatch):
    monkeypatch.setattr(mod, "_current_machine", lambda: "the-current-box")
    rc = mod.cmd_check_cross_machine("--for-date 2026-07-20 --machine some-other-box")
    err = capsys.readouterr().err
    assert rc == 1
    assert "cross-machine targeted wrap is not supported" in err
    assert "some-other-box" in err


def test_check_cross_machine_resolution_failure_fails_loud(mod, capsys, monkeypatch):
    def _boom():
        raise RuntimeError("cannot resolve claude-klabauter")

    monkeypatch.setattr(mod, "_current_machine", _boom)
    rc = mod.cmd_check_cross_machine("--for-date 2026-07-20 --machine some-box")
    err = capsys.readouterr().err
    assert rc == 1
    assert "cannot resolve current machine" in err


# ---------------------------------------------------------------------------
# run-step1
# ---------------------------------------------------------------------------


def test_run_step1_forwards_stdout_and_exit_code(mod, capsys, monkeypatch):
    fake_proc = mock.Mock(stdout="RC_UBT='skipped' RC_VALIDATE='skipped'\n", returncode=0)
    monkeypatch.setattr(mod.subprocess, "run", mock.Mock(return_value=fake_proc))
    rc = mod.cmd_run_step1([])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == "RC_UBT='skipped' RC_VALIDATE='skipped'\n"


def test_run_step1_propagates_nonzero_exit_code(mod, capsys, monkeypatch):
    fake_proc = mock.Mock(stdout="RC_UBT='skipped' RC_VALIDATE='blocked'\n", returncode=1)
    monkeypatch.setattr(mod.subprocess, "run", mock.Mock(return_value=fake_proc))
    rc = mod.cmd_run_step1([])
    assert rc == 1


def test_run_step1_missing_script_fails_loud(mod, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_BIN_DIR", str(tmp_path))
    rc = mod.cmd_run_step1([])
    err = capsys.readouterr().err
    assert rc == 1
    assert "not found" in err


# ---------------------------------------------------------------------------
# main() dispatch
# ---------------------------------------------------------------------------


def test_main_no_args_prints_usage_and_exits_2(mod, capsys):
    rc = mod.main([])
    err = capsys.readouterr().err
    assert rc == 2
    assert "usage:" in err


def test_main_help_flag_exits_0(mod, capsys):
    rc = mod.main(["--help"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "subcommands:" in out


def test_main_unknown_subcommand_exits_2(mod, capsys):
    rc = mod.main(["bogus-subcommand"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown subcommand" in err


def test_main_parse_front_door_missing_arg_exits_2(mod, capsys):
    rc = mod.main(["parse-front-door"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "usage:" in err
