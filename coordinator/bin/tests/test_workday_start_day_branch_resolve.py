"""test_workday_start_day_branch_resolve.py — regression suite for
workday-start-day-branch-resolve.py's ported Step -1 (reap-log-append) and
Step 0.45 (local-day/branch-span mismatch assertion) logic.

Covers the pure comparison core (`_span_assert`) with fake parse/format/machine
callables (no git, no coordinator_core dependency — fast + hermetic), plus the
`reap-log` subcommand's conditional-append behavior against a stubbed
reap-sessions.py invocation and a redirected HOME.

Spec backlink: coordinator/bin/workday-start-day-branch-resolve.py
"""
from __future__ import annotations

import datetime
import importlib.util
import os
import subprocess

import pytest

_REPO_ROOT = subprocess.run(
    ["git", "rev-parse", "--show-toplevel"],
    cwd=os.path.dirname(os.path.abspath(__file__)),
    capture_output=True,
    text=True,
    check=True,
).stdout.strip()
_TARGET = os.path.join(_REPO_ROOT, "coordinator", "bin", "workday-start-day-branch-resolve.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("workday_start_day_branch_resolve", _TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


# ---------------------------------------------------------------------------
# _span_assert — pure comparison core
# ---------------------------------------------------------------------------


def _fake_parse_branch_span(name):
    """Minimal fake mirroring coordinator_core.daily_branch.parse_branch_span's
    contract: work/{machine}/{YYYY-MM-DD} -> (start, start); None if unparseable."""
    if not name.startswith("work/"):
        return None
    parts = name.split("/")
    if len(parts) != 3:
        return None
    date = parts[2]
    if len(date) != 10:
        return None
    return (date, date)


def _fake_format_span_suffix(start_date, today):
    if start_date == today:
        return start_date
    end_dd = today.rsplit("-", 1)[-1]
    return f"{start_date}to{end_dd}"


def _fake_compute_machine():
    return "testmachine"


def test_span_assert_silent_pass_on_unparseable_branch(mod):
    assert mod._span_assert(
        "main", "2026-07-23", _fake_parse_branch_span, _fake_format_span_suffix, _fake_compute_machine
    ) is None


def test_span_assert_silent_pass_on_named_long_lived_branch(mod):
    assert mod._span_assert(
        "migration/foo", "2026-07-23", _fake_parse_branch_span, _fake_format_span_suffix, _fake_compute_machine
    ) is None


def test_span_assert_silent_pass_when_branch_already_covers_today(mod):
    assert mod._span_assert(
        "work/testmachine/2026-07-23", "2026-07-23", _fake_parse_branch_span, _fake_format_span_suffix, _fake_compute_machine
    ) is None


def test_span_assert_fires_on_stale_span(mod):
    msg = mod._span_assert(
        "work/testmachine/2026-07-21", "2026-07-23", _fake_parse_branch_span, _fake_format_span_suffix, _fake_compute_machine
    )
    assert msg is not None
    assert "work/testmachine/2026-07-21" in msg
    assert "2026-07-23" in msg
    assert "expected rename to `work/testmachine/2026-07-21to23`" in msg
    assert "Step 0 Check 4 did not fire" in msg


# ---------------------------------------------------------------------------
# cmd_span_assert — CLI-level smoke via subprocess (exercises real
# coordinator_core.daily_branch/daily_day/machine_resolver imports)
# ---------------------------------------------------------------------------


def test_cli_span_assert_main_branch_exits_zero_silent():
    result = subprocess.run(
        ["python3", _TARGET, "span-assert", "--branch", "main"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_cli_span_assert_stale_branch_exits_one_with_message():
    stale_date = (datetime.date.today() - datetime.timedelta(days=2)).isoformat()
    result = subprocess.run(
        ["python3", _TARGET, "span-assert", "--branch", f"work/pytestmachine/{stale_date}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "does not cover today" in result.stdout
    assert stale_date in result.stdout


def test_cli_span_assert_current_day_branch_exits_zero_silent():
    today = datetime.date.today().isoformat()
    result = subprocess.run(
        ["python3", _TARGET, "span-assert", "--branch", f"work/pytestmachine/{today}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_cli_unknown_subcommand_exits_two():
    result = subprocess.run(
        ["python3", _TARGET, "bogus-subcommand"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2


# ---------------------------------------------------------------------------
# reap-log — conditional-append behavior
# ---------------------------------------------------------------------------


def test_reap_log_appends_when_reap_sessions_prints_output(mod, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_run_reap_sessions", lambda: "reaped 3 stale sessions")
    monkeypatch.setattr(mod.Path, "home", staticmethod(lambda: tmp_path))
    rc = mod.cmd_reap_log(None)
    assert rc == 0
    log_path = tmp_path / ".claude" / "logs" / "coordinator-reap.log"
    assert log_path.is_file()
    content = log_path.read_text(encoding="utf-8")
    assert content.endswith("  reaped 3 stale sessions\n")
    # UTC timestamp shape: YYYY-MM-DDTHH:MM:SSZ
    ts = content.split("  ", 1)[0]
    datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")


def test_reap_log_no_append_when_reap_sessions_silent(mod, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_run_reap_sessions", lambda: "")
    monkeypatch.setattr(mod.Path, "home", staticmethod(lambda: tmp_path))
    rc = mod.cmd_reap_log(None)
    assert rc == 0
    log_path = tmp_path / ".claude" / "logs" / "coordinator-reap.log"
    assert not log_path.exists()


def test_reap_log_appends_multiple_lines_across_invocations(mod, monkeypatch, tmp_path):
    monkeypatch.setattr(mod.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(mod, "_run_reap_sessions", lambda: "first reap")
    mod.cmd_reap_log(None)
    monkeypatch.setattr(mod, "_run_reap_sessions", lambda: "second reap")
    mod.cmd_reap_log(None)
    log_path = tmp_path / ".claude" / "logs" / "coordinator-reap.log"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("first reap")
    assert lines[1].endswith("second reap")
