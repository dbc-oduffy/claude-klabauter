"""An option-only `bash`/`sh`/`zsh` argv names no script, so the three
guards' shared `<file> (interpreter-invoked script)` shape must not fire on
it.

Bug fix, 2026-08-22: all four call sites of that shape gated on token COUNT
(`len(working) >= 2`), so `bash --version`, `bash --help`, `sh -l` and every
other option-only invocation was classified as an unexaminable script
wrapper. Any preflight or diagnostic path running a bare `bash --version`
was wrongly denied. The count check is now `_has_script_operand`, which
decides on the presence of a non-option OPERAND.

The four sites, each covered below:
    - `block_subagent_destructive_action._evaluate_wrapper_indirection`
      (raw-text pass)
    - `block_subagent_destructive_action._evaluate_tokenized`
      (tokenized pass)
    - `_sentinel_creation_guard.SentinelCreationDetector`
    - `_sentinel_removal_guard.SentinelRemovalDetector`

Negative-spec: this suite must keep pinning that a real script operand still
fires, including the two edges an option-only rule can get wrong -- a `--`
end-of-options marker followed by a `-`-prefixed OPERAND, and a lone `-`
(script read from stdin, content unexaminable). `-c` is out of scope by
construction: the `_BUNDLED_C_FLAG_RE` branch claims it before any caller
reaches this shape, and this change neither widens nor narrows it.

Pure Python -- no shell spawns, no filesystem writes.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import block_subagent_destructive_action as guard
from coordinator_core.bash_guards._sentinel_creation_guard import (
    SentinelCreationDetector,
)
from coordinator_core.bash_guards._sentinel_removal_guard import (
    VERDICT_ALLOW,
    SentinelRemovalDetector,
)


SENTINEL = ".coordinator-bash-guards-disarmed"

OPTION_ONLY = [
    "bash --version",
    "bash --help",
    "bash -l",
    "sh --version",
    "zsh --version",
    "bash --noprofile --norc",
    "bash --",
]

SCRIPT_OPERAND = [
    "bash run-tests.sh",
    "sh ./deploy.sh",
    "bash -l repro.sh",
    "bash -- --version",
    "bash -",
]

#: an argv made ENTIRELY of these was misclassified option-only and ALLOWED,
ATTACHED_VALUE_SCRIPT_OPERAND = [
    "bash --rcfile=/tmp/evil.rc --norc",
    "bash --init-file=/tmp/evil.rc",
    "sh --rcfile=/tmp/evil.rc",
]


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--version"],
        ["--help"],
        ["-l"],
        ["--noprofile", "--norc"],
        ["--"],
    ],
)
def test_option_only_argv_has_no_script_operand(args):
    assert guard._has_script_operand(args) is False


@pytest.mark.parametrize(
    "args",
    [
        ["run-tests.sh"],
        ["-l", "repro.sh"],
        ["--", "--version"],
        ["-"],
        ["-l", "-", "extra"],
        ["--rcfile", "custom.rc"],
    ],
)
def test_operand_argv_has_a_script_operand(args):
    assert guard._has_script_operand(args) is True


def test_end_of_options_marker_makes_a_dash_token_an_operand():
    assert guard._has_script_operand(["--", "--version"]) is True
    assert guard._has_script_operand(["--"]) is False


def test_lone_dash_counts_as_an_operand():
    assert guard._has_script_operand(["-"]) is True


@pytest.mark.parametrize(
    "args",
    [
        ["--rcfile=/tmp/evil.rc", "--norc"],
        ["--init-file=/tmp/evil.rc"],
        ["--rcfile=/tmp/evil.rc"],
    ],
)
def test_attached_value_long_option_is_a_script_operand(args):
    assert guard._has_script_operand(args) is True


@pytest.mark.parametrize("cmd", OPTION_ONLY)
def test_raw_text_pass_allows_option_only_invocation(cmd):
    assert guard._evaluate_wrapper_indirection(cmd) is None


@pytest.mark.parametrize("cmd", SCRIPT_OPERAND)
def test_raw_text_pass_still_classifies_a_script_operand(cmd):
    verdict = guard._evaluate_wrapper_indirection(cmd)
    assert verdict is not None
    assert "interpreter-invoked script" in verdict


@pytest.mark.parametrize("cmd", ATTACHED_VALUE_SCRIPT_OPERAND)
def test_raw_text_pass_denies_attached_value_long_option(cmd):
    verdict = guard._evaluate_wrapper_indirection(cmd)
    assert verdict is not None
    assert "interpreter-invoked script" in verdict


@pytest.mark.parametrize("cmd", OPTION_ONLY)
def test_tokenized_pass_allows_option_only_invocation(cmd):
    assert guard._evaluate_tokenized(cmd).deny_kind is None


@pytest.mark.parametrize("cmd", SCRIPT_OPERAND)
def test_tokenized_pass_still_classifies_a_script_operand(cmd):
    deny_kind = guard._evaluate_tokenized(cmd).deny_kind
    assert deny_kind is not None
    assert "interpreter-invoked script" in deny_kind


@pytest.mark.parametrize("cmd", ATTACHED_VALUE_SCRIPT_OPERAND)
def test_tokenized_pass_denies_attached_value_long_option(cmd):
    deny_kind = guard._evaluate_tokenized(cmd).deny_kind
    assert deny_kind is not None
    assert "interpreter-invoked script" in deny_kind


def test_subagent_guard_allows_bash_version_end_to_end():
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "bash --version"},
        "session_id": "sess1",
        "agent_id": "deadbeef0123",
        "agent_type": "coordinator:executor",
        "cwd": "/repo",
    }
    assert guard.check(payload) is None


def test_bash_c_treatment_is_unchanged_by_the_operand_rule():
    inert = guard._evaluate_wrapper_indirection("bash -c 'echo hello'")
    assert inert is None
    hostile = guard._evaluate_wrapper_indirection("bash -c 'git push --force'")
    assert hostile is not None
    assert "-c '<inline>'" in hostile


@pytest.mark.parametrize("cmd", OPTION_ONLY)
def test_sentinel_creation_allows_option_only_invocation(cmd):
    deny, _reason, _cls = SentinelCreationDetector(SENTINEL).evaluate(cmd)
    assert deny is False


@pytest.mark.parametrize("cmd", SCRIPT_OPERAND)
def test_sentinel_creation_still_denies_a_script_operand(cmd):
    deny, reason, _cls = SentinelCreationDetector(SENTINEL).evaluate(cmd)
    assert deny is True
    assert "interpreter-invoked script" in reason


@pytest.mark.parametrize("cmd", ATTACHED_VALUE_SCRIPT_OPERAND)
def test_sentinel_creation_denies_attached_value_long_option(cmd):
    deny, reason, _cls = SentinelCreationDetector(SENTINEL).evaluate(cmd)
    assert deny is True
    assert "interpreter-invoked script" in reason


@pytest.mark.parametrize("cmd", OPTION_ONLY)
def test_sentinel_removal_allows_option_only_invocation(cmd):
    mentioning = f"cat {SENTINEL} && {cmd}"
    verdict, _reason, _cls = SentinelRemovalDetector(SENTINEL).evaluate(mentioning)
    assert verdict == VERDICT_ALLOW


@pytest.mark.parametrize("cmd", SCRIPT_OPERAND)
def test_sentinel_removal_still_advises_on_a_script_operand(cmd):
    mentioning = f"cat {SENTINEL} && {cmd}"
    verdict, reason, _cls = SentinelRemovalDetector(SENTINEL).evaluate(mentioning)
    assert verdict != VERDICT_ALLOW
    assert "interpreter-invoked script" in reason


@pytest.mark.parametrize("cmd", ATTACHED_VALUE_SCRIPT_OPERAND)
def test_sentinel_removal_advises_on_attached_value_long_option(cmd):
    mentioning = f"cat {SENTINEL} && {cmd}"
    verdict, reason, _cls = SentinelRemovalDetector(SENTINEL).evaluate(mentioning)
    assert verdict != VERDICT_ALLOW
    assert "interpreter-invoked script" in reason
