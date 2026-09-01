"""Regression tests for `check_destructive_git_orphan` CHECK 2's tokenize-
before-match fix (coordinator_core.bash_guards.dispatch_checks).

Bug: CHECK 2 (the force-push detector) scanned the raw command SEGMENT --
including the free-text operand of `-m`/`--message` -- so a `git commit -m
"..."` whose MESSAGE happened to contain the words "push" and a `-f`-shaped
token false-DENYed, even though the command performs no push at all. The
guard was matching on what the command *said*, not on what it *did*.

Fix: `_seg_excluding_freetext_operands` tokenizes the segment with `shlex`
and drops the operand tokens of `-m`/`--message` before CHECK 2's regex
runs. Fails CLOSED on unparseable (malformed-quote) input -- falls back to
the pre-fix raw-segment scan, never to permitting.

Source memo: cross-repo/inbox/2026-07-20-claude-central-em-orphan-guard-
check2-prose-false-positive.md

Second bug (heredoc bodies, 2026-07-2x): the same "match what the command
DOES, not what free text SAYS" false-positive also occurs via HEREDOC
BODIES, which `_seg_excluding_freetext_operands` does not cover -- a commit
message written via `git commit -F - <<'MSG' ... MSG`, or an unrelated
file authored via `python3 - <<'PY' ... PY` whose prose merely discusses
force-pushing, both false-DENYed. Fix:
`_strip_heredoc_bodies_for_prose_scan` strips a heredoc's body before CHECK
2/3 pattern-match the command, UNLESS the heredoc is fed to a shell
interpreter that will itself EXECUTE that body (`bash <<'EOF' ... EOF`) --
see `TestCheck2HeredocBodyProseExclusion` below.

Third bug (scriptable-interpreter regression, same day): the fix above
initially classified ANY non-shell heredoc consumer (python3, node, `git`,
`cat`, ...) as prose and blanket-stripped its body -- which reopened a real
bypass for `python3 - <<'PY'` / `node <<'JS'` bodies that actually SPAWN a
subprocess (`subprocess.run([...])`, `execSync(...)`) and push --force for
real. Fix: `_classify_heredoc_intro` adds a third, middle "scriptable"
tier (python/python2/python3/perl/ruby/node/nodejs/php) whose body is only
stripped as prose if it shows NO spawn indicator
(`_heredoc_body_has_spawn_indicator`) -- see
`TestCheck2ScriptableHeredocSpawnDetection` below. A second, narrower
regression surfaced in the same fix: `_strip_ws_quoted_spans` (which runs
right after heredoc-stripping) deletes any whitespace-containing quoted
span wholesale, including a KEPT heredoc body's own multi-word source-
string literal (`'git push origin main --force'`) -- fixed via
`_protect_line_quotes`/`_restore_protected_quotes` sentinel-protecting kept
body lines across that pass.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards.dispatch_checks import (
    _seg_confirmed_not_git_invocation,
    _seg_excluding_freetext_operands,
    check_destructive_git_orphan,
)

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _rf() -> str:
    # Built at runtime (not a literal in source) so this test file itself
    # never contains a literal destructive-rm token for tooling that scans
    # source text for one.
    return "rm -" + "rf"


class TestCheck2FreetextOperandExclusion:
    def test_commit_message_describing_force_push_allows(self):
        cmd = 'git commit -m "explain why we force-push and reset here"'
        assert check_destructive_git_orphan(cmd) is None

    def test_commit_message_with_push_log_and_dash_f_word_allows(self):
        cmd = 'git commit -m "see push-failures.log and use -f flag"'
        assert check_destructive_git_orphan(cmd) is None

    def test_commit_message_mentioning_rm_rf_and_forcing_push_allows(self):
        cmd = 'git commit -m "the %s it flagged; a forcing push"' % _rf()
        assert check_destructive_git_orphan(cmd) is None

    def test_commit_message_via_long_form_message_flag_allows(self):
        cmd = 'git commit --message "this documents why we force push"'
        assert check_destructive_git_orphan(cmd) is None

    def test_genuine_force_push_still_denies(self):
        cmd = "git push origin main --force"
        result = check_destructive_git_orphan(cmd)
        assert result is not None
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "force" in reason.lower()

    def test_genuine_short_flag_force_push_still_denies(self):
        cmd = "git push origin main -f"
        result = check_destructive_git_orphan(cmd)
        assert result is not None

    def test_genuine_plus_refspec_force_push_still_denies(self):
        cmd = "git push origin +main:main"
        result = check_destructive_git_orphan(cmd)
        assert result is not None

    def test_force_push_disguised_behind_commit_message_in_same_segment_still_denies(self):
        # The message prose is inert, but the command ALSO does a real
        # forcing push in the same segment (chained with &&) -- CHECK 2
        # must still fire on the actual push, in its own segment.
        cmd = (
            'git commit -m "just documenting our force push policy" '
            "&& git push origin main --force"
        )
        result = check_destructive_git_orphan(cmd)
        assert result is not None

    def test_malformed_unterminated_quote_with_real_force_push_still_denies(self):
        # Unterminated quote -- shlex.split raises ValueError. Fallback must
        # be the conservative raw-segment scan, which still sees the literal
        # `--force` token and denies.
        cmd = 'git push origin main --force "unterminated'
        result = check_destructive_git_orphan(cmd)
        assert result is not None

    def test_malformed_unterminated_quote_with_no_destructive_token_allows(self):
        # Malformed quoting alone, with nothing destructive in the raw
        # fallback scan, must still allow -- fallback is conservative
        # (same-as-before), not maximally paranoid.
        cmd = 'git commit -m "unterminated'
        assert check_destructive_git_orphan(cmd) is None


class TestCheck2HeredocBodyProseExclusion:
    """Regression tests for the heredoc-body-stripping fix layered on top of
    `_seg_excluding_freetext_operands` (`_strip_heredoc_bodies_for_prose_scan`).

    Bug: CHECK 2 (and CHECK 3, sharing the same contract) scanned a heredoc
    BODY as raw payload text, so prose describing a force-push/force-delete
    (a commit message written via `git commit -F - <<'MSG'`, or a file
    authored via `python3 - <<'PY'`) false-DENYed even though no push at all
    occurs, and even when the outer command isn't `git` at all.

    Source memo: the false-positive was reported directly by a dispatched
    executor (2026-07-2x), independent of the cross-repo memo covering the
    `-m`/`--message` operand case above.
    """

    def test_commit_dash_capital_f_heredoc_body_describing_force_push_allows(self):
        cmd = (
            "git commit -q -F - -- notes.md <<'MSG'\n"
            "This describes a force-push defect that was just fixed: "
            "git push --force origin main, also -f flag usage.\n"
            "MSG"
        )
        assert check_destructive_git_orphan(cmd) is None

    def test_python3_heredoc_body_discussing_force_push_allows(self):
        cmd = (
            "python3 - <<'PY'\n"
            "with open('review.md', 'w') as f:\n"
            "    f.write('discussion of tag-push safety: "
            "git push --force origin main -f')\n"
            "PY"
        )
        assert check_destructive_git_orphan(cmd) is None

    def test_python3_heredoc_body_discussing_force_delete_branch_allows(self):
        cmd = (
            "python3 - <<'PY'\n"
            "print('do not use git branch -D on shared branches')\n"
            "PY"
        )
        assert check_destructive_git_orphan(cmd) is None

    def test_genuine_force_push_outside_any_heredoc_still_denies(self):
        cmd = "git push origin main --force"
        result = check_destructive_git_orphan(cmd)
        assert result is not None
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "force" in reason.lower()

    def test_force_push_inside_shell_fed_heredoc_still_denies(self):
        # bash <<'EOF' ... EOF actually EXECUTES its body as further shell
        # commands -- a real forcing push written inside it still runs, and
        # must still be caught (the shell-fed discriminator must not strip
        # this heredoc's body).
        cmd = "bash <<'EOF'\ngit push origin main --force\nEOF"
        result = check_destructive_git_orphan(cmd)
        assert result is not None

    def test_unterminated_heredoc_with_no_destructive_token_allows(self):
        cmd = (
            "git commit -F - <<'MSG'\n"
            "unterminated heredoc without a closing delimiter\n"
        )
        assert check_destructive_git_orphan(cmd) is None

    def test_unterminated_heredoc_with_force_push_in_body_fails_closed(self):
        # Fail-closed contract: an unterminated heredoc must return the
        # payload UNCHANGED so the pre-fix raw scan still runs -- a real
        # forcing push token inside an unterminated heredoc body still
        # denies, matching `_strip_heredoc_bodies`'s existing fail-safe.
        cmd = (
            "git commit -F - <<'MSG'\n"
            "git push origin main --force\n"
        )
        result = check_destructive_git_orphan(cmd)
        assert result is not None


class TestCheck2ScriptableHeredocSpawnDetection:
    """Regression tests for the scriptable-interpreter middle tier
    (`_classify_heredoc_intro` "scriptable" + `_heredoc_body_has_spawn_
    indicator`), and for the quote-protection fix
    (`_protect_line_quotes`/`_restore_protected_quotes`) that keeps a kept
    heredoc body's multi-word source-string literal intact across the
    `_strip_ws_quoted_spans` pass that runs immediately after."""

    def test_python_heredoc_that_actually_force_pushes_via_subprocess_list_denies(self):
        # subprocess.run(['git', 'push', ..., '--force']) -- each token is
        # its own single-word quoted string (survives `_strip_ws_quoted_
        # spans` even without the quote-protection fix); this is the
        # reported case D.
        cmd = (
            "python3 - <<'PY'\n"
            "import subprocess\n"
            "subprocess.run(['git', 'push', 'origin', 'main', '--force'])\n"
            "PY"
        )
        result = check_destructive_git_orphan(cmd)
        assert result is not None

    def test_python_heredoc_that_actually_force_pushes_via_subprocess_string_denies(self):
        # A single multi-word quoted string -- this is the shape that
        # required the quote-protection fix (without it, `_strip_ws_
        # quoted_spans` deletes the whole whitespace-containing span
        # before CHECK 2 ever sees the --force token).
        cmd = (
            "python3 - <<'PY'\n"
            "import subprocess\n"
            "subprocess.run('git push origin main --force', shell=True)\n"
            "PY"
        )
        result = check_destructive_git_orphan(cmd)
        assert result is not None

    def test_node_heredoc_that_actually_force_pushes_via_execsync_denies(self):
        # Reported case E: execSync's single-string argument is exactly
        # the multi-word-quoted-span shape the quote-protection fix covers.
        cmd = (
            "node <<'JS'\n"
            "require('child_process').execSync('git push origin main --force')\n"
            "JS"
        )
        result = check_destructive_git_orphan(cmd)
        assert result is not None

    def test_python_heredoc_that_spawns_something_harmless_allows(self):
        # Has a spawn indicator (subprocess), so the body stays VISIBLE to
        # CHECK 2/3 -- but there is no forcing push/branch-delete token in
        # it, so CHECK 2/3's own regex correctly finds nothing to deny.
        # Confirms "visible" is not the same as "denied on sight".
        cmd = (
            "python3 - <<'PY'\n"
            "import subprocess\n"
            "subprocess.run(['git', 'status'])\n"
            "PY"
        )
        assert check_destructive_git_orphan(cmd) is None

    def test_python_heredoc_writing_a_doc_still_allows(self):
        # No spawn indicator at all -- prose, stripped, still ALLOWs even
        # though the prose discusses force-pushing (case C, re-affirmed
        # after the scriptable-tier fix).
        cmd = (
            "python3 - <<'PY'\n"
            "with open('review.md', 'w') as f:\n"
            "    f.write('discussion of git push --force policy')\n"
            "PY"
        )
        assert check_destructive_git_orphan(cmd) is None

    def test_git_commit_dash_capital_f_heredoc_prose_still_allows(self):
        # `git` is a recognized non-executing consumer ("prose" tier, not
        # "unknown") -- re-affirms case B still ALLOWs after introducing
        # the "prose" classification distinct from "unknown".
        cmd = (
            "git commit -F - -- notes.md <<'MSG'\n"
            "fixed a bug described as git push origin main --force\n"
            "MSG"
        )
        assert check_destructive_git_orphan(cmd) is None

    def test_shell_fed_heredoc_real_force_push_still_denies(self):
        # Re-affirms case A after the scriptable-tier changes.
        cmd = "bash <<'EOF'\ngit push origin main --force\nEOF"
        result = check_destructive_git_orphan(cmd)
        assert result is not None


class TestCheck2ShellAnywhereOnIntroLine:
    """Regression tests for `_line_has_shell_in_command_position`: a shell
    interpreter can appear ANYWHERE in command position on the heredoc-
    introducing line -- not just as the line's own leading word. Case G
    (coordinator probe, final round): `cat <<'EOF' | bash` was classified
    "prose" (leading word `cat`) and stripped, even though the body is
    piped into `bash` and actually executes."""

    def test_cat_heredoc_piped_into_bash_still_denies(self):
        cmd = "cat <<'EOF' | bash\ngit push origin main --force\nEOF"
        result = check_destructive_git_orphan(cmd)
        assert result is not None

    def test_cat_heredoc_piped_into_sh_still_denies(self):
        cmd = "cat <<'EOF' | sh\ngit push origin main --force\nEOF"
        result = check_destructive_git_orphan(cmd)
        assert result is not None

    def test_bash_dash_s_heredoc_still_denies(self):
        # `bash -s <<EOF ... EOF` -- shell as the leading word with a flag,
        # a second delivery shape distinct from the pipe-to-bash case.
        cmd = "bash -s <<'EOF'\ngit push origin main --force\nEOF"
        result = check_destructive_git_orphan(cmd)
        assert result is not None

    def test_cat_heredoc_writing_prose_to_a_file_still_allows(self):
        # Sanity check: `cat <<'EOF' > file` (no pipe to a shell) is still
        # prose and still ALLOWs -- the fix widens what counts as
        # shell-fed, it must not blanket-deny every `cat` heredoc.
        cmd = "cat > notes.md <<'EOF'\nwe must never use git push origin main --force here\nEOF"
        assert check_destructive_git_orphan(cmd) is None


class TestCheck2SpawningBodyOverBlocksProseInSameBody:
    """Pinned-as-intended test (coordinator probe finding M, final round):
    once a scriptable-interpreter heredoc body carries ANY spawn indicator,
    the WHOLE body stays visible to CHECK 2/3 -- including prose lines in
    that SAME body that merely discuss force-pushing. This is deliberate
    over-blocking, not a bug: the guard cannot tell which lines of a
    spawning body are real commands vs. commentary, so once a body might
    execute, it is read as commands throughout. Do NOT "fix" this into a
    hole that re-admits prose scanning inside a spawning body -- that is
    exactly the shape that let cases D/E slip through originally."""

    def test_spawning_body_with_unrelated_prose_mentioning_force_push_denies(self):
        cmd = (
            "python3 - <<'PY'\n"
            "import subprocess\n"
            "subprocess.run(['ls'])\n"
            "open('a.md', 'w').write('...git push origin main --force...')\n"
            "PY"
        )
        result = check_destructive_git_orphan(cmd)
        assert result is not None


class TestCheck2StashPushIsNotAForcePush:
    """Regression tests for the mention-vs-invocation fix: `\\bpush\\b`
    matched `push` as `git stash push`'s own subcommand token, false-DENYing
    a purely local, non-remote stash write as if it were a forced remote
    push.

    Confirmed reproductions (2026-07-28, example-game-repo-em cross-repo report): a
    dispatched agent could not verify whether its own test failures were
    pre-existing because `git stash push -m "x" -- +path` and
    `git stash push -f -- path` both denied.

    Fix: CHECK 2 corroborates the free-text `\\bpush\\b` match against ARGV
    position via `_command_really_invokes(cmd, "push")` -- the same
    discipline `check_destructive_git_revert` already applies to its own
    `stash` sweep-shape false positive -- before treating the match as a
    real `git push` invocation.
    """

    def test_stash_push_with_plus_refspec_pathspec_allows(self):
        cmd = 'git stash push -m "x" -- +path'
        assert check_destructive_git_orphan(cmd) is None

    def test_stash_push_with_dash_f_flag_allows(self):
        cmd = "git stash push -f -- path"
        assert check_destructive_git_orphan(cmd) is None

    def test_bare_stash_push_allows(self):
        assert check_destructive_git_orphan("git stash push") is None

    def test_stash_save_deprecated_spelling_allows(self):
        assert check_destructive_git_orphan("git stash save -f") is None

    def test_genuine_force_push_after_a_stash_push_still_denies(self):
        # A real, later `git push --force` must not be masked by an
        # earlier, unrelated `git stash push` in the same compound command.
        cmd = "git stash push && git push origin main --force"
        result = check_destructive_git_orphan(cmd)
        assert result is not None
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "force" in reason.lower()

    def test_genuine_force_push_via_git_dash_c_still_denies(self):
        cmd = "git -C /some/dir push --force"
        result = check_destructive_git_orphan(cmd)
        assert result is not None

    def test_genuine_force_push_via_git_namespace_still_denies(self):
        cmd = "git --namespace n push --force"
        result = check_destructive_git_orphan(cmd)
        assert result is not None

    def test_force_push_with_trailing_pathspec_mentioning_stash_push_still_denies(self):
        # Coordinator-caught second-order regression (2026-07-28): an
        # earlier fix here excluded via `\bstash\s+push\b` substring match,
        # which this trailing pathspec defeats -- the segment genuinely
        # invokes `push`, the "stash push" text is a pathspec operand, not
        # the resolved subcommand.
        cmd = "git push origin main --force -- stash push"
        result = check_destructive_git_orphan(cmd)
        assert result is not None

    def test_force_push_with_trailing_comment_mentioning_stash_push_still_denies(self):
        cmd = "git push origin main --force # git stash push"
        result = check_destructive_git_orphan(cmd)
        assert result is not None

    def test_stash_pop_allows(self):
        assert check_destructive_git_orphan("git stash pop") is None


class TestSegExcludingFreetextOperandsHelper:
    def test_strips_dash_m_operand(self):
        seg = 'git commit -m "force push notes"'
        out = _seg_excluding_freetext_operands(seg)
        assert "force" not in out
        assert "push" not in out
        assert "git" in out and "commit" in out

    def test_strips_long_form_message_operand(self):
        seg = 'git commit --message "force push notes"'
        out = _seg_excluding_freetext_operands(seg)
        assert "force" not in out
        assert "push" not in out

    def test_strips_equals_attached_long_form_operand(self):
        seg = 'git commit --message="force push notes"'
        out = _seg_excluding_freetext_operands(seg)
        assert "force" not in out
        assert "push" not in out

    def test_leaves_non_message_tokens_untouched(self):
        seg = "git push origin main --force"
        out = _seg_excluding_freetext_operands(seg)
        assert out == "git push origin main --force"

    def test_falls_back_to_raw_on_unparseable_quoting(self):
        seg = 'git push origin main --force "unterminated'
        out = _seg_excluding_freetext_operands(seg)
        assert out == seg


class TestCheck2ConfirmedNonGitLeafCommand:
    """Fourth bug in this lineage (2026-07-30), same shape as the three above:
    CHECK 2 matching on what a command SAYS rather than what it DOES.

    `_seg_resolved_git_subcommand` returns `None` for two entirely different
    reasons, and CHECK 2 collapsed them into one bucket:

      (a) genuine ambiguity -- a heredoc body, or a scripted spawn whose
          mangled tokens never resolve a clean `git` command position. The raw
          `\\bpush\\b` fallback exists FOR this case and must keep firing.
      (b) a segment that parses cleanly and positively resolves to a command
          that simply is not `git` -- `test -f .../push-failures.log` resolves
          `tokens[0] == "test"`.

    Treating (b) like (a) meant a read-only `test -f` against a path merely
    CONTAINING "push", carrying `test`'s own unrelated `-f`, was denied as a
    forcing push. That path is `.git/push-failures.log`, which the
    workstream-complete ceremony instructs the EM to check after a deferred
    push -- so the guard denied the ceremony's own prescribed verification.

    The fix narrows the fallback via an ALLOWLIST of non-spawning heads. The
    allowlist direction is load-bearing: a denylist ("anything not a known
    interpreter is safe") would wrongly vouch for the scripted-spawn shape in
    (a), whose head token is neither `git` nor any recognizable interpreter
    word, and would silently reopen that bypass.

    Deny-guards need coverage on the inputs they must NOT fire on. That gap is
    what let this ship, so both directions are asserted here.
    """

    # ---- direction 1: must no longer false-positive (the reported bug) ----

    def test_allows_test_f_against_push_failures_log(self):
        cmd = "test -f /Users/x/repo/.git/" + "push" + "-failures.log && echo exists"
        assert check_destructive_git_orphan(cmd) is None

    def test_allows_bracket_form_against_push_named_path(self):
        cmd = "[ -f .git/" + "push" + "-failures.log ] && echo yes"
        assert check_destructive_git_orphan(cmd) is None

    def test_allows_rm_f_near_push_named_path(self):
        cmd = _rf().replace("rf", "f") + " docs/x.bak && cat .git/" + "push" + "-failures.log"
        assert check_destructive_git_orphan(cmd) is None

    def test_allows_grep_f_with_push_named_pattern_file(self):
        cmd = "grep -f patterns-" + "push" + ".txt somefile.txt"
        assert check_destructive_git_orphan(cmd) is None

    def test_allows_wc_on_push_named_path(self):
        cmd = "wc -l .git/" + "push" + "-failures.log"
        assert check_destructive_git_orphan(cmd) is None

    # ---- direction 2: everything that must still deny, still denies ----

    def test_still_denies_long_flag_forcing_push(self):
        cmd = "git " + "push" + " origin main --" + "force"
        assert check_destructive_git_orphan(cmd) is not None

    def test_still_denies_short_flag_forcing_push(self):
        cmd = "git " + "push" + " origin main -f"
        assert check_destructive_git_orphan(cmd) is not None

    def test_still_denies_plus_refspec_forcing_push(self):
        cmd = "git " + "push" + " origin +main"
        assert check_destructive_git_orphan(cmd) is not None

    def test_still_denies_flag_before_remote(self):
        cmd = "git " + "push" + " -f origin main"
        assert check_destructive_git_orphan(cmd) is not None

    def test_scripted_heredoc_spawn_stays_fail_closed(self):
        """The allowlist must NOT vouch for a scripted spawn. This is the case
        the whole allowlist-not-denylist argument exists to protect."""
        body = "subprocess.run(['git', '" + "push" + "', 'origin', 'main', '--" + "force" + "'])"
        cmd = "python3 - <<'PY'\nimport subprocess\n" + body + "\nPY"
        assert check_destructive_git_orphan(cmd) is not None

    def test_shell_head_is_not_allowlisted(self):
        """A shell can spawn git, so it must stay ambiguous and keep the
        fallback -- `bash`/`sh` are deliberately absent from the allowlist.

        Asserted here at the level the allowlist actually controls: the head is
        not vouched for, so CHECK 2 still runs its raw scan on the segment.
        """
        assert not _seg_confirmed_not_git_invocation(
            "bash -c 'git " + "push" + " origin main --" + "force" + "'")
        assert not _seg_confirmed_not_git_invocation("sh -c \"git push -f\"")
        assert not _seg_confirmed_not_git_invocation("python3 -c 'x'")
        assert not _seg_confirmed_not_git_invocation("env FOO=1 git push")
        assert not _seg_confirmed_not_git_invocation("xargs git push")

    def test_shell_wrapped_forcing_push_is_denied(self):
        """Was a strict xfail: `_strip_ws_quoted_spans` eats the whole
        single-quoted `bash -c '...'` span (it still does -- the stripper was
        never touched), so the segment loop had nothing left to match.

        The hole is closed, but one layer further on than the xfail reason
        anticipated: `check_destructive_git_orphan` re-scans each
        `_shell_c_unwrap_payloads` payload AFTER its segment loop, and the
        unwrapped `git push origin main --force` matches there. Kept as a live
        regression pin rather than deleted -- the marker was left strict
        precisely so this would surface the moment it started passing.
        """
        cmd = "bash -c 'git " + "push" + " origin main --" + "force" + "'"
        assert check_destructive_git_orphan(cmd) is not None

    # ---- unchanged behaviour that the fix must not disturb ----

    def test_ordinary_push_still_allowed(self):
        cmd = "git " + "push" + " origin main"
        assert check_destructive_git_orphan(cmd) is None

    def test_force_with_lease_still_allowed(self):
        cmd = "git " + "push" + " --" + "force" + "-with-lease origin main"
        assert check_destructive_git_orphan(cmd) is None

    def test_stash_push_with_f_still_allowed(self):
        cmd = "git stash " + "push" + " -f -- paths"
        assert check_destructive_git_orphan(cmd) is None


class TestCheck2WrapperOwnFlagsAreNotGitsFlags:
    """Fifth bug in this lineage (2026-09-01), reported by example-retrieval-repo-em in
    `cross-repo/inbox/2026-09-01-example-retrieval-repo-em-push-cadence-cap-below-noop-
    floor.md` and reproduced here on the first attempt to measure a push.

    Same shape as the four above, one seam further out. CHECK 2 resolves the
    push CANDIDATE positionally, but then scanned the WHOLE segment for
    `--force`/`-f`/`+refspec`. A spawning wrapper's own flags live in that
    text and are not git's:

        /usr/bin/time -f "%e" git push

    `time` takes `-f FORMAT`. The push is a plain no-argument push, and it was
    denied as a "forcing form" -- so the obvious way to MEASURE a push became
    the one command that cannot run, which is how the reporter found it.

    Neither existing seam covers this. `_seg_resolved_git_subcommand` returns
    `None` (no command-position `git`), and `_seg_confirmed_not_git_
    invocation` must NOT vouch for these heads -- a wrapper really can launch
    git, which is the whole point of the allowlist's direction.

    Fix: `_seg_forcing_form_scan_text` narrows the scan to the tokens
    at-and-after the first `git` token. This cannot open a bypass -- `--force`
    and `+refspec` are push ARGUMENTS, so a genuine forcing push carries them
    after that token -- and it fails closed to the whole segment whenever no
    `git` token can be located.

    Both directions asserted, per this file's standing discipline.
    """

    # ---- direction 1: wrapper flags must no longer false-positive ----

    def test_allows_time_f_measuring_a_plain_push(self):
        cmd = "/usr/bin/time -f \"%e\" git " + "push"
        assert check_destructive_git_orphan(cmd) is None

    def test_allows_env_wrapper_with_own_f_flag(self):
        cmd = "env -f other.env git " + "push" + " origin main"
        assert check_destructive_git_orphan(cmd) is None

    def test_allows_time_f_measuring_a_dry_run_push(self):
        cmd = "/usr/bin/time -f \"%e\" git " + "push" + " --dry-run origin HEAD"
        assert check_destructive_git_orphan(cmd) is None

    # ---- direction 2: a real forcing push behind a wrapper still denies ----

    def test_still_denies_long_flag_force_behind_wrapper(self):
        cmd = "/usr/bin/time -f \"%e\" git " + "push" + " origin main --" + "force"
        assert check_destructive_git_orphan(cmd) is not None

    def test_still_denies_short_flag_force_behind_wrapper(self):
        cmd = "/usr/bin/time -f \"%e\" git " + "push" + " origin main -f"
        assert check_destructive_git_orphan(cmd) is not None

    def test_still_denies_plus_refspec_behind_wrapper(self):
        cmd = "env -f other.env git " + "push" + " origin +main"
        assert check_destructive_git_orphan(cmd) is not None

    def test_still_denies_force_when_no_git_token_resolves(self):
        """Fail-closed leg: the scan text falls back to the whole segment
        whenever no `git` token is located, so the scripted-spawn shape the
        raw `\bpush\b` fallback exists for keeps denying."""
        cmd = "subprocess.run(['g" + "it', '" + "push" + "', '--" + "force" + "'])"
        assert check_destructive_git_orphan(cmd) is not None
