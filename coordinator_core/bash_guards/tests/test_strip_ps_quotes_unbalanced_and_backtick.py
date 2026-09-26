
from __future__ import annotations

import os

from coordinator_core.bash_guards._dialect import _strip_ps_quotes


class TestUnbalancedOrTruncatedQuote:

    def test_unbalanced_double_quote_strips_leading_quote(self):
        token = '"C:/foreign/path'
        result = _strip_ps_quotes(token)
        assert result == "C:/foreign/path"
        assert not result.startswith('"')

    def test_unbalanced_single_quote_strips_leading_quote(self):
        token = "'C:/foreign/path"
        result = _strip_ps_quotes(token)
        assert result == "C:/foreign/path"
        assert not result.startswith("'")

    def test_unbalanced_quote_no_longer_defeats_isabs(self):
        token = '"C:/foreign/path'
        result = _strip_ps_quotes(token)
        assert os.path.isabs(result) or result[1:3] == ":/"


class TestBacktickEscapedEmbeddedQuote:

    def test_backtick_escaped_quote_before_real_close_still_strips_leading_quote(self):
        token = '"C:/foreign`"path/x"'
        result = _strip_ps_quotes(token)
        assert not result.startswith('"')
        assert result == 'C:/foreign"path/x'

    def test_trailing_backtick_escaped_quote_with_no_real_terminator_strips_leading_quote(self):
        """A token ending in a BACKTICK-ESCAPED quote (not a real
        terminator) with no genuine closing quote anywhere -- the naive
        last-char check would have read `token[-1]` as a matching quote
        and mis-stripped both ends; the leading quote must still come
        off."""
        token = '"C:/foreign/path`"'
        result = _strip_ps_quotes(token)
        assert not result.startswith('"')


class TestWellFormedCasesUnaffected:

    def test_balanced_double_quotes_stripped(self):
        assert _strip_ps_quotes('"pytest"') == "pytest"

    def test_balanced_single_quotes_stripped(self):
        assert _strip_ps_quotes("'pytest'") == "pytest"

    def test_unquoted_token_unchanged(self):
        assert _strip_ps_quotes("pytest") == "pytest"

    def test_balanced_double_quoted_path_stripped(self):
        assert _strip_ps_quotes('"C:/same-repo/file.txt"') == "C:/same-repo/file.txt"


class TestInteriorQuoteNotOpeningIsUntouched:

    def test_interior_quote_without_leading_quote_is_unchanged(self):
        token = "C:/weird'name/file.txt"
        assert _strip_ps_quotes(token) == token
