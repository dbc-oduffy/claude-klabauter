"""A `$VAR` in command position resolves statically only to a literal coordinator-invoke launcher."""
import pytest

from coordinator_core.bash_guards import block_subagent_destructive_action as g

_DENY = "unresolved $VAR"

_D1 = (
    'cd /work/repo && (command -v coordinator-invoke >/dev/null && CI=coordinator-invoke '
    '|| CI="${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/coordinator-invoke"); '
    '"$CI" ceremony.commit_v2 --repo /work/repo \'{"paths":["a"],"skip_missing":true}\'; '
    'echo "EXIT=$?"; git rev-parse HEAD'
)


_HOISTED = (
    'B="${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin"; '
    '"$B/coordinator-invoke" updatedocs.gates \'{}\''
)
_INLINE = (
    '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/coordinator-invoke" '
    "updatedocs.gates '{}'"
)


def _reason(cmd):
    from coordinator_core.guard_messages.testing import envelope_of

    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "session_id": "sess-launcher-var",
        "cwd": None,
        "agent_type": "coordinator:executor",
        "agent_id": "a1",
    }
    env = envelope_of(g.check(payload))
    return None if env is None else str(env)


_NESTED = "bash -c 'B=/x/bin; \"$B/coordinator-invoke\" a'"


@pytest.mark.parametrize("cmd", [_HOISTED, _NESTED])
def test_hoisted_dir_var_names_inline_rewrite_and_no_escalation(cmd) -> None:
    text = _reason(cmd)
    assert text is not None
    assert '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/<cli>"' in text
    assert "Report" not in text
    assert "authoriz" not in text
    assert "Route: the engine op" not in text


def test_nested_hoisted_var_is_a_deny() -> None:
    assert "BLOCKED" in (_reason(_NESTED) or "")


def test_inline_expansion_still_admitted() -> None:
    assert _reason(_INLINE) is None


def test_non_var_deny_keeps_its_route_line() -> None:
    text = _reason("git reset --hard HEAD~1")
    assert text is not None
    assert "inline the launcher" not in text


def _deny(cmd):
    return g._evaluate_tokenized(cmd).deny_kind


def test_d1_command_admitted() -> None:
    assert _deny(_D1) is None


def test_launcher_var_with_destructive_tail_still_judged() -> None:
    assert _deny('CI=coordinator-invoke; "$CI" x; git reset --hard') is not None


@pytest.mark.parametrize(
    "cmd",
    [
        'V=git; "$V" reset --hard',
        'V=$(something); "$V" ceremony.commit_v2',
        "V=coordinator-invoke; V=$(evil); \"$V\" x",
        "V=coordinator-invoke; read V; \"$V\" x",
        "V=coordinator-invoke; export V=rm; \"$V\" x",
        "V=/tmp/evil/coordinator-invoke-x; \"$V\" x",
        "V=coordinator-invoke || V=git; \"$V\" x",
        '"$V" x; V=coordinator-invoke',
        'V=${FOO:-coordinator-invoke}; "$V" x',
    ],
)
def test_non_launcher_or_unresolved_var_denied(cmd) -> None:
    assert _DENY in (_deny(cmd) or "")
