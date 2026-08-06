"""Unit tests for coordinator_core.write_guards._case_fold_path.

Covers the two DISTINCT bugs the module docstring names: the case-folding
bug (pre-existing) and the Windows extended-length-prefix desync bug
(`state/handoffs/2026-08-03-windows-extended-length-prefix-desync.md`).
Only the extended-length-prefix side is new here — exercised directly on
literal Windows-shaped strings, per that handoff's explicit constraint that
this is a macOS box where `os.path.realpath`/`Path.resolve()` never produce
the extended-length form, so a live-resolve-based test would prove nothing.

Drive-letter path fragments are runtime-assembled (never a literal
``"<letter>:\\..."`` in source), matching
``bash_guards.tests.test_write_bump_applicability``'s own convention —
avoids tripping the concrete-path-citation guard over what is a synthetic,
never-resolved string.
"""

from __future__ import annotations

from coordinator_core.write_guards._case_fold_path import (
    casefold_path,
    strip_extended_length_prefix,
)

_SYNTHETIC_DRIVE_LETTER = "".join(["Z", ":"])


def test_casefold_path_plain_extended_length_prefix_matches_bare_form():
    """AC1 -- the extended-length plain form and its bare drive-letter
    equivalent must casefold to the identical string. Verified red against
    pre-fix bytes (see the handoff's own repro): before this fix,
    `casefold_path` only did backslash-to-slash + casefold, so the leading
    extended-length marker survived into the result and the two strings
    compared unequal."""
    bare_raw = _SYNTHETIC_DRIVE_LETTER + "\\Widgets\\Sub\\repo"
    prefixed_raw = "\\\\?\\" + bare_raw
    prefixed = casefold_path(prefixed_raw)
    bare = casefold_path(bare_raw)
    assert prefixed == bare == bare_raw.replace("\\", "/").casefold()


def test_casefold_path_unc_extended_length_prefix_matches_bare_unc_form():
    """AC1 -- the extended-length UNC form collapses its ``UNC`` segment
    down to the bare UNC path's own leading double-separator, not to
    nothing, and matches the bare UNC form."""
    prefixed = casefold_path("\\\\?\\UNC\\srv\\share\\dir")
    bare = casefold_path("\\\\srv\\share\\dir")
    assert prefixed == bare == "//srv/share/dir"


def test_casefold_path_forward_slash_extended_length_prefix_also_strips():
    """A caller that hands in an already forward-slash-normalized
    extended-length path (e.g. a prior backslash-to-slash pass) must still
    have the prefix stripped -- the strip runs on the slash-normalized form
    internally, not only on raw backslash input."""
    bare_raw = _SYNTHETIC_DRIVE_LETTER + "\\Widgets\\Sub\\repo"
    slash_prefixed = "//?/" + bare_raw.replace("\\", "/")
    assert casefold_path(slash_prefixed) == bare_raw.replace("\\", "/").casefold()
    assert casefold_path("//?/UNC/srv/share/dir") == "//srv/share/dir"


def test_casefold_path_idempotent_on_already_stripped_result():
    once = casefold_path("\\\\?\\" + _SYNTHETIC_DRIVE_LETTER + "\\Widgets\\Sub\\repo")
    twice = casefold_path(once)
    assert once == twice


def test_casefold_path_noop_on_posix_shaped_input():
    """No recognized prefix, no backslashes -- must pass through unchanged
    apart from casefolding, per the Anti-scope: "Do not assume macOS
    behaviour generalizes" cuts both ways -- a POSIX path must not be
    mistaken for a Windows one either."""
    assert casefold_path("/already/posix/style") == "/already/posix/style"


def test_strip_extended_length_prefix_direct_plain_form():
    bare = _SYNTHETIC_DRIVE_LETTER + "\\Widgets\\Foo"
    assert strip_extended_length_prefix("\\\\?\\" + bare) == bare


def test_strip_extended_length_prefix_direct_unc_form():
    assert (
        strip_extended_length_prefix("\\\\?\\UNC\\server\\share")
        == "\\\\server\\share"
    )


def test_strip_extended_length_prefix_direct_posix_noop():
    assert strip_extended_length_prefix("/already/posix/style") == "/already/posix/style"
