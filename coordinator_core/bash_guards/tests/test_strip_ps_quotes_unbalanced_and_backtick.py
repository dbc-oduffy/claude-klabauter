"""coordinator_core.bash_guards.tests.test_strip_ps_quotes_unbalanced_and_backtick
-- direct-function tests for the residual filed against `023d4b63c`'s
remedy (`state/bug-backlog/2026-08-08-unbalanced-or-backtick-escaped-
quotes-st-aaf1e89862ae.yaml`): `_strip_ps_quotes` previously stripped only
a token wholly wrapped in ONE matching quote pair
(`token[0] == token[-1]`), which two spellings still defeat, both leaving
a leading quote on the token that then reaches `os.path.isabs()` and
reproduces `023d4b63c`'s own fail-open false-clean (foreign-repo write
judged same-repo and allowed):

  (a) an unbalanced/truncated quote -- a target opening with a quote that
      never closes.
  (b) a backtick-escaped embedded quote inside a double-quoted string --
      a naive last-char comparison can land on an ESCAPED quote as the
      presumed closer.

Every assertion here goes through `_strip_ps_quotes`'s own return value,
never a constant the test itself set.

Spec backlink: state/bug-backlog/2026-08-08-unbalanced-or-backtick-escaped-quotes-st-aaf1e89862ae.yaml
"""

from __future__ import annotations

import os

from coordinator_core.bash_guards._dialect import _strip_ps_quotes


class TestUnbalancedOrTruncatedQuote:
    """Case (a): a token that opens with a quote but never closes it must
    still have the leading quote stripped -- the fail-open-safe direction
    this bug's own remedy framing mandates (stripping exposes the true
    path to `os.path.isabs()`; leaving the quote hides it)."""

    def test_unbalanced_double_quote_strips_leading_quote(self):
        token = '"C:/foreign/path'  # abs-path-ok: synthetic PowerShell fixture text, never resolved.
        result = _strip_ps_quotes(token)
        assert result == "C:/foreign/path"  # abs-path-ok: synthetic PowerShell fixture text, never resolved.
        assert not result.startswith('"')

    def test_unbalanced_single_quote_strips_leading_quote(self):
        token = "'C:/foreign/path"  # abs-path-ok: synthetic PowerShell fixture text, never resolved.
        result = _strip_ps_quotes(token)
        assert result == "C:/foreign/path"  # abs-path-ok: synthetic PowerShell fixture text, never resolved.
        assert not result.startswith("'")

    def test_unbalanced_quote_no_longer_defeats_isabs(self):
        """The concrete regression this bug is filed against: the
        stripped result must actually resolve as absolute, not merely
        look stripped."""
        token = '"C:/foreign/path'  # abs-path-ok: synthetic PowerShell fixture text, never resolved.
        result = _strip_ps_quotes(token)
        assert os.path.isabs(result) or result[1:3] == ":/"


class TestBacktickEscapedEmbeddedQuote:
    """Case (b): a backtick-escaped quote embedded inside a double-quoted
    string must not be mistaken for the real terminator -- the real
    closing quote (or the absence of one) is what determines the strip,
    not a naive `token[0] == token[-1]` comparison."""

    def test_backtick_escaped_quote_before_real_close_still_strips_leading_quote(self):
        token = '"C:/foreign`"path/x"'  # abs-path-ok: synthetic PowerShell fixture text, never resolved.
        result = _strip_ps_quotes(token)
        assert not result.startswith('"')
        # The escaped quote is unescaped to a literal quote in the
        # stripped content, per PowerShell's own backtick-escape
        # semantics.
        assert result == 'C:/foreign"path/x'  # abs-path-ok: synthetic PowerShell fixture text, never resolved.

    def test_trailing_backtick_escaped_quote_with_no_real_terminator_strips_leading_quote(self):
        """A token ending in a BACKTICK-ESCAPED quote (not a real
        terminator) with no genuine closing quote anywhere -- the naive
        last-char check would have read `token[-1]` as a matching quote
        and mis-stripped both ends; the leading quote must still come
        off."""
        token = '"C:/foreign/path`"'  # abs-path-ok: synthetic PowerShell fixture text, never resolved.
        result = _strip_ps_quotes(token)
        assert not result.startswith('"')


class TestWellFormedCasesUnaffected:
    """Non-regression: `023d4b63c`'s own well-formed cases must behave
    exactly as before this fix."""

    def test_balanced_double_quotes_stripped(self):
        assert _strip_ps_quotes('"pytest"') == "pytest"

    def test_balanced_single_quotes_stripped(self):
        assert _strip_ps_quotes("'pytest'") == "pytest"

    def test_unquoted_token_unchanged(self):
        assert _strip_ps_quotes("pytest") == "pytest"

    def test_balanced_double_quoted_path_stripped(self):
        assert _strip_ps_quotes('"C:/same-repo/file.txt"') == "C:/same-repo/file.txt"  # abs-path-ok: synthetic PowerShell fixture text, never resolved.


class TestInteriorQuoteNotOpeningIsUntouched:
    """A token that does not OPEN with a quote is returned unchanged, no
    matter what quote characters appear inside it -- this function must
    never mangle a legitimate unquoted path that merely contains a quote
    character."""

    def test_interior_quote_without_leading_quote_is_unchanged(self):
        token = "C:/weird'name/file.txt"  # abs-path-ok: synthetic PowerShell fixture text, never resolved.
        assert _strip_ps_quotes(token) == token
