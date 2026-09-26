"""Regression tests for the sibling proofs (lang_clike, lang_hash) audited for the same
unsoundness class as the TS/JS fix: a delimiter that looks like a comment marker but sits
inside a string/literal the naive scanner didn't know how to skip.
"""
from __future__ import annotations

from coordinator_core.comment_strip import lang_clike, lang_hash


def _spans_text(spans) -> list[str]:
    return [s.text for s in spans]


# ---- lang_clike: C++ digit separators ----------------------------------------------------

def test_digit_separator_apostrophe_is_not_a_string():
    # A lone `'` here (only one separator, so an odd apostrophe count) used to flip `in_str`
    # on and never back off until some unrelated later `'`, swallowing everything between —
    # including the real `//` comment — as "inside a string".
    text = "x = 1'000; // real comment\ny = 2;\n"
    spans = lang_clike.find_comment_spans(text)
    assert _spans_text(spans) == ["// real comment"]


def test_digit_separator_does_not_hide_char_literal():
    text = "auto n = 1'000'000; char c = '\\''; // trailing\n"
    spans = lang_clike.find_comment_spans(text)
    assert _spans_text(spans) == ["// trailing"]


# ---- lang_clike: C++ raw strings ----------------------------------------------------------

def test_cpp_raw_string_hides_comment_markers():
    text = 'const char* s = R"(has // and /* inside)" ; // real\n'
    spans = lang_clike.find_comment_spans(text, cpp_raw_strings=True)
    assert _spans_text(spans) == ["// real"]


def test_cpp_raw_string_with_delimiter_tag():
    text = 'auto s = R"json({"a": "//not a comment"})json"; // real\n'
    spans = lang_clike.find_comment_spans(text, cpp_raw_strings=True)
    assert _spans_text(spans) == ["// real"]


# ---- lang_hash: shell/PowerShell/YAML `#` word-boundary ------------------------------------

def test_hash_inside_param_expansion_and_positional_count_not_a_comment():
    text = 'echo "${#arr[@]}"\nn=$#\nfoo#bar\n# real comment\n'
    spans = lang_hash.find_comment_spans(text)
    assert _spans_text(spans) == ["# real comment"]


def test_hash_mid_word_not_a_comment_yaml():
    text = "url: http://example.com#frag\n# real comment\n"
    spans = lang_hash.find_comment_spans(text)
    assert _spans_text(spans) == ["# real comment"]


def test_hash_comment_at_start_of_file_after_bom():
    # A leading UTF-8 BOM must not defeat the word-boundary rule for a `#` comment on the
    # file's very first line (regression: this hid a real, always-there comment).
    text = "﻿# header comment\nfoo()\n"
    spans = lang_hash.find_comment_spans(text)
    assert _spans_text(spans) == ["# header comment"]


# ---- lang_hash: PowerShell backslash is not an escape char --------------------------------

def test_powershell_double_quoted_string_ends_after_trailing_backslash():
    # Shell's backslash-skips-the-next-char rule is wrong for PowerShell: a Windows path
    # ending in a backslash before the closing quote (abs-path-ok: fixture literal, not a
    # real path) is a complete PS string ending right at that quote — backslash is literal.
    text = 'Write-Host "C:\\Temp\\" # real comment\n'
    spans = lang_hash.find_comment_spans(text, powershell_block=True)
    assert _spans_text(spans) == ["# real comment"]


def test_powershell_herestring_hides_hash_and_quotes():
    text = (
        '$body = @"\n'
        'Line with a # that must not be a comment and a "quote too.\n'
        '"@\n'
        "# real comment\n"
    )
    spans = lang_hash.find_comment_spans(text, powershell_block=True)
    assert _spans_text(spans) == ["# real comment"]
