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


def _deny(cmd):
    return g._evaluate_tokenized(cmd).deny_kind


def test_d1_command_admitted():
    assert _deny(_D1) is None


def test_launcher_var_with_destructive_tail_still_judged():
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
def test_non_launcher_or_unresolved_var_denied(cmd):
    assert _DENY in (_deny(cmd) or "")
