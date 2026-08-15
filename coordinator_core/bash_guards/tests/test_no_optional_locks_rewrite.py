"""Tests for
``coordinator_core.bash_guards.guard_no_optional_locks.check_git_no_optional_locks``
-- the mechanical leg of the fleet-wide `.git/index.lock` contention
campaign.

Red-first: written before the guard existed, pinning the rewrite rule, its
idempotence, its NOT-rewritten exclusions, and -- the shape that would
silently break every command it touches if gotten wrong -- that the flag
lands strictly BEFORE the subcommand, never after.

Spec backlink: coordinator_core/bash_guards/guard_no_optional_locks.py
"""

from __future__ import annotations

from coordinator_core.bash_guards import guard_no_optional_locks as guard


def _reason(out):
    assert out is not None, "expected an allow-rewrite envelope, got allow-through (None)"
    return out["hookSpecificOutput"]


def _rewritten_command(out) -> str:
    hso = _reason(out)
    assert hso["permissionDecision"] == "allow", (
        "expected an allow-rewrite, got %r" % hso.get("permissionDecision")
    )
    return hso["updatedInput"]["command"]


def _check(cmd: str):
    return guard.check_git_no_optional_locks(cmd)


class TestPlainRewrite:
    def test_git_status_rewritten(self):
        out = _check("git status")
        assert _rewritten_command(out) == "git --no-optional-locks status"

    def test_git_diff_bare_rewritten(self):
        out = _check("git diff")
        assert _rewritten_command(out) == "git --no-optional-locks diff"

    def test_git_diff_name_only_rewritten(self):
        out = _check("git diff --name-only")
        assert _rewritten_command(out) == "git --no-optional-locks diff --name-only"

    def test_git_status_with_args_rewritten(self):
        out = _check("git status --short")
        assert _rewritten_command(out) == "git --no-optional-locks status --short"


class TestDashCForm:
    def test_dash_c_status_rewritten(self):
        out = _check("git -C /repo status")
        assert (
            _rewritten_command(out)
            == "git -C /repo --no-optional-locks status"
        )

    def test_dash_c_diff_rewritten(self):
        out = _check("git -C /repo diff")
        assert (
            _rewritten_command(out)
            == "git -C /repo --no-optional-locks diff"
        )


class TestFlagLandsPreSubcommand:
    """The one bug shape that would silently break every rewritten command
    fleet-wide: `git status --no-optional-locks` exits 129 (unknown
    option); only `git --no-optional-locks status` works. Explicit pin, not
    just an incidental assertion inside another test."""

    def test_flag_precedes_subcommand_not_follows(self):
        out = _check("git status")
        rewritten = _rewritten_command(out)
        tokens = rewritten.split()
        assert tokens.index("--no-optional-locks") < tokens.index("status"), (
            "the flag must land BEFORE the subcommand -- "
            "'git status --no-optional-locks' exits 129 (unknown option), "
            "got: %r" % rewritten
        )

    def test_flag_precedes_subcommand_with_dash_c(self):
        out = _check("git -C /repo status")
        rewritten = _rewritten_command(out)
        tokens = rewritten.split()
        assert tokens.index("--no-optional-locks") < tokens.index("status")


class TestIdempotence:
    def test_already_flagged_status_passes_through_untouched(self):
        assert _check("git --no-optional-locks status") is None

    def test_already_flagged_diff_passes_through_untouched(self):
        assert _check("git --no-optional-locks diff") is None

    def test_already_flagged_dash_c_status_passes_through_untouched(self):
        assert _check("git -C /repo --no-optional-locks status") is None


class TestDiffCachedNotRewritten:
    def test_diff_cached_not_rewritten(self):
        assert _check("git diff --cached") is None

    def test_diff_staged_not_rewritten(self):
        assert _check("git diff --staged") is None


class TestLsFilesNotRewritten:
    def test_ls_files_m_not_rewritten(self):
        assert _check("git ls-files -m") is None


