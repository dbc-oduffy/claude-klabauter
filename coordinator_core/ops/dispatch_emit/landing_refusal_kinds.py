"""Closed table of the only reasons a landing may refuse; every other finding is provenance."""

from __future__ import annotations

LANDING_REFUSAL_KINDS = frozenset({"destructive-git", "secret-in-diff"})

PROVENANCE_KINDS = frozenset({
    "peer-hunks-co-declared",
    "out-of-ask-hunks",
    "unreadable-peer-claims",
    "shipped-claim-unbacked",
    "import-closure-untracked",
    "since-base-overlap",
    "py-unparseable",
    "targeted-tests-failed",
    "review-reverify-failed",
    "review-reverify-indeterminate",
    "tests-not-run",
    "cohold-declared-path",
    "halted-run",
    "recovery-withheld-unbacked",
})


def is_refusal(reason: str) -> bool:
    """True only for a reason in ``LANDING_REFUSAL_KINDS``; anything else (a provenance kind or
    an unknown string) is recorded and never refuses the landing."""
    return reason in LANDING_REFUSAL_KINDS
