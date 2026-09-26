
from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.bash_guards import guard_offer_invoke_params_stdin as _gi
from coordinator_core.bash_guards._command_tokenizer import (
    _MAX_TOKENIZABLE_COMMAND_CHARS as _CEILING,
)
from coordinator_core.bash_guards.guard_offer_invoke_params_stdin import (
    check_offer_invoke_params_stdin,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_REPO_ROOT = Path(__file__).resolve().parents[3]

_HAZARDOUS_PAYLOAD = {
    "worktree_root": "/Users/x/X/DoE-claude",
    "paths": ["docs/plans/p.md"],
    "message": (
        "reconcile: C1's claude-klabauter half landed\n\n"
        "Recorded C10 and C13 as confirmed-absent (build, not harden).\n"
    ),
}


def _cmd_with(payload: str, *, tail: str = " --repo /r --bare") -> str:
    return (
        "PYTHONPATH=/r python3 -m coordinator_core.invoke "
        "ceremony.scoped_git_commit '%s'%s" % (payload, tail)
    )


def _rewritten(verdict) -> str:
    return verdict["hookSpecificOutput"]["updatedInput"]["command"]


def test_the_live_failure_shape_is_rewritten_not_denied():
    cmd = _cmd_with(json.dumps(_HAZARDOUS_PAYLOAD))
    verdict = check_offer_invoke_params_stdin(cmd)
    assert verdict is not None
    out = verdict["hookSpecificOutput"]
    assert out["permissionDecision"] == "allow"
    assert "--params-file - <<'CCJSON'" in out["updatedInput"]["command"]


# C4b (docs/reference/guard-dialect-coverage.md row 10) -- `_INVOKE_RE` is a


def test_powershell_call_operator_prefixed_invocation_rewritten_same_as_bash():
    payload = json.dumps(_HAZARDOUS_PAYLOAD)
    cmd = "& " + _cmd_with(payload)
    verdict = check_offer_invoke_params_stdin(cmd)
    assert verdict is not None
    out = verdict["hookSpecificOutput"]
    assert out["permissionDecision"] == "allow"
    assert "--params-file - <<'CCJSON'" in out["updatedInput"]["command"]


def test_powershell_semicolon_chained_invocation_rewritten_same_as_bash():
    payload = json.dumps(_HAZARDOUS_PAYLOAD)
    cmd = "Set-Location C:\\repo; " + _cmd_with(payload)
    verdict = check_offer_invoke_params_stdin(cmd)
    assert verdict is not None
    out = verdict["hookSpecificOutput"]
    assert out["permissionDecision"] == "allow"
    assert "--params-file - <<'CCJSON'" in out["updatedInput"]["command"]


def test_rewrite_preserves_payload_bytes_exactly():
    payload = json.dumps(_HAZARDOUS_PAYLOAD)
    rewritten = _rewritten(check_offer_invoke_params_stdin(_cmd_with(payload)))
    body = rewritten.split("<<'CCJSON'", 1)[1]
    heredoc = body.split("\n", 1)[1].rsplit("\nCCJSON", 1)[0]
    assert heredoc == payload
    assert json.loads(heredoc) == _HAZARDOUS_PAYLOAD


def test_rewrite_keeps_flags_after_the_payload_and_places_heredoc_before_a_pipe():
    payload = json.dumps(_HAZARDOUS_PAYLOAD)
    rewritten = _rewritten(
        check_offer_invoke_params_stdin(
            _cmd_with(payload, tail=" --repo /r --bare 2>&1 | tail -5")
        )
    )
    first_line = rewritten.split("\n", 1)[0]
    assert first_line.endswith("--repo /r --bare 2>&1 | tail -5")
    assert first_line.index("<<'CCJSON'") < first_line.index("| tail -5")


#: from an unstamped tree has been refused with JSON-RPC `-32005` since
#: IN-PROCESS ONLY, deliberately not through the environment, so that a
_UNSTAMPED = " --allow-unstamped-dispatch"


def test_rewritten_command_is_valid_shell_and_reaches_the_op():
    payload = json.dumps({"note": "C1's half (build, not harden)"})
    cmd = (
        "%s -m coordinator_core.invoke ping '%s'%s --bare"
        % (shlex.quote(sys.executable), payload, _UNSTAMPED)
    )
    verdict = check_offer_invoke_params_stdin(cmd)
    assert verdict is not None
    proc = subprocess.run(
        ["bash", "-c", _rewritten(verdict)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=_REPO_ROOT,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["ok"] is True


@pytest.mark.parametrize(
    "tail",
    [
        pytest.param(" --bare &&  true", id="trailing-and-and"),
        pytest.param(" --bare ; true", id="trailing-semicolon"),
        pytest.param(" --bare &", id="backgrounded"),
    ],
)
def test_rewritten_command_is_valid_shell_for_and_semicolon_and_background(tail):
    payload = json.dumps({"note": "C1's half (build, not harden)"})
    cmd = (
        "%s -m coordinator_core.invoke ping '%s'%s%s"
        % (shlex.quote(sys.executable), payload, _UNSTAMPED, tail)
    )
    verdict = check_offer_invoke_params_stdin(cmd)
    assert verdict is not None
    proc = subprocess.run(
        ["bash", "-c", _rewritten(verdict)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=_REPO_ROOT,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["ok"] is True


def test_original_command_is_the_shell_syntax_error_this_guard_exists_for():
    cmd = _cmd_with(json.dumps(_HAZARDOUS_PAYLOAD))
    proc = subprocess.run(["bash", "-n", "-c", cmd], capture_output=True, text=True, **no_console_creationflags())
    assert proc.returncode != 0
    assert "syntax error" in proc.stderr


def test_shell_safe_payload_is_left_alone():
    assert check_offer_invoke_params_stdin(
        _cmd_with('{"dry_run": true, "limit": 5}')
    ) is None


def test_oversized_payload_is_rewritten_even_without_an_apostrophe():
    payload = json.dumps({"blob": "x" * 9000})
    verdict = check_offer_invoke_params_stdin(_cmd_with(payload))
    assert verdict is not None
    assert "argv ceiling" in verdict["hookSpecificOutput"]["additionalContext"]


def test_double_quoted_payload_is_left_alone():
    assert check_offer_invoke_params_stdin(
        'python3 -m coordinator_core.invoke ping "{\\"note\\": \\"it\'s\\"}" --bare'
    ) is None


def test_multiline_command_denies_with_the_shape_named():
    cmd = _cmd_with(json.dumps(_HAZARDOUS_PAYLOAD), tail=" --repo /r \\\n  --bare")
    verdict = check_offer_invoke_params_stdin(cmd)
    assert verdict is not None
    out = verdict["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "--params-file -" in out["permissionDecisionReason"]


def test_already_using_params_file_is_untouched():
    assert check_offer_invoke_params_stdin(
        "python3 -m coordinator_core.invoke ping --params-file /tmp/p.json --bare"
    ) is None


def test_non_invoke_command_is_untouched():
    assert check_offer_invoke_params_stdin(
        "curl -d '{\"a\": \"it's fine\"}' https://example.invalid"
    ) is None


def test_non_json_object_payload_is_untouched():
    assert check_offer_invoke_params_stdin(
        "python3 -m coordinator_core.invoke ping '{not json at all}' --bare"
    ) is None


def test_override_disables_the_guard(monkeypatch):
    monkeypatch.setenv("COORDINATOR_ALLOW_INVOKE_ARGV_PARAMS", "1")
    assert check_offer_invoke_params_stdin(
        _cmd_with(json.dumps(_HAZARDOUS_PAYLOAD))
    ) is None


def test_even_apostrophe_payload_denies_because_the_apostrophes_silently_vanish():
    """The case silence would have let through, and the reason the
    cross-check's CONTRADICTED outcome denies rather than reporting
    UNAVAILABLE.

    An EVEN number of apostrophes leaves the command well-formed shell:
    `'{"m":"isn't,doesn't"}'` has four quote characters total, so bash
    concatenates the adjacent quoted and unquoted runs into ONE token,
    `{"m":"isnt,doesnt"}` -- valid JSON with both apostrophes gone. Nothing
    fails; the op just receives a different message than the caller wrote.
    That is the quiet corruption this guard exists to stop, so it must not be
    waved through. (An ODD count is the other half of the story and is
    covered by the live-failure-shape test above: there the command is not
    tokenizable at all, so the cross-check cannot run and the rewrite fires
    on the JSON parse alone.)
    """
    cmd = (
        "python3 -m coordinator_core.invoke ping "
        "'{\"m\":\"isn't,doesn't\"}' --bare"
    )
    assert shlex.split(cmd)[4] == '{"m":"isnt,doesnt"}'
    assert json.loads(shlex.split(cmd)[4]) == {"m": "isnt,doesnt"}
    verdict = check_offer_invoke_params_stdin(cmd)
    assert verdict is not None
    out = verdict["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "vanish" in out["permissionDecisionReason"]


def test_two_adjacent_quoted_tokens_that_merge_into_valid_json_are_not_rewritten():
    """Regression test for the Finding-1 counterexample: two SEPARATE
    single-quoted argv tokens (`'{"a": "unterminated --repo '` and
    `'done", "b": 2}'`), with an odd apostrophe count inside the first,
    whose `'{` / `}'` span bracketing merges them into one document that
    still parses as a JSON object -- even though neither shell token, on
    its own, is that document. `_span_is_single_shell_token` cross-checks
    the extracted span against `cmd`'s own shell tokenization (this command
    IS cleanly tokenizable -- three plain argv words, no unterminated
    quoting) and reports CONTRADICTED because the merged span matches neither
    token, so the guard denies instead of rewriting a document whose true
    boundaries it cannot determine."""
    cmd = (
        "python3 -m coordinator_core.invoke ping "
        "'{\"a\": \"unterminated --repo ' 'done\", \"b\": 2}' --bare"
    )
    verdict = check_offer_invoke_params_stdin(cmd)
    assert verdict is not None
    out = verdict["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "updatedInput" not in out


def test_heredoc_delimiter_never_appears_inside_the_body():
    payload = json.dumps({"message": "line\nCCJSON\nmore ' apostrophe"})
    rewritten = _rewritten(check_offer_invoke_params_stdin(_cmd_with(payload)))
    delim = rewritten.split("<<'", 1)[1].split("'", 1)[0]
    body = rewritten.split("\n", 1)[1].rsplit("\n" + delim, 1)[0]
    assert delim not in body.split("\n")


@pytest.mark.parametrize("empty", ["", None])
def test_empty_command_is_untouched(empty):
    assert check_offer_invoke_params_stdin(empty) is None


class TestCrossCheckOutcomesEachHaveTheirOwnVerdict:

    def test_confirmed_span_is_rewritten(self):
        cmd = _cmd_with(json.dumps({"m": "a" * (_gi._ARGV_PAYLOAD_HAZARD_BYTES + 1)}))
        assert _gi._span_is_single_shell_token(
            cmd, cmd.split("'")[1]
        ) == _gi._CROSS_CHECK_CONFIRMED
        out = check_offer_invoke_params_stdin(cmd)["hookSpecificOutput"]
        assert out["permissionDecision"] == "allow"
        assert "updatedInput" in out

    def test_contradicted_span_denies(self):
        cmd = (
            "python3 -m coordinator_core.invoke ping "
            "'{\"a\": \"unterminated --repo ' 'done\", \"b\": 2}' --bare"
        )
        assert _gi._span_is_single_shell_token(
            cmd, '{"a": "unterminated --repo \' \'done", "b": 2}'
        ) == _gi._CROSS_CHECK_CONTRADICTED
        out = check_offer_invoke_params_stdin(cmd)["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"

    def test_unavailable_cross_check_still_rewrites_the_guards_target_shape(self):
        """The DELIBERATE non-denial. An odd apostrophe count makes `cmd`
        untokenizable, which is the exact live 2026-07-29 shape this guard
        exists to repair -- denying it would deny ordinary work."""
        cmd = _cmd_with(json.dumps(_HAZARDOUS_PAYLOAD))
        assert _gi._span_is_single_shell_token(
            cmd, cmd.split("'")[1]
        ) == _gi._CROSS_CHECK_UNAVAILABLE
        out = check_offer_invoke_params_stdin(cmd)["hookSpecificOutput"]
        assert out["permissionDecision"] == "allow"
        assert "updatedInput" in out

    def test_over_ceiling_command_denies_instead_of_buying_a_rewrite(self):
        payload = json.dumps({"m": "x' y" + "A" * (_CEILING + 1)})
        cmd = _cmd_with(payload)
        assert len(cmd) > _CEILING
        assert _gi._span_is_single_shell_token(
            cmd, payload
        ) == _gi._CROSS_CHECK_TOO_LARGE
        out = check_offer_invoke_params_stdin(cmd)["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert "updatedInput" not in out
        assert "too large" in out["permissionDecisionReason"]

    def test_the_over_ceiling_deny_cannot_fire_below_the_ceiling(self):
        payload = json.dumps({"m": "x' y" + "A" * 4000})
        cmd = _cmd_with(payload)
        assert len(cmd) <= _CEILING
        out = check_offer_invoke_params_stdin(cmd)["hookSpecificOutput"]
        assert out["permissionDecision"] == "allow"
        assert "too large" not in out.get("permissionDecisionReason", "")
