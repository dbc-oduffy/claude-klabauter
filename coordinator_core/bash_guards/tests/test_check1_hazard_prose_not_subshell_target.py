"""CHECK 1 must not read hazard-documenting prose as a subshell-resolved reset target.

Both directions are pinned deliberately. A strip rule loose enough to clear the
false positives below is loose enough to drop a real deny, so the REAL_* cases
are the load-bearing half of this file, not decoration.

Origin: DoE-claude hit this writing DR-144 -- a decision record whose subject is
guard coverage was denied for naming the hazard it documents. Reported via
cross-repo memo 2026-08-19-doe-claude-em-check1-hazard-prose-false-positive-
reproduced-and-bounded.md; see DR-144 in DoE-claude for the doctrine side.

Negative spec: markdown `code span` backticks and shell `command substitution`
backticks are the same character, so CHECK 1's subshell arm cannot separate them
by text alone. FALSE_POSITIVE `trailing_comment` proves the fix is not "strip
heredoc bodies harder" -- that case carries no heredoc at all.
"""

import json

import pytest

from coordinator_core.bash_guards import dispatch, dispatch_checks

BT = chr(96)
SUBST_OPEN = "$" + "("
HAZARD = "git reset " + "--hard"

SUBSHELL_DENY_MARKER = "subshell-resolved"


def _decision(command: str) -> tuple[str, str]:
    payload = json.dumps({
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "test-hazard-prose",
        "cwd": ".",
    })
    result = dispatch.evaluate_payload_json(payload)
    if not isinstance(result, dict):
        return "allow", ""
    out = result.get("hookSpecificOutput", result)
    return (
        out.get("permissionDecision", "allow"),
        out.get("permissionDecisionReason", "") or "",
    )


FALSE_POSITIVES = {
    "heredoc_python": (
        "python - <<'PY'\n"
        's = "a guard allowing ' + BT + HAZARD + BT + ' is worse than the bypass"\n'
        "PY"
    ),
    "heredoc_commit_message": (
        'git commit -m "' + SUBST_OPEN + "cat <<'EOF'\n"
        "doc: name " + BT + HAZARD + BT + " as the hazard it is\n"
        "EOF\n"
        ')"'
    ),
    # No heredoc anywhere. This is why the fix is not a heredoc-stripping fix.
    "trailing_comment": (
        'python -c "print(1)"  # the doc mentions ' + BT + HAZARD + BT
    ),
    "quoted_string_arg": (
        "python -c 'print(\"docs say " + BT + HAZARD + BT + " is the hazard\")'"
    ),
}

REAL_INVOCATIONS = {
    "command_substitution_parens": HAZARD + " " + SUBST_OPEN + "git rev-parse origin/main)",
    "command_substitution_backticks": HAZARD + " " + BT + "git rev-parse origin/main" + BT,
    "nested_in_compound": "cd /tmp && " + HAZARD + " " + SUBST_OPEN + "echo HEAD~5)",
}


@pytest.mark.parametrize("name", sorted(FALSE_POSITIVES))
def test_hazard_prose_is_not_denied_as_a_subshell_target(name):
    """Prose naming the hazard is documentation, not an invocation."""
    decision, reason = _decision(FALSE_POSITIVES[name])
    assert SUBSHELL_DENY_MARKER not in reason, (
        f"{name}: hazard-documenting prose denied as a subshell-resolved target.\n"
        f"reason: {reason[:300]}"
    )
    assert decision != "deny" or SUBSHELL_DENY_MARKER not in reason


@pytest.mark.parametrize("name", sorted(REAL_INVOCATIONS))
def test_real_subshell_resolved_reset_still_denied(name):
    """The load-bearing half: relaxing the scan must not drop a genuine deny."""
    decision, reason = _decision(REAL_INVOCATIONS[name])
    assert decision == "deny", f"{name}: real subshell-resolved reset was ALLOWED"
    assert SUBSHELL_DENY_MARKER in reason, (
        f"{name}: denied, but not by CHECK 1 -- a different guard caught it, so "
        f"CHECK 1's own coverage is unproven here.\nreason: {reason[:300]}"
    )


