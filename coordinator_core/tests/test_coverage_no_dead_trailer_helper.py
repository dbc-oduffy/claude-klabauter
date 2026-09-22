"""Standing tripwire: ``coordinator_core/coverage.py`` carries no unreferenced
batched-trailer-lookup helper.

A prior large deletion from this module left one function behind that its
own docstring already documented as having zero remaining callers (the
subprocess it built was for a leg that had itself been removed). Nothing in
production or test code called it, and its only other repo mention was a
docstring elsewhere describing the *shape* it modelled -- prose, not a call
site. This test pins that the helper stays gone rather than accreting again
as a second orphan the next time this module is trimmed.
"""

from __future__ import annotations

import coordinator_core.coverage as coverage


def test_commit_deliverable_id_trailers_not_reintroduced():
    """The batched sha->Deliverable-Id trailer reader has no live caller and
    must not be reintroduced as dead weight in this module."""
    assert not hasattr(coverage, "_commit_deliverable_id_trailers")
