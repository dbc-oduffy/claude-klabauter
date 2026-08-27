"""Tests for coordinator_core.bash_guards.guard_powershell_via_bash.

Coverage:
  (a) the exact live incident command (module docstring) fires, advisory-
      only, naming the `PowerShell` tool as the alternative.
  (b) a single-quoted script body never fires -- bash performs no `$`
      expansion inside single quotes, so there is nothing to warn about.
  (c) `-EncodedCommand`/`-e` never fires -- already-encoded, exempt by flag
      identity regardless of quoting.
  (d) a double-quoted script body with NO `$` inside never fires -- nothing
      for bash to expand.
  (e) `pwsh`/`pwsh.exe`/`powershell`/`POWERSHELL.EXE` (case-insensitive,
      both binary spellings) all fire under the same double-quoted-`$`
      shape.
  (f) non-matches: a non-Bash tool, an empty command, and a command with
      no powershell/pwsh invocation at all all return None.
  (g) escape hatch: COORDINATOR_OVERRIDE_POWERSHELL_VIA_BASH_GUARD=1
      suppresses the guard entirely.
  (h) never denies -- every firing envelope is allow+additionalContext,
      never permissionDecisionReason/deny.

Pure Python -- no shell spawns, no filesystem writes, no real platform
dependency.

Spec backlink: coordinator_core/bash_guards/guard_powershell_via_bash.py
"""

from __future__ import annotations

from coordinator_core.bash_guards import guard_powershell_via_bash as guard


def _payload(command, tool_name="Bash"):
    return {
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": None,
    }


def _result(cmd, tool_name="Bash"):
    return guard.check(_payload(cmd, tool_name=tool_name))


def _envelope(cmd, **kwargs):
    result = _result(cmd, **kwargs)
    assert result is not None, f"expected a non-None envelope for: {cmd!r}"
    return result["hookSpecificOutput"]


# ---------------------------------------------------------------------------
# (a) The live incident command fires.
# ---------------------------------------------------------------------------

_LIVE_INCIDENT_CMD = (
    r'powershell.exe -NoProfile -Command "$p=Get-Process -Id 44448 -EA '
    r'SilentlyContinue; if($p){\"ALIVE $($p.ProcessName)\"}else{\'DEAD\'}; ..."'
)


class TestLiveIncidentFires:
    def test_fires_advisory(self):
        out = _envelope(_LIVE_INCIDENT_CMD)
        assert out["permissionDecision"] == "allow"
        assert "additionalContext" in out
        assert "permissionDecisionReason" not in out

    def test_names_the_powershell_tool_as_the_alternative(self):
        out = _envelope(_LIVE_INCIDENT_CMD)
        assert "PowerShell" in out["additionalContext"]

    def test_names_the_mechanism_not_a_refusal(self):
        """The message must say a script gets silently ALTERED, never
        frame this as a refusal or a policy violation."""
        out = _envelope(_LIVE_INCIDENT_CMD)
        ctx = out["additionalContext"]
        assert "expand" in ctx.lower()


# ---------------------------------------------------------------------------
# (b) Single-quoted body -- never fires.
# ---------------------------------------------------------------------------


class TestSingleQuotedBodyNeverFires:
    def test_pwsh_single_quoted_with_dollar_inside_is_silent(self):
        cmd = "pwsh -Command 'Write-Host $HOME; if ($true) { \"ok\" }'"
        assert _result(cmd) is None

    def test_powershell_single_quoted_is_silent(self):
        cmd = "powershell.exe -Command 'Get-Process -Id $pid'"
        assert _result(cmd) is None


# ---------------------------------------------------------------------------
# (c) -EncodedCommand/-e -- exempt regardless of quoting.
# ---------------------------------------------------------------------------


class TestEncodedCommandExempt:
    def test_bare_encoded_command_is_silent(self):
        cmd = "powershell.exe -NoProfile -EncodedCommand SQBmACgAJAB0AHIAdQBlACkA"
        assert _result(cmd) is None

    def test_encoded_command_short_flag_is_silent(self):
        cmd = 'pwsh -e "some base64 payload with a $ in it"'
        assert _result(cmd) is None


# ---------------------------------------------------------------------------
# (d) Double-quoted, no `$` -- nothing to expand, silent.
# ---------------------------------------------------------------------------


class TestDoubleQuotedNoDollarIsSilent:
    def test_no_dollar_in_body(self):
        cmd = 'powershell.exe -Command "Get-Date"'
        assert _result(cmd) is None

    def test_escaped_dollar_only_is_silent(self):
        """A literal `\\$` inside a double-quoted bash argument is NOT an
        expansion trigger -- bash treats it as an escaped literal dollar
        sign, not a variable reference."""
        cmd = r'powershell.exe -Command "echo \$5"'
        assert _result(cmd) is None


# ---------------------------------------------------------------------------
# (e) Binary spelling / case-insensitivity.
# ---------------------------------------------------------------------------


class TestBinarySpellingsAllFire:
    def test_pwsh_bare(self):
        assert _result('pwsh -Command "Write-Host $HOME"') is not None

    def test_pwsh_exe(self):
        assert _result('pwsh.exe -Command "Write-Host $HOME"') is not None

    def test_powershell_bare(self):
        assert _result('powershell -Command "Write-Host $HOME"') is not None

    def test_powershell_uppercase_exe(self):
        assert _result('POWERSHELL.EXE -Command "Write-Host $HOME"') is not None

    def test_c_short_flag_also_fires(self):
        assert _result('powershell.exe -c "Write-Host $HOME"') is not None


# ---------------------------------------------------------------------------
# (f) Non-matches.
# ---------------------------------------------------------------------------


class TestNonMatches:
    def test_non_bash_tool_is_silent(self):
        assert _result(_LIVE_INCIDENT_CMD, tool_name="PowerShell") is None

    def test_empty_command_is_silent(self):
        assert _result("") is None

    def test_unrelated_command_is_silent(self):
        assert _result('echo "$HOME is set"') is None


# ---------------------------------------------------------------------------
# (g) Escape hatch.
# ---------------------------------------------------------------------------


class TestOverrideEnvVar:
    def test_override_suppresses_the_guard(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_POWERSHELL_VIA_BASH_GUARD", "1")
        assert _result(_LIVE_INCIDENT_CMD) is None


# ---------------------------------------------------------------------------
# (h) Never denies.
# ---------------------------------------------------------------------------


class TestNeverDenies:
    def test_firing_envelope_is_never_a_deny(self):
        out = _envelope(_LIVE_INCIDENT_CMD)
        assert out.get("permissionDecision") != "deny"
