"""
coordinator_core.ops.test_detect_guardless_sessions — unit coverage for the
process-table guardless-session detector.

Spec backlink: dispatch brief "guardless-session-detector" (2026-08-10).

Mirrors the module's own three-way verdict contract: cannot-determine is
never collapsed into a clean verdict, exactly one PowerShell subprocess call
is made per `detect()` invocation, and `--plugin-dir` presence/absence in the
command line is the sole guarded/guardless discriminator.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.detect_guardless_sessions import (
    ProcessObservation,
    _is_guarded,
    _parse_probe_output,
    _plugin_dir_value,
    detect,
    main,
)

# Declared, not excused: this file spawns a real process (git/python) because
# the property under test is that binary's own behaviour, which no fixture
# stands in for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for a new file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.spawns_process]


@pytest.fixture(autouse=True)
def _default_unresolved_plugin_dir(monkeypatch):
    # Most tests below exercise the "coordinator"-suffix approximation
    # fallback deterministically, independent of this machine's real
    # operator config (which would otherwise make _is_guarded's exact-match
    # path fire unpredictably depending on the host running the suite).
    # test_is_guarded_uses_resolved_coordinator_plugin_dir_when_available
    # overrides this explicitly to exercise the exact-match path instead.
    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions._resolved_coordinator_plugin_dir",
        lambda: None,
    )


def _completed(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["powershell"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_non_windows_platform_is_cannot_determine():
    result = detect(platform_system="Linux")
    assert result.cannot_determine is True
    assert result.guardless == []
    assert "Windows-only" in result.reason


def _force_unresolved_plugin_dir(monkeypatch):
    """Force `_is_guarded`'s exact-match path off so tests exercise the
    documented "coordinator"-suffix approximation deterministically,
    independent of this machine's real operator config."""
    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions._resolved_coordinator_plugin_dir",
        lambda: None,
    )


def test_is_guarded_true_when_plugin_dir_present(monkeypatch):
    _force_unresolved_plugin_dir(monkeypatch)
    assert _is_guarded('claude  --plugin-dir "P:\\fixture-repo\\coordinator" --dangerously-skip-permissions')  # abs-path-ok: fixture-only placeholder command line, not a real host path


def test_is_guarded_false_when_plugin_dir_absent(monkeypatch):
    _force_unresolved_plugin_dir(monkeypatch)
    assert not _is_guarded("claude --dangerously-skip-permissions")


def test_is_guarded_false_on_empty_or_none(monkeypatch):
    _force_unresolved_plugin_dir(monkeypatch)
    assert not _is_guarded("")
    assert not _is_guarded(None)  # type: ignore[arg-type]


def test_is_guarded_false_when_plugin_dir_value_unrelated(monkeypatch):
    # An unrelated --plugin-dir value has zero coordinator guards and must
    # not be classified guarded merely because the flag is present.
    _force_unresolved_plugin_dir(monkeypatch)
    assert not _is_guarded('claude --plugin-dir "P:\\some-other-plugin" --dangerously-skip-permissions')  # abs-path-ok: fixture-only placeholder command line, not a real host path


def test_is_guarded_false_when_substring_only_not_a_flag(monkeypatch):
    # A command line merely containing the substring "--plugin-dir" inside
    # an unrelated token (no actual flag+value shape) must not read as
    # guarded.
    _force_unresolved_plugin_dir(monkeypatch)
    assert not _is_guarded("claude --note=see --plugin-dir-docs-for-details")


def test_is_guarded_uses_resolved_coordinator_plugin_dir_when_available(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions._resolved_coordinator_plugin_dir",
        lambda: "p:\\doe-root\\coordinator",  # abs-path-ok: fixture-only stand-in for resolve_operator_config's return value, not a real host path
    )
    assert _is_guarded('claude --plugin-dir "P:\\doe-root\\coordinator"')  # abs-path-ok: fixture-only placeholder command line, not a real host path
    # A different, unrelated directory (even one also named "coordinator")
    # does not match the resolved value, so it is NOT guarded — the exact
    # resolution path takes priority over the approximation fallback.
    assert not _is_guarded('claude --plugin-dir "P:\\unrelated\\coordinator"')  # abs-path-ok: fixture-only placeholder command line, not a real host path


def test_plugin_dir_value_extracts_quoted_value():
    assert _plugin_dir_value('claude --plugin-dir "P:\\fixture\\coordinator" --x') == "P:\\fixture\\coordinator"  # abs-path-ok: fixture-only placeholder command line, not a real host path


def test_plugin_dir_value_none_when_absent():
    assert _plugin_dir_value("claude --dangerously-skip-permissions") is None


def test_parse_probe_output_empty_string_is_zero_processes():
    assert _parse_probe_output("") == []


def test_parse_probe_output_null_is_zero_processes():
    assert _parse_probe_output("null") == []


def test_parse_probe_output_single_object_not_list():
    # PowerShell's ConvertTo-Json emits a bare object (not a list) for a
    # single matching row — this is the real-world shape, not an edge case.
    out = json.dumps({"ProcessId": 123, "CommandLine": 'claude --plugin-dir "P:\\fixture-repo\\coordinator"'})  # abs-path-ok: synthetic command-line fixture, not a real host path
    parsed = _parse_probe_output(out)
    assert parsed == [
        ProcessObservation(
            pid=123,
            command_line='claude --plugin-dir "P:\\fixture-repo\\coordinator"',  # abs-path-ok: synthetic command-line fixture, not a real host path
            guarded=True,
        )
    ]


def test_parse_probe_output_list_mixed_guarded_and_guardless():
    out = json.dumps(
        [
            {"ProcessId": 1, "CommandLine": 'claude --plugin-dir "P:\\fixture-repo\\coordinator"'},  # abs-path-ok: synthetic command-line fixture, not a real host path
            {"ProcessId": 2, "CommandLine": "claude --dangerously-skip-permissions"},
        ]
    )
    parsed = _parse_probe_output(out)
    assert parsed[0].guarded is True
    assert parsed[1].guarded is False


