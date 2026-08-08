"""Regression: a banned token adjacent to a backslash escape must be a finding.

THE FALSE-NEGATIVE CLASS THIS CLOSES
------------------------------------
`_tok` bounds every banned token with an ALPHANUMERIC lookbehind, deliberately,
so `_` and `-` count as separators. But source that ships regex or string
literals writes a name as ``\\bName\\b`` -- and the ``b`` of that preceding
escape IS alphanumeric. The lookbehind therefore read it as an intra-word
occurrence and skipped it, so any ``\\<letter>``-adjacent persona name in any
payload was invisible to the gate whose entire purpose is to catch it. A real
leak (`lib/percolate/phase4_audit.py`'s persona pattern table) shipped publicly
through this hole.

NEGATIVE SPEC: closing it must NOT widen the boundary generally. A token glued
into a longer word (`MyName`, `xxNamexx`) stays a non-finding -- the escape
alternative requires a literal backslash immediately before a single letter.

Fixtures assemble their tokens from fragments for the same reason the checker
does: a contiguous literal in these bytes would itself be the residual.
"""
from __future__ import annotations

import importlib.util
import pathlib

_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[3]
    / "dist"
    / "mirror-native"
    / "claude-klabauter"
    / ".github"
    / "scripts"
    / "check-persona-names.py"
)

_PERSONA = "Came" + "lia"
_CODENAME = "mak" + "ima"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_persona_names", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _labels(text: str) -> list[str]:
    return [label for label, _ in _load_module().findings_in(text, "some/file.py")]


def test_word_boundary_escape_adjacent_persona_is_a_finding():
    text = '    r"\\b' + _PERSONA + '\\b",'
    assert "persona" in _labels(text), (
        "a persona name written as a regex word-boundary literal was not reported"
    )


def test_word_boundary_escape_adjacent_codename_is_a_finding():
    text = 'pattern = r"\\b' + _CODENAME + '_ROOT\\b"'
    assert "fleet codename" in _labels(text)


def test_other_backslash_escapes_are_also_boundaries():
    text = '"line one\\t' + _PERSONA + '"'
    assert "persona" in _labels(text)


def test_genuine_intra_word_occurrence_is_still_not_a_finding():
    assert _labels("My" + _PERSONA) == []
    assert _labels("xx" + _PERSONA + "xx") == []


def test_plain_occurrence_still_a_finding():
    assert "persona" in _labels(_PERSONA + " reviewed it")


def test_underscore_and_hyphen_still_separate():
    assert "persona" in _labels("role_" + _PERSONA + "_id")
    assert "persona" in _labels("role-" + _PERSONA + "-id")


def test_permitted_span_behaviour_unchanged_by_the_new_boundary():
    module = _load_module()
    text = "See dbc-oduffy/project-" + "claude-klabauter for details."
    spans = module.permitted_spans(text, "some/file.py")
    assert len(spans) == 1 and text[spans[0][0]:spans[0][1]] == "dbc-example-operator"


# KNOWN, ACCEPTED FALSE-POSITIVE CLASS
# -------------------------------------
# The escape alternative is deliberately escape-agnostic: it checks for
# `\<single letter>` immediately before the token, without verifying the
# letter is one of the real Python/regex escape letters (b, t, n, r, f, v,
# s, w, d, ...). That is by design -- narrowing to the real escape-letter set
# does not solve the case it would be narrowed for (`x` IS a real escape
# letter, via `\x41`), so a Windows path segment or line-continuation
# backslash glued directly to a single letter and then a banned token can
# also fire. This table pins the accepted tradeoff rather than leaving it
# unstated: a rare false positive is preferred over missing a real persona
# leak on a public-publish boundary.
def test_single_backslash_letter_prefix_false_positive_is_accepted():
    # The real leak this alternative exists to catch.
    assert _labels('    r"\\b' + _PERSONA + '\\b",') != []
    # Confirmed false positive: `x` is a real escape letter (`\x41`), so
    # narrowing to "real" escape letters would not exclude this shape either.
    assert _labels("\\x" + _PERSONA) != []
    # A real name glued into an actual path -- must keep firing.
    assert _labels("C:\\" + _PERSONA + "\\d") != []  # abs-path-ok: fixture, not a real filesystem path


def test_two_char_backslash_prefix_is_not_escape_adjacent():
    # Two letters between the backslash and the token: the escape alternative
    # is fixed-width (one letter only), so this stays a non-finding.
    assert _labels("\\y" + _PERSONA + "Docs") == []
