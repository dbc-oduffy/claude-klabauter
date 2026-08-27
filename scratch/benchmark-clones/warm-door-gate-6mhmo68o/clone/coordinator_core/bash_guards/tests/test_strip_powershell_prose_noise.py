"""coordinator_core.bash_guards.tests.test_strip_powershell_prose_noise --
adversarial test set for `strip_powershell_prose_noise` (C2 of
`docs/plans/2026-08-18-the-held-guard-cohort-becomes-dialect-safe.md`),
required BEFORE any guard consumes the helper -- this chunk's own dispatch
brief: "THIS IS THE CORRELATED-RISK CHUNK. Three guards consume it, so a
wrong strip rule is wrong in all three at once, silently, in both
directions: strip too little and the spurious-deny class survives; strip
too much and a real deny is dropped."

Each class below maps directly onto the brief's own required minimum test
set:

  - `TestHasErrorFormPassesThrough` -- the `&> out.txt` form the module
    docstring already confirms `has_error=True` for; this helper only runs
    on the tokens-is-None route, so its job here is to be a no-op on plain
    (unquoted) command text -- nothing to strip, nothing lost.
  - `TestHereStringBodyStripped` -- a here-string whose body contains
    `git stash drop`.
  - `TestDoubleQuotedSpanStripped` -- a double-quoted span containing
    `worktree add`.
  - `TestHazardProseNotDenied` -- the real-world shape doe-claude hit: a
    hazard-documenting prose string naming a destructive git command
    inside a quoted block, which must NOT read as a live command once
    stripped.
  - `TestRealCommandResemblingProseStillDenies` -- the inverse: an
    actually-issued (unquoted) command that merely resembles quoted prose
    in shape must survive the strip, so a caller's pattern still matches.

Spec backlink: state/dispatch-briefs/2026-08-19-the-held-guard-cohort-becomes-dialect-safe/C2.md
"""

from __future__ import annotations

import re

from coordinator_core.bash_guards._dialect import strip_powershell_prose_noise

_STASH_DROP_RE = re.compile(r"\bstash\b.*\bdrop\b")
_WORKTREE_ADD_RE = re.compile(r"\bworktree\b.*\badd\b")


class TestHasErrorFormPassesThrough:
    """Plain, unquoted command text -- nothing here-string- or
    quote-shaped -- must be returned with no content lost, since there is
    nothing to strip. Mirrors the `&> out.txt` `has_error=True` shape the
    module docstring already measured; this helper's job on that route is
    to leave the free-text residue intact for the caller's own scan."""

    def test_redirect_form_unchanged(self):
        text = "git status &> out.txt"
        result = strip_powershell_prose_noise(text)
        assert "git" in result
        assert "status" in result
        assert "out.txt" in result


class TestHereStringBodyStripped:
    """A here-string body containing `git stash drop` must not surface
    that phrase in the residue -- it is DATA the caller wrote, not a
    command being issued."""

    def test_literal_here_string_body_stripped(self):
        text = "$doc = @'\nthis writeup mentions git stash drop as history\n'@"
        result = strip_powershell_prose_noise(text)
        assert not _STASH_DROP_RE.search(result)

    def test_expandable_here_string_body_stripped(self):
        text = '$doc = @"\nthis writeup mentions git stash drop as history\n"@'
        result = strip_powershell_prose_noise(text)
        assert not _STASH_DROP_RE.search(result)


class TestDoubleQuotedSpanStripped:
    """A double-quoted span containing `worktree add` must not surface
    that phrase in the residue."""

    def test_double_quoted_span_stripped(self):
        text = 'Write-Output "do not run git worktree add here"'
        result = strip_powershell_prose_noise(text)
        assert not _WORKTREE_ADD_RE.search(result)
        assert "Write-Output" in result


class TestHazardProseNotDenied:
    """The real-world shape doe-claude hit today: a hazard-documenting
    prose string naming a destructive git command inside a quoted block.
    Once stripped, a caller's free-text pattern for the destructive verb
    must NOT fire -- this must not deny."""

    def test_hazard_prose_in_double_quotes_does_not_match(self):
        text = (
            'Write-Output "NEVER run git stash drop on a shared branch -- '
            'it is irrecoverable"'
        )
        result = strip_powershell_prose_noise(text)
        assert not _STASH_DROP_RE.search(result)

    def test_hazard_prose_in_single_quotes_does_not_match(self):
        text = "Write-Output 'NEVER run git worktree add without a plan'"
        result = strip_powershell_prose_noise(text)
        assert not _WORKTREE_ADD_RE.search(result)


class TestRealCommandResemblingProseStillDenies:
    """The inverse: an actually-issued (unquoted) command that merely
    resembles quoted prose in shape must still be visible to a caller's
    pattern after stripping -- this helper must not strip UNQUOTED
    command text."""

    def test_unquoted_stash_drop_survives_strip(self):
        text = "git stash drop"
        result = strip_powershell_prose_noise(text)
        assert _STASH_DROP_RE.search(result)

    def test_unquoted_worktree_add_survives_strip(self):
        text = "git worktree add ../scratch"
        result = strip_powershell_prose_noise(text)
        assert _WORKTREE_ADD_RE.search(result)

    def test_quoted_prose_alongside_real_unquoted_command_still_denies(self):
        """A command line that BOTH quotes hazard prose AND issues a real
        unquoted destructive command -- the real command must still be
        visible after the quoted portion is stripped."""
        text = 'Write-Output "do not run git stash drop"; git stash drop'
        result = strip_powershell_prose_noise(text)
        assert _STASH_DROP_RE.search(result)


class TestWordSeamNotGlued:
    """A stripped span is replaced with a space, never deleted outright,
    so two words that were only adjacent because of an intervening quoted
    span never glue into an accidental match across the seam."""

    def test_stripped_span_leaves_a_separator(self):
        text = 'echo"mid"word'
        result = strip_powershell_prose_noise(text)
        assert "echoword" not in result


class TestUnterminatedQuoteConsumesToEnd:
    """An unterminated quote (no real closing quote anywhere) consumes to
    the end of the text -- fail-closed for a scanner, rather than
    guessing a boundary."""

    def test_unterminated_double_quote_strips_to_end(self):
        text = 'Write-Output "this never closes git stash drop'
        result = strip_powershell_prose_noise(text)
        assert not _STASH_DROP_RE.search(result)
        assert "Write-Output" in result
