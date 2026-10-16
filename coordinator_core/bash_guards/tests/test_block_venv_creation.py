from __future__ import annotations

import pytest

from coordinator_core.bash_guards import block_venv_creation as guard

_OVERRIDE = "COORDINATOR_OVERRIDE_VENV_CREATION"


@pytest.fixture(autouse=True)
def _clean_override(monkeypatch):
    monkeypatch.delenv(_OVERRIDE, raising=False)


def _payload(command, tool="Bash"):
    return {
        "tool_name": tool,
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": "/repo",
    }


def _denied(out):
    assert out is not None, "expected a deny, got silent allow"
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "PER-REPO-VENVS-ARE-BANNED-FLEET-WIDE" in hso["permissionDecisionReason"]
    return hso["permissionDecisionReason"]


def _advisory(out):
    assert out is not None, "expected an advisory envelope, got silent allow"
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    return hso["additionalContext"]


class TestNonMatchingAllowsSilently:
    def test_non_command_tool_allows(self):
        payload = {"tool_name": "Edit", "tool_input": {"file_path": "x"}}
        assert guard.check(payload) is None

    def test_empty_command_allows(self):
        assert guard.check(_payload("")) is None

    def test_malformed_tool_input_allows(self):
        payload = {"tool_name": "Bash", "tool_input": "not-a-dict"}
        assert guard.check(payload) is None

    @pytest.mark.parametrize(
        "cmd",
        [
            "grep venv requirements.txt",
            "ls .venv",
            'cat notes.txt | grep "python -m venv"',
            "python3 script.py --venv-path .venv",
            "echo python -m venv .venv",
            "pip install --target ./vendor requests",
            "python -m pip install --target ./vendor requests",
            "git status",
        ],
    )
    def test_non_creating_commands_allow(self, cmd):
        assert guard.check(_payload(cmd)) is None

    def test_heredoc_prose_naming_venv_allows(self):
        cmd = "cat <<'EOF' > notes.md\nrun python -m venv .venv\nEOF"
        assert guard.check(_payload(cmd)) is None


class TestVenvCreationShapesDeny:
    @pytest.mark.parametrize(
        "cmd",
        [
            "python3 -m venv .venv",
            "python -m venv .venv",
            "py -m venv .venv",
            "python3.12 -m venv .venv",
            "/usr/bin/python3 -m venv .venv",
            "virtualenv .venv",
            "uv venv",
            "conda create -n foo python=3.11",
            "cd /repo && python3 -m venv .venv",
            "FOO=1 python3 -m venv .venv",
            "nice python3 -m venv .venv",
            "bash -c 'python3 -m venv .venv'",
            "sh -ic 'cd x && uv venv'",
            "echo ok; virtualenv .venv",
        ],
    )
    def test_denies(self, cmd):
        _denied(guard.check(_payload(cmd)))

    def test_message_offers_alternative(self):
        reason = _denied(guard.check(_payload("python3 -m venv .venv")))
        assert "system interpreter" in reason


class TestOverrideKey:
    def test_override_one_allows_silently(self, monkeypatch):
        monkeypatch.setenv(_OVERRIDE, "1")
        assert guard.check(_payload("python3 -m venv .venv")) is None

    @pytest.mark.parametrize("value", ["", "0", "true", "yes"])
    def test_non_one_value_still_denies(self, monkeypatch, value):
        monkeypatch.setenv(_OVERRIDE, value)
        _denied(guard.check(_payload("python3 -m venv .venv")))

    def test_message_points_at_override_doc_without_naming_key(self):
        reason = _denied(guard.check(_payload("virtualenv .venv")))
        assert "guard-override-keys" in reason
        assert _OVERRIDE not in reason


class TestPowerShell:
    @pytest.mark.parametrize(
        "cmd",
        [
            "python -m venv .venv",
            "py -3 -m venv .venv",
            "python.exe -m venv $env:TEMP\\v",
            "virtualenv .venv",
            "uv venv",
            "conda create -n foo",
            "Set-Location x; python -m venv .venv",
            "& python -m venv .venv",
        ],
    )
    def test_denies(self, cmd):
        _denied(guard.check(_payload(cmd, tool="PowerShell")))

    @pytest.mark.parametrize(
        "cmd",
        [
            "Get-ChildItem .venv",
            "Write-Output 'python -m venv .venv'",
            "pip install --target ./vendor requests",
        ],
    )
    def test_non_creating_allows(self, cmd):
        assert guard.check(_payload(cmd, tool="PowerShell")) is None

    def test_override_allows(self, monkeypatch):
        monkeypatch.setenv(_OVERRIDE, "1")
        assert guard.check(_payload("python -m venv .venv", tool="PowerShell")) is None


class TestAdvisoryShapesStayAdvisory:
    @pytest.mark.parametrize(
        "cmd",
        [
            "uv sync",
            "pipenv install",
            "pipenv sync",
            "pipenv shell",
            "poetry install",
        ],
    )
    def test_warns_without_denying(self, cmd):
        reason = _advisory(guard.check(_payload(cmd)))
        assert "PER-REPO-VENVS-ARE-BANNED-FLEET-WIDE" in reason

    def test_deny_wins_over_advisory_in_one_command(self):
        _denied(guard.check(_payload("poetry install && python -m venv .venv")))


class TestOverrideClearsAdvisory:
    def test_override_silences_advisory_shape(self, monkeypatch):
        monkeypatch.setenv(_OVERRIDE, "1")
        assert guard.check(_payload("uv sync")) is None


class TestThroughDispatchChain:
    @pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
    def test_chain_denies_venv_creation(self, tool):
        import json

        from coordinator_core.bash_guards import dispatch

        out = dispatch.evaluate_payload_json(
            json.dumps(_payload("python -m venv .venv", tool=tool))
        )
        assert out is not None
        hso = out["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"
        assert "PER-REPO-VENVS-ARE-BANNED-FLEET-WIDE" in hso["permissionDecisionReason"]
