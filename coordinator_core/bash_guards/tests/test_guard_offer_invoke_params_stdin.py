"""Tests for ``check_offer_invoke_params_stdin`` -- the argv-payload →
``--params-file -`` heredoc rewrite.

The subject-under-test corpus is anchored on the live 2026-07-29 failure
(a ``ceremony.scoped_git_commit`` payload whose commit message contained
``C1's`` and ``(build, not harden)``), because the property that matters is
not "a regex matched" but "the payload the op receives is byte-identical to
the one the caller wrote, and the shell can no longer see into it."
"""

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

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

# The two tests below spawn `python3 -m coordinator_core.invoke` as a real
# subprocess. That child inherits cwd but NOT pytest's rootdir sys.path
# insertion, so it can only resolve the `coordinator_core` package when cwd
# is (or is under) the repo root -- from any other cwd it dies with
# ModuleNotFoundError before it can write anything to stdout. Pinning cwd to
# the repo root derived from this file's own path makes the subprocess
# resolvable regardless of the invoking shell's cwd.
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


# ---------------------------------------------------------------------------
# C4b (docs/reference/guard-dialect-coverage.md row 10) -- `_INVOKE_RE` is a
# literal text-pattern match over the raw command string, independent of
# shell dialect. No real PowerShell parse is exercised (this guard takes a
# bare `cmd: str`, never a payload/tool_name at all) -- this proves the
# SAME regex-over-text detection reaches the identical rewrite on a
# PowerShell-spelled invocation (call-operator prefix, `;`-chained
# statement ahead of it) as on the bash-spelled one.
# ---------------------------------------------------------------------------


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
    """The rung-A claim in the guard's docstring, asserted rather than
    argued: the heredoc body is the original JSON, unchanged."""
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
    # The heredoc operator must sit inside the invoke command, ahead of the
    # pipe -- appended at end-of-line it would attach to `tail` instead.
    assert first_line.index("<<'CCJSON'") < first_line.index("| tail -5")


def test_rewritten_command_is_valid_shell_and_reaches_the_op():
    """End-to-end: run the rewrite the guard produced. Asserts the two halves
    together -- bash accepts the command AND the engine's `--params-file -`
    branch parses the heredoc body. `ping` is used because it is
    scope-`none` (no repo resolution) and has no side effects."""
    payload = json.dumps({"note": "C1's half (build, not harden)"})
    cmd = (
        "%s -m coordinator_core.invoke ping '%s' --bare"
        % (shlex.quote(sys.executable), payload)
    )
    verdict = check_offer_invoke_params_stdin(cmd)
    assert verdict is not None
    proc = subprocess.run(
        ["bash", "-c", _rewritten(verdict)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=_REPO_ROOT,
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
    """Extends `test_rewritten_command_is_valid_shell_and_reaches_the_op` to
    the three shapes Finding 2 named as untested: a trailing `&&`, a
    trailing `;`, and a backgrounded `&` invocation. `subprocess.run` waits
    on the pipe's write end regardless of backgrounding, because a forked
    child inherits the same fd -- the pipe only reaches EOF once every
    process holding it (including a backgrounded one) has exited."""
    payload = json.dumps({"note": "C1's half (build, not harden)"})
    cmd = (
        "%s -m coordinator_core.invoke ping '%s'%s"
        % (shlex.quote(sys.executable), payload, tail)
    )
    verdict = check_offer_invoke_params_stdin(cmd)
    assert verdict is not None
    proc = subprocess.run(
        ["bash", "-c", _rewritten(verdict)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=_REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["ok"] is True


def test_original_command_is_the_shell_syntax_error_this_guard_exists_for():
    """Pins the premise. If bash ever stops choking on this shape, the guard's
    justification changed and this test says so."""
    cmd = _cmd_with(json.dumps(_HAZARDOUS_PAYLOAD))
    proc = subprocess.run(["bash", "-n", "-c", cmd], capture_output=True, text=True)
    assert proc.returncode != 0
    assert "syntax error" in proc.stderr


def test_shell_safe_payload_is_left_alone():
    """The argv form stays a good transport for machine-generated params --
    rewriting those would be noise, not safety."""
    assert check_offer_invoke_params_stdin(
        _cmd_with('{"dry_run": true, "limit": 5}')
    ) is None


def test_oversized_payload_is_rewritten_even_without_an_apostrophe():
    payload = json.dumps({"blob": "x" * 9000})
    verdict = check_offer_invoke_params_stdin(_cmd_with(payload))
    assert verdict is not None
    assert "argv ceiling" in verdict["hookSpecificOutput"]["additionalContext"]


def test_double_quoted_payload_is_left_alone():
    """Pins the deliberate non-coverage argued in `_extract_inline_payload`:
    a double-quoted payload's JSON quotes are backslash-escaped, so the raw
    span never parses and the rewrite has no proof to stand on. Silence, not
    a guess."""
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
    # Premise first: the shell really does silently drop the apostrophes,
    # leaving one token that is still valid JSON.
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
    # Never a rewrite: the span merged two tokens, so any rewrite would be
    # guessing at the payload's boundaries.
    assert "updatedInput" not in out


def test_heredoc_delimiter_never_appears_inside_the_body():
    """The heredoc terminates early only if a body line equals the delimiter.
    A payload trying hardest to cause that still cannot: JSON escapes the
    newline, so 'CCJSON' is never at line-start in the transported bytes.
    This is provably unreachable, not merely well-defended: strict
    `json.loads` (required by `_extract_inline_payload` before any rewrite)
    rejects a raw newline inside a JSON string, so `payload` can never
    contain an actual `\\n` byte and the heredoc body is always exactly one
    line -- the collision loop in `check_offer_invoke_params_stdin` can
    never fire today. This test asserts the invariant it protects rather
    than the loop firing, so a future payload shape that DOES collide fails
    here."""
    payload = json.dumps({"message": "line\nCCJSON\nmore ' apostrophe"})
    rewritten = _rewritten(check_offer_invoke_params_stdin(_cmd_with(payload)))
    delim = rewritten.split("<<'", 1)[1].split("'", 1)[0]
    body = rewritten.split("\n", 1)[1].rsplit("\n" + delim, 1)[0]
    assert delim not in body.split("\n")


@pytest.mark.parametrize("empty", ["", None])
def test_empty_command_is_untouched(empty):
    assert check_offer_invoke_params_stdin(empty) is None


class TestCrossCheckOutcomesEachHaveTheirOwnVerdict:
    """The cross-check used to be `Optional[bool]`, and the caller named only
    the `is False` branch -- so the unparseable `None` reached
    `_allow_rewrite` by NOT being mentioned, and an over-ceiling command
    would have inherited that same ALLOW when the DoS ceiling was applied.
    Each of the four named outcomes is pinned here to its verdict, so a
    future edit cannot re-collapse two of them onto one branch."""

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
        """Padding past the tokenizer ceiling must not buy an ALLOW from the
        guard the padding defeats -- and, since a rewrite short-circuits the
        guard chain, must not skip the bands behind this one either."""
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
        """Gated on the shared predicate, so the new DENY is invisible to
        every command in the size band real work occupies. Same payload
        shape, one byte under."""
        payload = json.dumps({"m": "x' y" + "A" * 4000})
        cmd = _cmd_with(payload)
        assert len(cmd) <= _CEILING
        out = check_offer_invoke_params_stdin(cmd)["hookSpecificOutput"]
        assert out["permissionDecision"] == "allow"
        assert "too large" not in out.get("permissionDecisionReason", "")
