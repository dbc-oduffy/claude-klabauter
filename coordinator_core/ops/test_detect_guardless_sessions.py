
from __future__ import annotations

import json

import psutil
import pytest

from coordinator_core.ops.detect_guardless_sessions import (
    ProcessObservation,
    _CommandLineUnavailable,
    _is_guarded,
    _plugin_dir_value,
    detect,
    main,
)


@pytest.fixture(autouse=True)
def _default_unresolved_plugin_dir(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions._resolved_coordinator_plugin_dir",
        lambda: None,
    )


def test_non_windows_platform_is_cannot_determine():
    result = detect(platform_system="Linux")
    assert result.cannot_determine is True
    assert result.guardless == []
    assert "Windows-only" in result.reason


def _force_unresolved_plugin_dir(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions._resolved_coordinator_plugin_dir",
        lambda: None,
    )


def test_is_guarded_true_when_plugin_dir_present(monkeypatch):
    _force_unresolved_plugin_dir(monkeypatch)
    assert _is_guarded('claude  --plugin-dir "P:\\fixture-repo\\coordinator" --dangerously-skip-permissions')


def test_is_guarded_false_when_plugin_dir_absent(monkeypatch):
    _force_unresolved_plugin_dir(monkeypatch)
    assert not _is_guarded("claude --dangerously-skip-permissions")


def test_is_guarded_false_on_empty_or_none(monkeypatch):
    _force_unresolved_plugin_dir(monkeypatch)
    assert not _is_guarded("")
    assert not _is_guarded(None)  # type: ignore[arg-type]


def test_is_guarded_false_when_plugin_dir_value_unrelated(monkeypatch):
    _force_unresolved_plugin_dir(monkeypatch)
    assert not _is_guarded('claude --plugin-dir "P:\\some-other-plugin" --dangerously-skip-permissions')


def test_is_guarded_false_when_substring_only_not_a_flag(monkeypatch):
    _force_unresolved_plugin_dir(monkeypatch)
    assert not _is_guarded("claude --note=see --plugin-dir-docs-for-details")


def test_is_guarded_uses_resolved_coordinator_plugin_dir_when_available(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions._resolved_coordinator_plugin_dir",
        lambda: "p:\\doe-root\\coordinator",
    )
    assert _is_guarded('claude --plugin-dir "P:\\doe-root\\coordinator"')
    assert not _is_guarded('claude --plugin-dir "P:\\unrelated\\coordinator"')


def test_plugin_dir_value_extracts_quoted_value():
    assert _plugin_dir_value('claude --plugin-dir "P:\\fixture\\coordinator" --x') == "P:\\fixture\\coordinator"


def test_plugin_dir_value_none_when_absent():
    assert _plugin_dir_value("claude --dangerously-skip-permissions") is None


def test_plugin_dir_value_handles_trailing_backslash_before_closing_quote():
    command_line = 'claude --plugin-dir "P:\\fixture\\coordinator\\" --x'
    assert _plugin_dir_value(command_line) == "P:\\fixture\\coordinator\\"


def test_plugin_dir_value_last_wins_on_repeated_flag():
    command_line = (
        'claude --plugin-dir "P:\\first\\coordinator" '
        '--plugin-dir "P:\\second\\coordinator"'
    )
    assert _plugin_dir_value(command_line) == "P:\\second\\coordinator"


def test_detect_windows_clean_when_all_guarded(monkeypatch):
    observed = [
        ProcessObservation(pid=1, command_line='claude --plugin-dir "P:\\fixture-repo\\coordinator"', guarded=True),
        ProcessObservation(pid=2, command_line='claude --plugin-dir "P:\\fixture-repo\\coordinator"', guarded=True),
    ]

    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions._run_process_probe",
        lambda: observed,
    )
    result = detect(platform_system="Windows")
    assert result.cannot_determine is False
    assert result.guardless == []
    assert len(result.observed) == 2


def test_detect_windows_reports_guardless(monkeypatch):
    observed = [
        ProcessObservation(pid=1, command_line='claude --plugin-dir "P:\\fixture-repo\\coordinator"', guarded=True),
        ProcessObservation(pid=2, command_line="claude --dangerously-skip-permissions", guarded=False),
    ]

    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions._run_process_probe",
        lambda: observed,
    )
    result = detect(platform_system="Windows")
    assert result.cannot_determine is False
    assert len(result.guardless) == 1
    assert result.guardless[0].pid == 2