# --- The load-bearing half of the FIX itself -------------------------------
#
# The two discriminators added to close the cases above are narrow by
# construction: comments are text the shell never executes, and a backtick is
# a spawn indicator only in the languages where it means command
# substitution. These pin both narrowings so a later "simplification" cannot
# widen them back into a dropped deny.
#
# Body VISIBILITY is what the backtick narrowing controls, so the cases below
# assert it through CHECK 2's force-push deny rather than CHECK 1's own: a
# `git reset --hard` whose target orphans nothing is allowed by design, which
# would make a reset-shaped case here pass for the wrong reason.

FORCE_PUSH = "git push origin main --force"

FIX_MUST_STILL_DENY = {
    # Backtick IS command substitution in Perl/Ruby/PHP -- narrowing the
    # indicator by interpreter must leave every one of those bodies visible.
    "perl_heredoc_backtick_spawn": (
        "perl - <<'PL'\n"
        "my $out = " + BT + FORCE_PUSH + BT + ";\n"
        "PL"
    ),
    "ruby_heredoc_backtick_spawn": (
        "ruby - <<'RB'\n"
        "out = " + BT + FORCE_PUSH + BT + "\n"
        "RB"
    ),
    "php_heredoc_backtick_spawn": (
        "php - <<'PHP'\n"
        "$out = " + BT + FORCE_PUSH + BT + ";\n"
        "PHP"
    ),
    # Python bodies keep every NON-backtick indicator: the backtick arm is the
    # only thing that narrowed.
    "python_heredoc_subprocess_spawn": (
        "python - <<'PY'\n"
        "import subprocess\n"
        "subprocess.run(['git', 'push', 'origin', 'main', '--force'])\n"
        "PY"
    ),
    # (An unrecognized-but-parseable command word classifies as "prose" and
    # never reaches the indicator at all, so it is pinned at the unit level in
    # `test_python_backtick_alone_no_longer_holds_a_body_visible` instead.)
    # `#` inside a quoted span is not a comment -- the stripper must not eat
    # the rest of a real invocation.
    "hash_inside_quotes_is_not_a_comment": (
        "git commit -m 'refs #12' && " + HAZARD + " " + SUBST_OPEN + "echo HEAD~5)"
    ),
}


@pytest.mark.parametrize("name", sorted(FIX_MUST_STILL_DENY))
def test_narrowed_scans_did_not_drop_a_real_deny(name):
    """Each case is a shape the two narrowings could plausibly have dropped."""
    decision, reason = _decision(FIX_MUST_STILL_DENY[name])
    assert decision == "deny", f"{name}: real destructive invocation was ALLOWED"
    assert reason.strip(), f"{name}: denied with no reason text"


def test_python_backtick_alone_no_longer_holds_a_body_visible():
    """The narrowing itself, at the unit the fix changed: a lone backtick is a
    spawn indicator for Perl/Ruby/PHP and for an unresolved interpreter, and is
    NOT one for Python/Node, where it is a syntax error rather than a spawn."""
    body = ["s = " + BT + HAZARD + BT]
    assert not dispatch_checks._heredoc_body_has_spawn_indicator(body, "python")
    assert not dispatch_checks._heredoc_body_has_spawn_indicator(body, "node")
    for interp in ("perl", "ruby", "php"):
        assert dispatch_checks._heredoc_body_has_spawn_indicator(body, interp), interp
    assert dispatch_checks._heredoc_body_has_spawn_indicator(body, None)
    assert dispatch_checks._heredoc_body_has_spawn_indicator(body, "someexec")
    assert dispatch_checks._heredoc_body_has_spawn_indicator(
        ["subprocess.run(['git'])"], "python"
    )


def test_comment_stripping_does_not_unblock_check2_stash_exclusion():
    """`_seg_resolved_git_subcommand`'s own documented mirror-image hazard: a
    comment naming `git stash push` must not suppress CHECK 2 on a real forcing
    push. Stripping comments strengthens that walk rather than competing."""
    decision, reason = _decision(FORCE_PUSH + "  # git stash push")
    assert decision == "deny", "a real forcing push was ALLOWED behind a comment"
    assert reason.strip()
