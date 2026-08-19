"""
Tests for `review_trail_write._attestation_remedy_clause` — the tail of the
single-commit foreign-trailer refusal, which must name the attestation route
only when that route can actually succeed.

The defect this covers: AC1c admits an attested commit only if it sits inside
this session's own frozen-diff range, and the sole producer of that range
(`freeze-review-diff.py`'s `_open_pending_trail_record`) writes through this
same op with no `attestation_dispatch_id` — so it is refused at the very
branch whose remedy text named the attestation route. For an inherited
chain commit the range cannot come to contain the commit, and the printed
remedy named a call that could never succeed.
→ state/bug-backlog/2026-08-19-inherited-chain-commit-review-records-are-unwritable.yaml

Lives in its own module rather than in `test_review_trail_write.py` because
that module is marked `cadence` + `spawns_process` wholesale; these cases
spawn nothing (both git-touching helpers are mocked) and belong on the fast
tier.

Negative-spec:
    - Does NOT assert on the full refusal message. The undetermined-note and
      the no-narrower-range sentence are a different contract with their own
      coverage; pinning them here would make this module fail on an edit it
      has no opinion about.
    - Does NOT build a git repo to produce a real frozen range. The clause's
      whole input is the two SHA sets, so a fixture repo would add spawn cost
      (the amplification ratchet) and test `git rev-list`, not this branch.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from coordinator_core.ops import review_trail_write

_SHA = "53b61a1fefcd60995d969f968c38ab9c23f4a4ed"
_RANGE = f"{_SHA}~1..{_SHA}"
_SESSION = "165d5ec7-bb3c-4705-9116-5f3a116822f1"


def _clause(range_shas, frozen_shas) -> str:
    with mock.patch.object(
        review_trail_write, "_resolve_range_shas", return_value=range_shas,
    ), mock.patch.object(
        review_trail_write, "_own_frozen_diff_shas", return_value=frozen_shas,
    ):
        return review_trail_write._attestation_remedy_clause(_RANGE, _SESSION, Path("."))


def test_commit_outside_the_frozen_range_is_told_the_record_is_unwritable() -> None:
    """The inherited-chain population: an empty frozen range (what every
    measured close actually has, because the freeze that would populate it is
    refused at this same branch) must NOT be told to supply an attestation."""
    clause = _clause(frozenset({_SHA}), frozenset())

    assert "No review-trail record naming this commit is writable" in clause
    assert "attestation_dispatch_id) admits a commit only inside" in clause
    assert "supply reviewer_evidence" not in clause


def test_unwritable_clause_names_the_own_commit_chain_record_trap() -> None:
    """The own-commit `--scope chain` write SUCCEEDS and discharges nothing —
    a reader who tries it gets a rc=0 that looks like discharge. The clause
    has to name that, or it trades one unreachable remedy for a misleading
    one."""
    clause = _clause(frozenset({_SHA}), frozenset())

    assert "not a substitute" in clause
    assert "succeeds and covers nothing" in clause


def test_commit_inside_the_frozen_range_still_gets_the_attestation_remedy() -> None:
    """The reachable population is unchanged: a commit this session froze for
    review CAN be attested, and must still be told how."""
    clause = _clause(frozenset({_SHA}), frozenset({_SHA, "aaaa" * 10}))

    assert "supply reviewer_evidence" in clause
    assert "attestation_dispatch_id (that dispatch's" in clause
    assert "is writable" not in clause


def test_partially_frozen_range_is_not_treated_as_reachable() -> None:
    """AC1c's own test is `issubset`, not intersection — a range only
    partly inside the frozen set would be refused by AC1c, so the clause must
    not promise it the attestation route."""
    other = "b" * 40
    clause = _clause(frozenset({_SHA, other}), frozenset({_SHA}))

    assert "No review-trail record naming this commit is writable" in clause


def test_unresolvable_range_fails_toward_naming_the_remedy() -> None:
    """`_resolve_range_shas` returns None on any git failure, which is
    indistinguishable here from 'genuinely not frozen'. Telling a caller to
    try a route that then refuses costs one command; telling them a record is
    unwritable when it was writable loses the record — so the ambiguous case
    takes the recoverable side."""
    clause = _clause(None, frozenset())

    assert "supply reviewer_evidence" in clause
