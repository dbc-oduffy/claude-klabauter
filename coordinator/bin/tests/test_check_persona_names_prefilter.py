"""Regression: the `_LEAD`-stripped prefilter must never narrow a real finding.

THE OPTIMISATION THIS PINS
---------------------------
`_prefilter_for` derives a cheap per-pattern superset gate at runtime by
stripping each `BANNED` pattern's leading `_LEAD` lookbehind. `_LEAD` is
zero-width, so removing it can only ENLARGE the match set -- the residual
matches everything the full pattern did, plus whatever the lookbehind used to
reject. `findings_in` uses the prefilter only to decide whether the expensive
full pattern runs at all on a given text; it never substitutes the prefilter's
match objects for the real ones.

AC3 ORACLE (pinned here)
-------------------------
1. A planted violation of each of the 15 `BANNED` patterns is still caught
   end-to-end through `findings_in` -- the prefilter gate never suppresses a
   real hit.
2. Every derived prefilter matches at least everything its own full pattern
   matches, including the escape-adjacent boundary case from
   `test_check_persona_names_escape_adjacent.py` -- the superset property
   must hold on the same boundary shapes that pattern relies on, not merely
   on a plain occurrence.
3. `_prefilter_for` fails closed: a pattern that does not start with `_LEAD`
   becomes its own prefilter (no speedup, but never narrower than itself).

Fixtures assemble their tokens from fragments for the same reason the checker
and its sibling test do: a contiguous literal here would be indistinguishable
from a real leak to the upstream residual-pattern guard.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re

_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[3]
    / "dist"
    / "mirror-native"
    / "claude-klabauter"
    / ".github"
    / "scripts"
    / "check-persona-names.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("check_persona_names", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# One planted violation per `BANNED` entry, IN ORDER. Each must be a bare,
# boundary-clean occurrence of that entry's token so it is unambiguous which
# pattern it is meant to trip.
_VIOLATIONS: list[str] = [
    "Zol" + "i",  # persona: Zol[ií]
    "the Data Science Reviewer",  # persona
    "Pal" + "i",  # persona: Pal[ií]
    "the Staff Engineer",  # persona
    "the Game Dev Reviewer",  # persona (case-sensitive)
    "the UX Reviewer",  # persona (case-sensitive)
    "the VP-Product Reviewer",  # persona (case-sensitive)
    "nimb" + "alyst",  # codename
    "gastown" + "hall",  # codename
    "mak" + "ima",  # fleet codename
    "opti" + "con",  # fleet codename
    "holo" + "deck",  # fleet codename
    "del" + "phi",  # fleet codename
    "project" + "-" + "rag",  # fleet codename
    "o" + "duffy",  # operator identity: o['’]?duffy
]


def _bracket(token: str) -> str:
    """Wrap TOKEN with plain-space boundaries so it is unambiguously bounded."""
    return f" {token} "


def test_violation_fixtures_align_one_to_one_with_banned_entries():
    module = _load_module()
    assert len(_VIOLATIONS) == len(module.BANNED), (
        "the planted-violation fixture list must cover every BANNED entry "
        "exactly once, in order"
    )


def test_each_planted_violation_is_still_caught_end_to_end():
    module = _load_module()
    for (label, pattern), token in zip(module.BANNED, _VIOLATIONS):
        text = _bracket(token)
        assert pattern.search(text), (
            f"fixture {token!r} does not even match its own full pattern "
            f"for label {label!r} -- fixture is wrong, not the checker"
        )
        labels = [lbl for lbl, _ in module.findings_in(text, "some/file.py")]
        assert label in labels, (
            f"planted violation of {label!r} ({token!r}) was not reported by "
            "findings_in -- the prefilter gate suppressed a real finding"
        )


def test_each_prefilter_matches_at_least_its_own_full_pattern():
    module = _load_module()
    for (label, pattern), prefilter, token in zip(
        module.BANNED, module._PREFILTERS, _VIOLATIONS
    ):
        text = _bracket(token)
        assert pattern.search(text)
        assert prefilter.search(text), (
            f"prefilter for {label!r} ({token!r}) failed to match text its "
            "own full pattern matches -- the derived gate is narrower than "
            "the pattern it stands in for, which can hide a real finding"
        )


def test_prefilter_still_matches_the_escape_adjacent_boundary_case():
    """The `_LEAD` second alternative (`(?<=\\\\[A-Za-z])`) is the false-negative
    class `test_check_persona_names_escape_adjacent.py` closes. Stripping
    `_LEAD` must not lose coverage of that same boundary shape: a token glued
    to a backslash-escape must still trip the prefilter, since the prefilter
    has no lookbehind at all and therefore imposes no boundary requirement on
    the left side -- it is a strict superset by construction, but this pins
    that property on the exact boundary shape the escape fix exists for.
    """
    module = _load_module()
    for (label, pattern), prefilter, token in zip(
        module.BANNED, module._PREFILTERS, _VIOLATIONS
    ):
        text = 'r"\\b' + token + '\\b"'
        if not pattern.search(text):
            # Case-sensitive short tokens (the Game Dev Reviewer/the UX Reviewer/the VP-Product Reviewer) plus multi-word/space
            # entries may not reassemble cleanly glued to `\\b...\\b` without
            # their own internal separators; only assert the superset
            # property where the full pattern itself actually fires here.
            continue
        assert prefilter.search(text), (
            f"prefilter for {label!r} lost the escape-adjacent match its "
            "full pattern still catches"
        )


def test_prefilter_derivation_fails_closed_when_lead_is_absent():
    module = _load_module()
    odd_pattern = re.compile(r"someliteralwithnolead", re.IGNORECASE)
    derived = module._prefilter_for(odd_pattern)
    assert derived.pattern == odd_pattern.pattern
    assert derived.flags == odd_pattern.flags


def test_prefilter_strips_exactly_the_lead_prefix_when_present():
    module = _load_module()
    sample = module.BANNED[0][1]
    assert sample.pattern.startswith(module._LEAD)
    derived = module._prefilter_for(sample)
    assert derived.pattern == sample.pattern[len(module._LEAD):]


# ---------------------------------------------------------------------------
# The union gate and the lazy permitted-span computation. Both are pure cost
# reductions layered on top of the per-pattern prefilter above: neither may
# change a single finding, and the union gate in particular runs BEFORE every
# other check, so a union narrower than the fifteen it stands in for would
# silently suppress a real leak on every line of every publish.
# ---------------------------------------------------------------------------


def test_union_gate_matches_whenever_any_individual_prefilter_does():
    module = _load_module()
    union = module._ANY_BANNED_PREFILTER
    assert union is not None, (
        "the union gate failed to build for today's BANNED table -- findings_in "
        "still works (it falls back to the fifteen individual prefilters) but "
        "this test exists to catch that silently happening"
    )
    for (label, _pattern), prefilter, token in zip(
        module.BANNED, module._PREFILTERS, _VIOLATIONS
    ):
        text = _bracket(token)
        assert prefilter.search(text)
        assert union.search(text), (
            f"union gate missed {label!r} ({token!r}) although that entry's own "
            "prefilter matches -- the first gate is narrower than what it gates"
        )


def test_union_gate_preserves_per_entry_case_sensitivity():
    """`the Game Dev Reviewer`/`the UX Reviewer`/`the VP-Product Reviewer` are case-SENSITIVE on purpose so ordinary lowercase
    identifiers in engine code are not false positives. An alternation carries
    one flag set for the whole pattern, so the union must scope IGNORECASE per
    branch -- a union that applied it globally would re-introduce exactly the
    false-positive class those three entries are written to avoid.
    """
    module = _load_module()
    union = module._ANY_BANNED_PREFILTER
    assert union is not None
    for (label, pattern), token in zip(module.BANNED, _VIOLATIONS):
        if pattern.flags & re.IGNORECASE:
            continue
        lowered = _bracket(token.lower())
        assert not pattern.search(lowered), (
            f"fixture assumption broken: {label!r} is meant to be case-sensitive"
        )
        assert not union.search(lowered), (
            f"union gate matched the lowercase form of case-sensitive entry "
            f"{label!r} -- IGNORECASE leaked across branches"
        )


def test_union_gate_short_circuits_only_lines_with_no_possible_match():
    module = _load_module()
    clean = "def resolve_session_identifier(self, sid_value: str) -> None:"
    assert module.findings_in(clean, "coordinator_core/some_module.py") == []
    for token in _VIOLATIONS:
        text = _bracket(token)
        assert module.findings_in(text, "some/nested/file.py"), (
            f"{token!r} produced no finding through the full findings_in path"
        )


def test_permitted_spans_still_suppress_when_computed_lazily():
    """`permitted_spans` is now built on the first ban match rather than up
    front. The suppression it provides must be unchanged: a permitted span on
    an attribution surface still swallows the match inside it.
    """
    module = _load_module()
    handle = "dbc-" + "example-operator"
    slug = f"see github.com/{handle}/some-repo for details"
    assert module.findings_in(slug, "README.md") == [], (
        "the owner/repo slug form is a permitted span and must still suppress "
        "the operator-identity match inside it"
    )
    bare = " " + "o" + "duffy" + " "
    assert module.findings_in(bare, "coordinator_core/nested.py"), (
        "a bare operator-identity token in engine source is not inside any "
        "permitted span and must still be reported"
    )
