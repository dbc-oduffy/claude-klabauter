"""Regression coverage (2026-07-28, break-class): a present-but-empty
frontmatter key in a handoff must NOT read back as the FOLLOWING line's text.

The defect
==========
``coordinator_core/coverage.py``'s two raw handoff readers each carried a
forked frontmatter-key regex whose value pad was ``\\s*``::

    _parse_handoff_consumed_by    rf'^{field}:\\s*["\\']?([^"\\'#\\n\\r]+)["\\']?\\s*$'
    _parse_handoff_deliverable_id  r'^deliverable_id:\\s*["\\']?([^"\\'#\\n\\r]+)["\\']?\\s*$'

``\\s`` matches a NEWLINE, so on a handoff whose ``claimed_by:`` is present but
empty the pad walked past the line break and the capture took the next line —
returning the literal string ``'consumed_by: alice-session'`` as the claim
HOLDER, and ``'status: open'`` as the ``deliverable_id``. The ``[^"'#\\n\\r]+``
character class LOOKS newline-safe and is irrelevant: the pad consumed the
newline before the class began matching.

Why this one is higher blast radius than its siblings in the same sweep: these
two feed session attribution and liveness. A garbage claim-holder string can
make a live session look like a different holder — or a dead one look live —
which is what drives claim-takeover decisions on a shared branch.

Both functions now route key resolution through
``coordinator_core.frontmatter.primitives.read_fm_field_unquoted`` (the
canonical ``[ \\t]*`` pad + ``(?=[ \\t]|\\r?$)`` boundary lookahead). The
structural gate ``test_no_forked_frontmatter_key_regex.py`` prevents the fork
from being re-minted for the *interpolated*-key half; it is blind by
construction to ``_parse_handoff_deliverable_id``'s literal key, so the
literal-key half is held by THIS test and nothing else.

Every case is parametrized over LF and CRLF — the pre-fix ``\\s*`` swallowed
``\\r\\n`` just as happily as ``\\n``, and Windows is first-class here.
"""

from __future__ import annotations

import pytest

from coordinator_core.coverage import (
    _parse_handoff_consumed_by,
    _parse_handoff_deliverable_id,
)

EOLS = pytest.mark.parametrize("eol", ["\n", "\r\n"], ids=["lf", "crlf"])


def _write_handoff(tmp_path, lines: list[str], eol: str) -> str:
    """Write a handoff with EXACTLY the requested line ending.

    ``newline=""`` disables Python's newline translation, so a ``\\r\\n``
    fixture reaches disk as CRLF on every platform rather than being rewritten
    to the host convention — without it the CRLF half of each parametrization
    would silently degrade into a duplicate of the LF half on POSIX.
    """
    path = tmp_path / "handoff.md"
    path.write_text(eol.join(lines), encoding="utf-8", newline="")
    return str(path)


# ---------------------------------------------------------------------------
# _parse_handoff_consumed_by — the claim-holder reader
# ---------------------------------------------------------------------------

@EOLS
def test_empty_claimed_by_does_not_return_the_next_line(tmp_path, eol):
    """The reproduction. Pre-fix this returned the STRING
    ``'consumed_by: alice-session'`` as the claim holder."""
    path = _write_handoff(
        tmp_path,
        [
            "---",
            "id: h-1",
            "claimed_by:",
            "consumed_by: alice-session",
            "status: open",
            "---",
            "",
            "body",
        ],
        eol,
    )

    holder = _parse_handoff_consumed_by(path)

    assert holder != "consumed_by: alice-session"
    assert holder is None or ":" not in holder, (
        f"claim holder {holder!r} contains a `key: value` separator — the "
        "reader has captured a whole frontmatter LINE, not a value"
    )
    # `claimed_by` is empty, so the DR-084 transitional `consumed_by` fallback
    # legitimately supplies the holder — as a VALUE, not as the raw line.
    assert holder == "alice-session"


@EOLS
def test_empty_claimed_by_with_no_fallback_yields_no_holder(tmp_path, eol):
    """The attribution consequence, isolated from the ``consumed_by``
    fallback: an empty ``claimed_by:`` means UNCLAIMED, and the neighbouring
    line's value must not be conscripted into standing in for a holder.

    This is the case that decides a claim takeover. Pre-fix it reported
    ``'status: open'`` — a non-None holder, i.e. "somebody holds this" — for a
    handoff nobody had claimed.
    """
    path = _write_handoff(
        tmp_path,
        ["---", "id: h-2", "claimed_by:", "status: open", "---", "", "body"],
        eol,
    )

    assert _parse_handoff_consumed_by(path) is None


@EOLS
def test_empty_claimed_by_does_not_capture_a_live_session_id(tmp_path, eol):
    """Liveness-facing shape of the same bug: the line after an empty
    ``claimed_by:`` holds a REAL session id, so the pre-fix reader returned a
    string that merely CONTAINS a live session id
    (``'session_id: 2f904224-…'``) without equalling it. Such a holder matches
    no live session by equality, so a live handoff reads as dead — the
    inverse-direction attribution failure from the case above.
    """
    session_id = "2f904224-cee2-4ecb-8371-67e12d1d476b"
    path = _write_handoff(
        tmp_path,
        ["---", "claimed_by:", f"session_id: {session_id}", "---", ""],
        eol,
    )

    holder = _parse_handoff_consumed_by(path)

    assert holder is None
    assert holder != f"session_id: {session_id}"


