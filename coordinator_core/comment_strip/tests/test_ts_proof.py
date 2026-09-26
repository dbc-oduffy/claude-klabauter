"""Regression tests for the two unsound TS/JS cases the EM's tscheck caught:

1. `//` inside a template literal's `${...}` substitution tail was mis-scanned as a real
   line comment by the old raw-scanner approach, truncating the template.
2. JSDoc prose quoting `*/` ends the real comment early per the language grammar; the old
   proof (re-lex-and-diff) could not tell that apart from a sound removal.

Both must now be caught by the AST/printer-based proof (`engine._proof_ts` / `scan_ts.js`),
which requires a `typescript` package to be resolvable (`engine._find_ts_pkg`) — these tests
skip outright when none is available rather than asserting on the fixture's absence.
"""
from __future__ import annotations

import pytest

from coordinator_core.comment_strip import engine

pytestmark = pytest.mark.skipif(
    engine._find_ts_pkg() is None, reason="no vendored typescript package available"
)


def _plan(tmp_path, name: str, text: str):
    (tmp_path / name).write_text(text, encoding="utf-8")
    try:
        return engine.plan_file(tmp_path, name, set())
    finally:
        engine._TsWarmProc.close_all()


def test_template_substitution_tail_not_mistaken_for_comment(tmp_path):
    # The `//not-a-comment` text lives inside the template's substitution tail; a real
    # removable comment sits elsewhere so a plan is actually attempted. A raw-scanner
    # implementation loses template context right after `${a}` and treats the `//` as a
    # genuine line comment, truncating the template and everything up to the next newline.
    text = (
        "const x = `${a}//not-a-comment${b}`;\n"
        "foo(); // a real comment to remove\n"
    )
    res = _plan(tmp_path, "tpl.ts", text)
    assert res.proof_ok is True
    assert res.changed is True
    assert res.new_text is not None
    assert "`${a}//not-a-comment${b}`" in res.new_text
    assert "a real comment to remove" not in res.new_text


def test_jsdoc_after_template_substitution_not_eaten(tmp_path):
    # A template with a `${...}` substitution desyncs a raw scanner (no `reScanTemplateToken`
    # in the loop): after `}`, the next backtick it meets is (wrongly) read as opening a NEW
    # template that runs to EOF, swallowing this JSDoc, the function body and the trailing
    # `//` comment whole as fake template text. The old scanner reported zero comments here —
    # a silent false negative — and, with a second backtick later in the file, would instead
    # truncate a real comment/code boundary (case 1's mechanism). The AST-based collector must
    # see and correctly remove BOTH real comments while leaving the code untouched.
    text = (
        "const url = `${host}/api`;\n"
        "\n"
        "/**\n"
        " * Real doc comment that should be removed.\n"
        " */\n"
        "function f() { return 1; } // trailing real comment\n"
    )
    res = _plan(tmp_path, "doc.ts", text)
    assert res.proof_ok is True
    assert res.changed is True
    assert res.new_text is not None
    assert "Real doc comment" not in res.new_text
    assert "trailing real comment" not in res.new_text
    assert "const url = `${host}/api`;" in res.new_text
    assert "function f() { return 1; }" in res.new_text


def test_ordinary_comment_near_template_still_removed(tmp_path):
    # Sanity check the fix doesn't just refuse everything touching a template literal.
    text = (
        "// header comment\n"
        "export const url = `https://example.com/${path}`;\n"
    )
    res = _plan(tmp_path, "ok.ts", text)
    assert res.proof_ok is True
    assert res.changed is True
    assert "header comment" not in res.new_text
    assert "`https://example.com/${path}`" in res.new_text
