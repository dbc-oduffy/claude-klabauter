"""The no-op-commit carve-out must cover every git-commit invocation shape
that stages and commits nothing -- not only `-h`/`--help`.

Bug row: state/bug-backlog/2026-08-31-the-noop-commit-carve-out-stops-at-help.yaml

`_bt_commit_is_help_invocation` (now `_bt_commit_is_noop_invocation`) carved
`-h`/`--help` out of the bare-commit predicates because a help invocation
names no pathspec and every bare-commit predicate reads that as unscoped.
`--dry-run` and `--version` share the identical property -- no staging, no
commit, no pathspec -- but were not carved out, so a compound
`add`-then-`commit --dry-run` still hit `_bt_compound_add_bare_commit`'s
unconditional deny even though nothing would be committed.

NEGATIVE SPEC pinned alongside the positive cases: `--short` and
`--porcelain` are NOT no-op flags (git still commits under either), and a
genuinely bare compound `add && commit` with none of these flags must keep
denying -- widening the carve-out must never reopen the bare-commit hole
`test_no_pathspec_less_commit.py` closed.

Also pinned: the match reads argv OPERANDS only, never the commit MESSAGE --
a message that merely mentions `--dry-run` must still deny as a bare commit.
"""

from __future__ import annotations

from coordinator_core.bash_guards.dispatch_checks import (
    _bt_commit_is_noop_invocation,
    check_git_commit_safe_commit_advise,
)


def test_dry_run_token_reads_as_noop() -> None:
    assert _bt_commit_is_noop_invocation(["commit", "--dry-run"]) is True


def test_version_token_reads_as_noop() -> None:
    assert _bt_commit_is_noop_invocation(["commit", "--version"]) is True


def test_short_and_porcelain_are_not_noop_members() -> None:
    assert _bt_commit_is_noop_invocation(["commit", "--short"]) is False
    assert _bt_commit_is_noop_invocation(["commit", "--porcelain"]) is False


def test_dry_run_as_message_value_does_not_read_as_noop() -> None:
    assert (
        _bt_commit_is_noop_invocation(["commit", "-m", "--dry-run", "--", "x.py"])
        is False
    )


def test_compound_add_then_commit_dry_run_does_not_deny() -> None:
    cmd = "git add one.py && git commit --dry-run"
    result = check_git_commit_safe_commit_advise(cmd)
    assert result is None, f"expected no verdict on a no-op commit, got {result!r}"


def test_compound_add_then_commit_version_does_not_deny() -> None:
    cmd = "git add one.py && git commit --version"
    result = check_git_commit_safe_commit_advise(cmd)
    assert result is None, f"expected no verdict on a no-op commit, got {result!r}"


def test_compound_add_then_bare_commit_still_denies() -> None:
    cmd = "git add one.py && git commit -m wip"
    result = check_git_commit_safe_commit_advise(cmd)
    assert result is not None, "bare compound commit must still fire a verdict"