def test_detect_windows_zero_processes_is_cannot_determine(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions._run_process_probe",
        lambda: [],
    )
    result = detect(platform_system="Windows")
    assert result.cannot_determine is True
    assert result.observed == []
    assert result.guardless == []
    assert "impossible" in result.reason


def test_detect_non_windows_zero_processes_is_cannot_determine_for_platform_reason(monkeypatch):
    result = detect(platform_system="Linux")
    assert result.cannot_determine is True
    assert "Windows-only" in result.reason


def test_detect_probe_enumeration_error_is_cannot_determine(monkeypatch):
    def fake_probe():
        raise psutil.Error("enumeration failed")

    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions._run_process_probe", fake_probe
    )
    result = detect(platform_system="Windows")
    assert result.cannot_determine is True
    assert result.guardless == []
    assert "enumeration failed" in result.reason


def test_detect_cmdline_access_denied_is_cannot_determine(monkeypatch):
    def fake_probe():
        raise _CommandLineUnavailable(pid=4242)

    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions._run_process_probe", fake_probe
    )
    result = detect(platform_system="Windows")
    assert result.cannot_determine is True
    assert result.guardless == []
    assert "4242" in result.reason


def test_detect_calls_probe_exactly_once(monkeypatch):
    call_count = {"n": 0}

    def fake_probe():
        call_count["n"] += 1
        return []

    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions._run_process_probe", fake_probe
    )
    detect(platform_system="Windows")
    assert call_count["n"] == 1


def test_run_process_probe_filters_by_name_and_joins_cmdline(monkeypatch):
    class _FakeProc:
        def __init__(self, pid, name, cmdline):
            self.info = {"pid": pid, "name": name}
            self.pid = pid
            self._cmdline = cmdline

        def cmdline(self):
            return self._cmdline

    fake_procs = [
        _FakeProc(1, "claude.exe", ["claude.exe", "--plugin-dir", "P:\\fixture-repo\\coordinator"]),
        _FakeProc(2, "notepad.exe", ["notepad.exe"]),
    ]

    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions._resolved_coordinator_plugin_dir",
        lambda: None,
    )
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: iter(fake_procs))

    from coordinator_core.ops.detect_guardless_sessions import _run_process_probe

    observed = _run_process_probe()
    assert len(observed) == 1
    assert observed[0].pid == 1
    assert observed[0].command_line == 'claude.exe --plugin-dir P:\\fixture-repo\\coordinator'


def test_run_process_probe_skips_process_that_exits_before_cmdline_read(monkeypatch):
    class _FakeProc:
        def __init__(self, pid, name, cmdline_effect):
            self.info = {"pid": pid, "name": name}
            self.pid = pid
            self._cmdline_effect = cmdline_effect

        def cmdline(self):
            if isinstance(self._cmdline_effect, Exception):
                raise self._cmdline_effect
            return self._cmdline_effect

    fake_procs = [
        _FakeProc(1, "claude.exe", psutil.NoSuchProcess(1)),
        _FakeProc(2, "claude.exe", ["claude.exe", "--plugin-dir", "P:\\fixture-repo\\coordinator"]),
    ]

    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions._resolved_coordinator_plugin_dir",
        lambda: None,
    )
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: iter(fake_procs))

    from coordinator_core.ops.detect_guardless_sessions import _run_process_probe

    observed = _run_process_probe()
    assert len(observed) == 1
    assert observed[0].pid == 2


def test_main_returns_0_on_clean(monkeypatch, capsys):
    observed = [
        ProcessObservation(pid=1, command_line='claude --plugin-dir "P:\\fixture-repo\\coordinator"', guarded=True)
    ]

    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions._run_process_probe",
        lambda: observed,
    )
    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions.platform.system", lambda: "Windows"
    )
    rc = main([])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert rc == 0
    assert payload["cannot_determine"] is False
    assert payload["guardless_count"] == 0


def test_main_returns_1_when_guardless_present(monkeypatch, capsys):
    observed = [
        ProcessObservation(pid=9, command_line="claude --dangerously-skip-permissions", guarded=False)
    ]

    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions._run_process_probe",
        lambda: observed,
    )
    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions.platform.system", lambda: "Windows"
    )
    rc = main([])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["guardless_count"] == 1


def test_main_returns_2_when_cannot_determine(monkeypatch, capsys):
    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions.platform.system", lambda: "Darwin"
    )
    rc = main([])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["cannot_determine"] is True
