"""Pins `_GRANT_DETAIL_POINTER`'s target content -- the module docstring's
GRANT leg -- against the exact facts the pointer claims live there.

Why this file exists (Review: coordinator:code-reviewer, Finding 1): the
2026-07-30 word-budget cut moved the ceremony-by-ceremony grant enumeration,
the no-implicit-grant ceremony list, and the session-scope/liveness
paragraph out of `_deny_reason_grant`'s inline deny text and behind
`_GRANT_DETAIL_POINTER`. Two of those facts were silently DELETED rather
than relocated -- the pointer's own comment and `_deny_reason_grant`'s own
docstring both claimed the content "lives" at the module docstring's GRANT
leg, but a grep of the whole module turned up zero occurrences of
`bug-blitz`, `mise-en-place`, `session-scoped`, or `liveness-gated` outside
the claim itself. This mirrors
`test_operator_override_note_retains_affordances.py`'s doc-content pins for
the analogous `OVERRIDE_KEYS_DOC` cut -- that suite is why the equivalent
trim there did not lose anything, and its absence here is why this one did.

NEGATIVE SPEC -- do not weaken these to a substring-anywhere-in-the-file
check. The claim is specifically that this content lives in the MODULE
DOCSTRING's GRANT leg (what `_GRANT_DETAIL_POINTER` names), so the assertion
reads the module docstring itself, not the whole source text.
"""

from __future__ import annotations

from coordinator_core.bash_guards import check_test_suite_invocation as guard


def _grant_leg_text() -> str:
    doc = guard.__doc__ or ""
    assert doc, "check_test_suite_invocation module docstring is empty/missing"
    return doc


def test_grant_leg_names_the_no_implicit_grant_ceremonies():
    """`_GRANT_DETAIL_POINTER` and `_deny_reason_grant`'s own docstring both
    claim the no-implicit-grant ceremony list "lives" at the module
    docstring's GRANT leg. If any of these four ceremony names is missing,
    a caller following the pointer will not learn that ceremony does NOT
    carry an implicit Tier-U grant."""
    text = _grant_leg_text()
    for ceremony in (
        "bug-blitz",
        "bug-sweep",
        "mise-en-place",
        "finishing-a-development-branch",
    ):
        assert ceremony in text, (
            "GRANT leg must name /%s as NOT carrying an implicit Tier-U grant "
            "-- this fact was silently deleted by the 2026-07-30 word-budget "
            "cut, not relocated, and the pointer claims it is here" % ceremony
        )


def test_grant_leg_states_the_session_scope_and_liveness_constraint():
    """A grant is session-scoped and liveness-gated: one PM ask covers the
    asking session for its lifetime, but a grant left behind by a dead
    session never authorizes a different (even resumed) session. This is
    exactly what an agent gets wrong by default -- the whole reason it was
    written down in the first place."""
    text = _grant_leg_text()
    assert "session-scoped" in text or "session scoped" in text, (
        "GRANT leg must state a grant is session-scoped"
    )
    assert "liveness-gated" in text or "liveness gated" in text, (
        "GRANT leg must state a grant is liveness-gated"
    )


def test_grant_leg_names_the_ceremonies_that_do_carry_an_implicit_grant():
    """The ceremonies that DO write an implicit grant at ceremony open must
    stay named, so the no-implicit-grant list above reads as a contrast,
    not a standalone claim with no baseline. Verified against DoE's tree:
    `coordinator/commands/workday-complete.md` (DoE `8a3efb1d8`),
    `coordinator/skills/merging-to-main/SKILL.md`, and
    `coordinator/commands/workweek-complete.md` each invoke
    `tier-u-grant-cli grant ceremony`."""
    text = _grant_leg_text()
    for ceremony in ("workday-complete", "workweek-complete", "merging-to-main"):
        assert ceremony in text, (
            "GRANT leg must keep naming /%s as an implicit-grant ceremony" % ceremony
        )


def test_grant_leg_names_the_explicit_pm_grant_and_the_override_warning():
    """Even with all three ceremony writers present, a session with no live
    grant (e.g. outside any of the three ceremonies) still needs the
    explicit-PM-grant path named, and the override warned against -- the
    override remains the cheapest-looking wrong exit whenever a grant is
    absent, regardless of ceremony coverage."""
    text = _grant_leg_text()
    assert "tier-u-grant-cli grant\n     pm" in text or "tier-u-grant-cli grant pm" in text, (
        "GRANT leg must name the explicit PM grant as the honest path"
    )
    assert "COORDINATOR_OVERRIDE_TEST_SUITE_INVOCATION" in text, (
        "GRANT leg must name the override and say it is not a substitute "
        "for the explicit grant"
    )
