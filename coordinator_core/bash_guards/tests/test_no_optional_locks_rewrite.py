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
