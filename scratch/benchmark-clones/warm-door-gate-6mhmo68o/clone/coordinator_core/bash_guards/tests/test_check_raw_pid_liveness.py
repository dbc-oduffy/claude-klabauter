"""Tests for coordinator_core.bash_guards.check_raw_pid_liveness.

Covers all three detection forms (``ps -p``, ``kill -0``,
``os.kill(pid, 0)``), the "pid-shaped argument nearby" narrowing condition,
segment-scoping (a match in one pipeline stage must not borrow context from
an unrelated neighboring stage), the override escape hatch, and that the
guard is NOT identity-gated (fires with or without ``agent_id`` present).

Pure Python -- no shell spawns, no git repo required.

Spec backlink: coordinator_core/bash_guards/check_raw_pid_liveness.py
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import _verdict
from coordinator_core.bash_guards import check_raw_pid_liveness as guard


def _payload(command, agent_id=None):
    p = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": "/repo",
    }
    if agent_id is not None:
        p["agent_id"] = agent_id
    return p


def _reason(out):
    assert out is not None, "expected an advisory envelope, got a bare allow"
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    return hso["additionalContext"]


@pytest.fixture(autouse=True)
def _clean_override(monkeypatch):
    monkeypatch.delenv(guard._OVERRIDE_ENV, raising=False)


class TestNonBashOrEmpty:
    def test_non_bash_tool_allows(self):
        payload = {"tool_name": "Edit", "tool_input": {"file_path": "x"}}
        assert guard.check(payload) is None

    def test_empty_command_allows(self):
        assert guard.check(_payload("")) is None

    def test_malformed_tool_input_allows(self):
        payload = {"tool_name": "Bash", "tool_input": "not-a-dict"}
        assert guard.check(payload) is None


class TestPsPForm:
    def test_ps_p_variable_arg_advises(self):
        out = guard.check(_payload('pid=$(cat claim.pid); ps -p "$pid"'))
        reason = _reason(out)
        assert "ps -p" in reason
        assert "session-liveness-cli" in reason

    def test_ps_p_literal_pid_number_advises(self):
        out = guard.check(_payload("ps -p 12345"))
        _reason(out)

    def test_ps_p_no_argument_allows(self):
        # A bare flag with nothing after it cannot itself be a liveness
        # probe of anything -- not this guard's concern (would error at the
        # shell before ever reaching the question this guard polices).
        assert guard.check(_payload("ps -p")) is None

    def test_ps_without_p_flag_allows(self):
        assert guard.check(_payload("ps aux | grep something")) is None


class TestKill0Form:
    def test_kill_0_variable_arg_advises(self):
        out = guard.check(_payload("kill -0 $pid"))
        reason = _reason(out)
        assert "kill -0" in reason

    def test_kill_0_dollar_dollar_advises(self):
        out = guard.check(_payload("kill -0 $$"))
        _reason(out)

    def test_kill_non_zero_signal_allows(self):
        assert guard.check(_payload("kill -9 $pid")) is None

    def test_plain_kill_allows(self):
        assert guard.check(_payload("kill $pid")) is None


class TestOsKillForm:
    def test_python_one_liner_advises(self):
        out = guard.check(
            _payload("python3 -c \"import os; os.kill(pid, 0)\"")
        )
        reason = _reason(out)
        assert "os.kill(pid, 0)" in reason

    def test_python_one_liner_no_spaces_advises(self):
        out = guard.check(_payload("python3 -c \"os.kill(target,0)\""))
        _reason(out)

    def test_os_kill_non_zero_signal_allows(self):
        assert guard.check(_payload("python3 -c \"os.kill(pid, 9)\"")) is None


class TestSegmentScoping:
    def test_unrelated_neighboring_segment_does_not_leak_context(self):
        # "pid" only appears in a segment that is NOT the ps -p segment;
        # the ps -p segment itself has no pid-shaped argument (a literal
        # word with no digits/var/pid-substring) -- accepted false-negative
        # per the module's documented no-cross-segment-correlation posture.
        assert guard.check(_payload("ps -p nothing_here_at_all_xyz; echo pid")) is None

    def test_matched_segment_carries_its_own_pid_hint(self):
        out = guard.check(_payload("echo hello && ps -p $mypid"))
        _reason(out)


class TestOverride:
    def test_override_env_allows(self, monkeypatch):
        monkeypatch.setenv(guard._OVERRIDE_ENV, "1")
        assert guard.check(_payload("kill -0 $pid")) is None


class TestHeredocBodyStripped:
    def test_heredoc_body_containing_ps_p_pid_allows(self):
        # A heredoc BODY is stdin DATA, not shell command text -- a probe
        # script/findings write-up whose PROSE happens to contain "ps -p
        # 1234" as data must not be mistaken for a live liveness probe.
        # 2026-07-29 incident fix, applied here per every sibling guard in
        # this package.
        cmd = "cat <<'EOF' > probe.py\n" "ps -p 1234\n" "EOF\n"
        assert guard.check(_payload(cmd)) is None

    def test_actual_command_after_heredoc_still_advises(self):
        cmd = "cat <<'EOF' > probe.py\nsome data\nEOF\nps -p $pid"
        out = guard.check(_payload(cmd))
        _reason(out)


class TestNotIdentityGated:
    def test_advises_without_agent_id(self):
        # No agent_id -> top-level EM call in every OTHER guard in this
        # package's convention; THIS guard fires regardless (see module
        # docstring negative-spec: raw-pid liveness is wrong for every
        # caller, not just subagents).
        out = guard.check(_payload("ps -p $pid"))
        _reason(out)

    def test_advises_with_agent_id(self):
        out = guard.check(_payload("ps -p $pid", agent_id="a0123456789abcdef"))
        _reason(out)


class TestPowerShellDialectRecordsSilent:
    """Row 19, `docs/reference/guard-dialect-coverage.md` -- no PowerShell
    liveness idiom is recognized at all, so a PowerShell command must never
    read as a confirmed clean; it records SILENT instead."""

    def test_powershell_command_returns_none_but_records_silent(self):
        payload = _payload("Get-Process -Id $pid")
        payload["tool_name"] = "PowerShell"
        with _verdict.collecting() as silences:
            result = guard.check(payload)
        assert result is None
        assert _verdict.was_silent("check_raw_pid_liveness", silences)

    def test_powershell_dialect_does_not_scan_command_text(self):
        # Even a command text that WOULD match the POSIX forms verbatim
        # must not be scanned once the dialect is PowerShell -- the
        # splitter/detection forms are POSIX-only and re-using them would
        # be exactly the guess the plan forbids.
        payload = _payload("ps -p $pid")
        payload["tool_name"] = "PowerShell"
        with _verdict.collecting() as silences:
            result = guard.check(payload)
        assert result is None
        assert _verdict.was_silent("check_raw_pid_liveness", silences)