@EOLS
def test_claimed_by_wins_over_consumed_by_when_both_present(tmp_path, eol):
    """Guards the DR-084 dual-tolerance precedence while the pad is being
    changed underneath it: ``claimed_by`` wins even when ``consumed_by``
    occurs EARLIER in the file (the dedicated-search-per-name property the
    function's own docstring calls out)."""
    path = _write_handoff(
        tmp_path,
        [
            "---",
            "consumed_by: old-session",
            "claimed_by: new-session",
            "---",
            "",
        ],
        eol,
    )

    assert _parse_handoff_consumed_by(path) == "new-session"


@EOLS
@pytest.mark.parametrize(
    "line,expected",
    [
        ("claimed_by: plain-session", "plain-session"),
        ("claimed_by: 'quoted-session'", "quoted-session"),
        ('claimed_by: "dq-session"', "dq-session"),
        ("claimed_by:    padded-session   ", "padded-session"),
        ("claimed_by: null", None),
        ("claimed_by: none", None),
    ],
    ids=["plain", "single-quoted", "double-quoted", "padded", "null", "none"],
)
def test_ordinary_values_survive_the_pad_change(tmp_path, line, expected, eol):
    """Negative control: the non-empty readings the fix must NOT disturb,
    including the quote-stripping and ``null``/``none`` sentinels the old
    regex handled with ``.strip("\\"'")``."""
    path = _write_handoff(tmp_path, ["---", "id: h-3", line, "---", ""], eol)

    assert _parse_handoff_consumed_by(path) == expected


@EOLS
def test_claimed_by_is_not_matched_by_a_longer_key(tmp_path, eol):
    """The boundary lookahead that comes with routing through the canonical
    primitive: ``claimed_by`` must not resolve against ``claimed_by_proxy:``.
    The old fork had no lookahead at all, so a prefix key could match."""
    path = _write_handoff(
        tmp_path,
        ["---", "claimed_by_proxy: proxy-session", "---", ""],
        eol,
    )

    assert _parse_handoff_consumed_by(path) is None


# ---------------------------------------------------------------------------
# _parse_handoff_deliverable_id — the literal-key sibling
#
# Invisible BY CONSTRUCTION to test_no_forked_frontmatter_key_regex.py, whose
# narrowing (1) requires a runtime-interpolated key. These cases are the only
# standing guard on this half.
# ---------------------------------------------------------------------------

@EOLS
def test_empty_deliverable_id_does_not_return_the_next_line(tmp_path, eol):
    """The second reproduction. Pre-fix this returned ``'status: open'``."""
    path = _write_handoff(
        tmp_path,
        ["---", "id: h-4", "deliverable_id:", "status: open", "---", "", "body"],
        eol,
    )

    resolved = _parse_handoff_deliverable_id(path)

    assert resolved != "status: open"
    assert resolved is None, (
        f"an empty deliverable_id must fall back to Session-Id-only "
        f"attribution (None), not to {resolved!r}"
    )


@EOLS
def test_empty_deliverable_id_does_not_capture_a_neighbouring_deliverable(tmp_path, eol):
    """Cross-attribution shape: the line after an empty ``deliverable_id:``
    names a DIFFERENT deliverable, so the pre-fix reader attributed this
    node's commits to a deliverable it has nothing to do with."""
    path = _write_handoff(
        tmp_path,
        ["---", "deliverable_id:", "parent_deliverable_id: dlv-other", "---", ""],
        eol,
    )

    assert _parse_handoff_deliverable_id(path) is None


@EOLS
@pytest.mark.parametrize(
    "line,expected",
    [
        ("deliverable_id: dlv-abc", "dlv-abc"),
        ("deliverable_id: 'dlv-quoted'", "dlv-quoted"),
        ("deliverable_id: null", None),
        ("deliverable_id: ~", None),
    ],
    ids=["plain", "quoted", "null", "tilde"],
)
def test_deliverable_id_ordinary_values_survive(tmp_path, line, expected, eol):
    """Negative control, including the schema-legal ``null`` and ``~``
    pre-backfill sentinels."""
    path = _write_handoff(tmp_path, ["---", line, "---", ""], eol)

    assert _parse_handoff_deliverable_id(path) == expected


@EOLS
def test_deliverable_id_is_not_matched_by_a_longer_key(tmp_path, eol):
    """Boundary lookahead on the literal-key half."""
    path = _write_handoff(
        tmp_path,
        ["---", "deliverable_id_source: dlv-wrong", "---", ""],
        eol,
    )

    assert _parse_handoff_deliverable_id(path) is None


def test_deliverable_id_missing_file_is_conservative_none(tmp_path):
    """Unchanged contract: an unreadable path is conservative-None, never a
    raise — the OSError guard must survive the routing change."""
    assert _parse_handoff_deliverable_id(str(tmp_path / "nope.md")) is None
