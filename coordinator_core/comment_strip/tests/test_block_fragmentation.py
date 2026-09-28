"""Regression for the fleet-wide strip incident: a multi-line comment block where one
line matches a KEEP rule (tooling pattern or cross-file marker reference) must be kept
or dropped as a WHOLE contiguous block -- never fragmented into a surviving subset of
lines that reads as mid-sentence nonsense."""
from __future__ import annotations

from coordinator_core.comment_strip.engine import _plan_python, _finish_generic_lex


def test_python_block_with_marker_on_one_line_keeps_whole_block():
    # Only the middle line carries a marker token referenced elsewhere in the tree;
    # a per-line keep decision would keep line 2 alone and delete lines 1 and 3,
    # leaving a fragment that reads as nonsense.
    text = (
        "# This explains the SOME_LONG_MARKER_TOKEN in context, spread across\n"
        "# a REFERENCED_TOKEN_XYZ that another file's tripwire looks up by name\n"
        "# and this trailing line closes out the same sentence.\n"
        "x = 1\n"
    )
    res = _plan_python("mod.py", text, {"REFERENCED_TOKEN_XYZ"}, False)
    assert res.changed is False
    assert res.new_text == text


def test_python_block_without_marker_is_fully_removed():
    text = (
        "# plain comment line one\n"
        "# plain comment line two\n"
        "# plain comment line three\n"
        "x = 1\n"
    )
    res = _plan_python("mod.py", text, set(), False)
    assert res.changed is True
    assert "plain comment" not in res.new_text
    assert res.new_text.strip() == "x = 1"


def test_clike_block_with_marker_on_one_line_keeps_whole_block():
    text = (
        "// first line of prose about SOME_TOKEN\n"
        "// REFERENCED_TOKEN_ABC lives here, mid-sentence\n"
        "// and this line finishes the sentence\n"
        "int x = 1;\n"
    )

    class _Sp:
        def __init__(self, start, end, text):
            self.start, self.end, self.text = start, end, text

    spans = []
    for line_start, line in [
        (0, "// first line of prose about SOME_TOKEN"),
        (41, "// REFERENCED_TOKEN_ABC lives here, mid-sentence"),
        (91, "// and this line finishes the sentence"),
    ]:
        spans.append(_Sp(text.index(line), text.index(line) + len(line), line))

    res = _finish_generic_lex(
        "mod.c", "clike", text, spans, {"REFERENCED_TOKEN_ABC"}, False,
    )
    assert res.changed is False
    assert res.new_text == text