def test_parse_probe_output_malformed_json_returns_none():
    assert _parse_probe_output("{not json") is None


def test_parse_probe_output_missing_pid_returns_none():
    out = json.dumps([{"CommandLine": "claude"}])
    assert _parse_probe_output(out) is None


def test_detect_windows_clean_when_all_guarded(monkeypatch):
    out = json.dumps(
        [
            {"ProcessId": 1, "CommandLine": 'claude --plugin-dir "P:\\fixture-repo\\coordinator"'},  # abs-path-ok: synthetic command-line fixture, not a real host path
            {"ProcessId": 2, "CommandLine": 'claude --plugin-dir "P:\\fixture-repo\\coordinator"'},  # abs-path-ok: synthetic command-line fixture, not a real host path
        ]
    )

    def fake_run(*args, **kwargs):
        return _completed(out)

    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions._run_powershell_probe", fake_run
    )
    result = detect(platform_system="Windows")
    assert result.cannot_determine is False
    assert result.guardless == []
    assert len(result.observed) == 2


def test_detect_windows_reports_guardless(monkeypatch):
    out = json.dumps(
        [
            {"ProcessId": 1, "CommandLine": 'claude --plugin-dir "P:\\fixture-repo\\coordinator"'},  # abs-path-ok: synthetic command-line fixture, not a real host path
            {"ProcessId": 2, "CommandLine": "claude --dangerously-skip-permissions"},
        ]
    )

    def fake_run(*args, **kwargs):
        return _completed(out)

    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions._run_powershell_probe", fake_run
    )
    result = detect(platform_system="Windows")
    assert result.cannot_determine is False
    assert len(result.guardless) == 1
    assert result.guardless[0].pid == 2


def test_detect_windows_zero_processes_is_cannot_determine(monkeypatch):
    # Zero observed claude.exe processes is impossible (this detector runs
    # from inside a live claude session) and must not collapse into clean —
    # this replaces the previous (defective) assertion of clean-on-zero.
    def fake_run(*args, **kwargs):
        return _completed("")

    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions._run_powershell_probe", fake_run
    )
    result = detect(platform_system="Windows")
    assert result.cannot_determine is True
    assert result.observed == []
    assert result.guardless == []
    assert "impossible" in result.reason


def test_detect_non_windows_zero_processes_is_cannot_determine_for_platform_reason(monkeypatch):
    # On non-Windows the probe never runs at all, so the platform check
    # short-circuits before the zero-processes self-observation check.
    result = detect(platform_system="Linux")
    assert result.cannot_determine is True
    assert "Windows-only" in result.reason


def test_detect_probe_nonzero_exit_is_cannot_determine(monkeypatch):
    def fake_run(*args, **kwargs):
        return _completed("", returncode=1, stderr="access denied")

    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions._run_powershell_probe", fake_run
    )
    result = detect(platform_system="Windows")
    assert result.cannot_determine is True
    assert result.guardless == []
    assert "access denied" in result.reason


def test_detect_probe_timeout_is_cannot_determine(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="powershell", timeout=20)

    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions._run_powershell_probe", fake_run
    )
    result = detect(platform_system="Windows")
    assert result.cannot_determine is True
    assert result.guardless == []
    assert "did not complete" in result.reason


def test_detect_probe_missing_powershell_is_cannot_determine(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions._run_powershell_probe", fake_run
    )
    result = detect(platform_system="Windows")
    assert result.cannot_determine is True
    assert result.guardless == []


def test_detect_probe_undecodable_output_is_cannot_determine(monkeypatch):
    def fake_run(*args, **kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions._run_powershell_probe", fake_run
    )
    result = detect(platform_system="Windows")
    assert result.cannot_determine is True
    assert result.guardless == []
    assert "could not be decoded" in result.reason


def test_detect_malformed_probe_output_is_cannot_determine(monkeypatch):
    def fake_run(*args, **kwargs):
        return _completed("not json at all")

    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions._run_powershell_probe", fake_run
    )
    result = detect(platform_system="Windows")
    assert result.cannot_determine is True
    assert result.guardless == []


def test_detect_calls_probe_exactly_once(monkeypatch):
    call_count = {"n": 0}

    def fake_run(*args, **kwargs):
        call_count["n"] += 1
        return _completed("")

    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions._run_powershell_probe", fake_run
    )
    detect(platform_system="Windows")
    assert call_count["n"] == 1


def test_main_returns_0_on_clean(monkeypatch, capsys):
    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions.platform.system", lambda: "Linux"
    )
    # Force a deterministic Windows-path clean run instead of relying on host
    # platform. A clean verdict requires at least one observed process (zero
    # is cannot_determine, not clean — see
    # test_detect_windows_zero_processes_is_cannot_determine), so use one
    # guarded process.
    out = json.dumps([{"ProcessId": 1, "CommandLine": 'claude --plugin-dir "P:\\fixture-repo\\coordinator"'}])  # abs-path-ok: synthetic command-line fixture, not a real host path

    def fake_run(*args, **kwargs):
        return _completed(out)

    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions._run_powershell_probe", fake_run
    )
    # main() calls detect() with no explicit platform_system, so force Windows
    # via platform.system() patch above being overridden here instead.
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
    out_json = json.dumps([{"ProcessId": 9, "CommandLine": "claude --dangerously-skip-permissions"}])

    def fake_run(*args, **kwargs):
        return _completed(out_json)

    monkeypatch.setattr(
        "coordinator_core.ops.detect_guardless_sessions._run_powershell_probe", fake_run
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