class TestRefToRefDiffNotRewritten:
    def test_ref_colon_path_diff_not_rewritten(self):
        assert _check("git diff HEAD:file.py stash@{0}:file.py") is None

    def test_sha_range_diff_not_rewritten(self):
        assert _check("git diff abc123..HEAD") is None


class TestChainedCommands:
    def test_two_git_calls_both_rewritten(self):
        out = _check("git status && git diff")
        assert (
            _rewritten_command(out)
            == "git --no-optional-locks status && git --no-optional-locks diff"
        )

    def test_one_rewritten_one_excluded_in_chain(self):
        out = _check("git status && git diff --cached")
        assert (
            _rewritten_command(out)
            == "git --no-optional-locks status && git diff --cached"
        )


class TestQuotedTextNotRewritten:
    def test_git_like_text_inside_quotes_untouched(self):
        assert _check('echo "git status"') is None

    def test_commit_message_mentioning_git_status_untouched(self):
        assert _check('git commit -m "run git status first"') is None


class TestUnaffectedCommands:
    def test_no_git_in_command(self):
        assert _check("echo hello") is None

    def test_git_log_not_a_target_subcommand(self):
        assert _check("git log -1") is None

    def test_empty_command(self):
        assert _check("") is None


class TestSurgicalInsertionDoesNotCorruptUntouchedSegments:
    """Regression for the live incident: the prior shape re-tokenized and
    rebuilt EVERY segment via `shlex.quote`-join, which silently destroyed
    shell metacharacters (redirects, `$`-expansions) in segments the guard
    never meant to touch at all. The fix must insert the flag surgically and
    leave every other byte of the original command untouched."""

    def test_live_repro_redirect_and_dollar_expansion_survive(self):
        cmd = (
            "git -C /path status --porcelain | sed 's/^...//' "
            "> /tmp/out.txt; echo \"RC=$?\""
        )
        out = _check(cmd)
        rewritten = _rewritten_command(out)
        assert rewritten == (
            "git -C /path --no-optional-locks status --porcelain | "
            "sed 's/^...//' > /tmp/out.txt; echo \"RC=$?\""
        )
        # The two failure modes actually observed live: a redirect operator
        # quoted into a literal filename argument, and a `$` expansion
        # single-quoted into inert text.
        assert "'>'" not in rewritten
        assert "> /tmp/out.txt" in rewritten
        assert '"RC=$?"' in rewritten

    def test_redirect_target_untouched_in_rewritten_segment(self):
        out = _check("git status --porcelain > /tmp/out.txt")
        rewritten = _rewritten_command(out)
        assert rewritten == (
            "git --no-optional-locks status --porcelain > /tmp/out.txt"
        )

    def test_dollar_expansion_in_trailing_segment_untouched(self):
        out = _check('git status; echo "$HOME"')
        rewritten = _rewritten_command(out)
        assert rewritten == 'git --no-optional-locks status; echo "$HOME"'

    def test_command_substitution_in_trailing_segment_untouched(self):
        out = _check("git status; echo $(pwd)")
        rewritten = _rewritten_command(out)
        assert rewritten == "git --no-optional-locks status; echo $(pwd)"

    def test_fd_dup_redirect_after_rewritten_subcommand_untouched(self):
        out = _check("git status 2>&1")
        rewritten = _rewritten_command(out)
        assert rewritten == "git --no-optional-locks status 2>&1"


class TestMultiLineBailsRatherThanMisplace:
    """`split_unquoted_newlines` rewrites the text the decision tokenizer
    sees before this guard's own raw-offset scanner ever runs, so a raw
    offset computed against `cmd` cannot be trusted to line up once a
    newline is in play. The guard must pass the command through untouched
    rather than risk an offset landing in the wrong place."""

    def test_multiline_command_not_rewritten(self):
        assert _check("git status\necho done") is None
