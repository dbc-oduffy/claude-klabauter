"""Pin for IBPBE-B10 (`docs/plans/2026-09-26-inbox-blitz-part-b-engine-defects.md`).

The row asked for a repro-first check of whether the compound
`git add ... && git commit ...` bare-commit-half deny
(`_bt_compound_add_bare_commit`, PM ruling 2026-08-30) survives three
message-delivery shapes that route the commit message through stdin or a
heredoc rather than a `-m` literal:

- `git commit -F -` fed from a pipe;
- `git commit -F - <<'EOF' ... EOF`;
- `git commit -m "$(cat <<'EOF' ... EOF)"`.

`_bt_compound_add_bare_commit` is pure token inspection over the segments
`check_git_commit_safe_commit_advise` builds after `_bt_strip_heredocs`
already runs on the raw command text (see that function's own comment on
why the strip happens at that seam). All three shapes were found to
already deny when this row ran -- the fix, if any, was upstream in the
segmenter/tokenizer, not in `_bt_compound_add_bare_commit` itself. Per the
row's own instruction ("If all three already deny, close wont_do with the
test kept as the pin"), no production change was made; this test pins the
already-correct behavior so a future regression is caught.
"""

from coordinator_core.bash_guards.dispatch_checks import (
    check_git_commit_safe_commit_advise,
)


def _is_deny(result):
    if result is None:
        return False
    decision = result.get("hookSpecificOutput", {}).get("permissionDecision")
    return decision == "deny"


def test_compound_add_then_commit_dash_f_dash_piped_denies():
    cmd = 'git add a.py && echo msg | git commit -F -'
    result = check_git_commit_safe_commit_advise(cmd)
    assert _is_deny(result), result


def test_compound_add_then_commit_dash_f_dash_heredoc_fed_denies():
    cmd = "git add a.py && git commit -F - <<'EOF'\nfoo\nEOF"
    result = check_git_commit_safe_commit_advise(cmd)
    assert _is_deny(result), result


def test_compound_add_then_commit_dash_m_command_substitution_heredoc_denies():
    cmd = (
        'git add a.py && git commit -m "$(cat <<\'EOF\'\n'
        'foo\n'
        'EOF\n'
        ')"'
    )
    result = check_git_commit_safe_commit_advise(cmd)
    assert _is_deny(result), result
