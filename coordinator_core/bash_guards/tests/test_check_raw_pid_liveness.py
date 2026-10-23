
from __future__ import annotations

import pytest

from coordinator_core.bash_guards import _verdict
from coordinator_core.bash_guards import check_raw_pid_liveness as guard
from coordinator_core.guard_messages.testing import envelope_of


def _enveloped(check):
    return lambda *args, **kwargs: envelope_of(check(*args, **kwargs))


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
    assert out is not None, "expected a deny envelope, got a bare allow"
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    return hso["permissionDecisionReason"]


@pytest.fixture(autouse=True)
def _clean_override(monkeypatch):
    monkeypatch.delenv(guard._OVERRIDE_ENV, raising=False)


class TestNonBashOrEmpty:
    def test_non_bash_tool_allows(self):
        payload = {"tool_name": "Edit", "tool_input": {"file_path": "x"}}
        assert _enveloped(guard.check)(payload) is None

    def test_empty_command_allows(self):
        assert _enveloped(guard.check)(_payload("")) is None

    def test_malformed_tool_input_allows(self):
        payload = {"tool_name": "Bash", "tool_input": "not-a-dict"}
        assert _enveloped(guard.check)(payload) is None


class TestPsPForm:
    def test_ps_p_variable_arg_advises(self):
        out = _enveloped(guard.check)(_payload('pid=$(cat claim.pid); ps -p "$pid"'))
        reason = _reason(out)
        assert "ps -p" in reason
        assert "session-liveness-cli" in reason

    def test_ps_p_literal_pid_number_advises(self):
        out = _enveloped(guard.check)(_payload("ps -p 12345"))
        _reason(out)

    def test_ps_p_no_argument_allows(self):
        assert _enveloped(guard.check)(_payload("ps -p")) is None

    def test_ps_without_p_flag_allows(self):
        assert _enveloped(guard.check)(_payload("ps aux | grep something")) is None


class TestKill0Form:
    def test_kill_0_variable_arg_advises(self):
        out = _enveloped(guard.check)(_payload("kill -0 $pid"))
        reason = _reason(out)
        assert "kill -0" in reason

    def test_kill_0_dollar_dollar_advises(self):
        out = _enveloped(guard.check)(_payload("kill -0 $$"))
        _reason(out)

    def test_kill_non_zero_signal_allows(self):
        assert _enveloped(guard.check)(_payload("kill -9 $pid")) is None

    def test_plain_kill_allows(self):
        assert _enveloped(guard.check)(_payload("kill $pid")) is None


class TestOsKillForm:
    def test_python_one_liner_advises(self):
        out = _enveloped(guard.check)(
            _payload("python3 -c \"import os; os.kill(pid, 0)\"")
        )
        reason = _reason(out)
        assert "os.kill(pid, 0)" in reason

    def test_python_one_liner_no_spaces_advises(self):
        out = _enveloped(guard.check)(_payload("python3 -c \"os.kill(target,0)\""))
        _reason(out)

    def test_os_kill_non_zero_signal_allows(self):
        assert _enveloped(guard.check)(_payload("python3 -c \"os.kill(pid, 9)\"")) is None


class TestSegmentScoping:
    def test_unrelated_neighboring_segment_does_not_leak_context(self):
        assert _enveloped(guard.check)(_payload("ps -p nothing_here_at_all_xyz; echo pid")) is None

    def test_matched_segment_carries_its_own_pid_hint(self):
        out = _enveloped(guard.check)(_payload("echo hello && ps -p $mypid"))
        _reason(out)


class TestOverride:
    def test_override_env_allows(self, monkeypatch):
        monkeypatch.setenv(guard._OVERRIDE_ENV, "1")
        assert _enveloped(guard.check)(_payload("kill -0 $pid")) is None


class TestHeredocBodyStripped:
    def test_heredoc_body_containing_ps_p_pid_allows(self):
        cmd = "cat <<'EOF' > probe.py\n" "ps -p 1234\n" "EOF\n"
        assert _enveloped(guard.check)(_payload(cmd)) is None

    def test_actual_command_after_heredoc_still_advises(self):
        cmd = "cat <<'EOF' > probe.py\nsome data\nEOF\nps -p $pid"
        out = _enveloped(guard.check)(_payload(cmd))
        _reason(out)


class TestNotIdentityGated:
    def test_advises_without_agent_id(self):
        out = _enveloped(guard.check)(_payload("ps -p $pid"))
        _reason(out)

    def test_advises_with_agent_id(self):
        out = _enveloped(guard.check)(_payload("ps -p $pid", agent_id="a0123456789abcdef"))
        _reason(out)


class TestPowerShellDialectRecordsSilent:

    def test_powershell_command_returns_none_but_records_silent(self):
        payload = _payload("Get-Process -Id $pid")
        payload["tool_name"] = "PowerShell"
        with _verdict.collecting() as silences:
            result = _enveloped(guard.check)(payload)
        assert result is None
        assert _verdict.was_silent("check_raw_pid_liveness", silences)

    def test_powershell_dialect_does_not_scan_command_text(self):
        payload = _payload("ps -p $pid")
        payload["tool_name"] = "PowerShell"
        with _verdict.collecting() as silences:
            result = _enveloped(guard.check)(payload)
        assert result is None
        assert _verdict.was_silent("check_raw_pid_liveness", silences)


_REPRO = (
    'coordinator sizing-assemble --slug s --intent "the guard denied this\n'
    "text: ps -p 123; kill -0 $X | y\n"
    'and more prose" --out o'
)


class TestQuotedArgumentMask:
    def test_idiom_inside_non_executed_quoted_arg_is_allowed(self):
        assert _enveloped(guard.check)(_payload(_REPRO)) is None

    @pytest.mark.parametrize(
        "command",
        [
            "ps -p 123",
            "kill -0 $PID",
            'ps -p "$PID"',
            'bash -c "kill -0 $PID"',
            'timeout 5 sh -c "ps -p 9"',
            'echo "$(kill -0 $pid)"',
            'ssh h "kill -0 $p"',
            'FOO=1 bash -c "ps -p 3"',
            'echo "unbalanced ps -p 123',
        ],
    )
    def test_executed_or_bare_idiom_still_denies(self, command):
        assert "session-liveness-cli session-live SID" in _reason(
            _enveloped(guard.check)(_payload(command))
        )

    def test_mask_is_length_preserving(self):
        assert len(guard._mask_quoted_arguments(_REPRO)) == len(_REPRO)


class TestEnvelopeOffers:
    def test_keeps_the_two_session_offers(self):
        reason = _reason(_enveloped(guard.check)(_payload("kill -0 $PID")))
        assert "`session-liveness-cli session-live SID`" in reason
        assert "`session-liveness-cli claim-holder-live CLAIM_DIR`" in reason
