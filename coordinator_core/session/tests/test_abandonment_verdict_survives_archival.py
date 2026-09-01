"""
coordinator_core.session.tests.test_abandonment_verdict_survives_archival —
the standing occasion for the prime exit criterion of
docs/plans/2026-09-01-the-abandonment-verdict-outlives-the-archiver.md
(chunk C4).

Purpose: relocates `tasks/abandonment-falsifier/falsify.py`'s discriminator
into a standing test (`tasks/` is ephemera in this repo, per that plan's own
C4 body — a test was chosen over a cadence row, which runs too rarely to
catch a regression near its introduction, and over a registered op, which
would mint a new op identity DR-344 rules out). Reads the LIVE `state/
handoffs/*.md` corpus and asserts the PROPERTY, never a count: no holder in
the abandonment population may receive the same answer as a live holder, and
every claimed baton must resolve into one of `liveness`'s own named
`raw_basis` values (`_RAW_BASIS_VALUES` below — `adjudicate_claimed_batons`
carries no second, derived vocabulary of its own). SKIPS when
the live corpus carries no claimed batons at all, so a fresh clone reports
honestly rather than passing vacuously on an empty population — a green with
nothing to check is the same instrument defect this plan's own lesson names
(state/lessons/2026-08-26-liveness-has-three-answers-not-two-and-m-
23bdebd1994e.yaml).

Baseline (historical record, measured at the `tasks/` path before this
relocation, do not re-measure or update): 9e3c0151647a7f4b73370e84f62d15b1caff9f8b
recorded 20 unadjudicated claimed batons and FALSIFIED. From C2's repoint of
the falsifier at `abandonment_basis`/`session_live` onward, the same corpus
resolves every row into a named bucket -- this file is that same
discriminator's only home after C4 (the `tasks/` copy is deleted in the same
chunk).

Run from the repo root: python -m pytest
coordinator_core/session/tests/test_abandonment_verdict_survives_archival.py -q
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.pickup_assemble.apply import adjudicate_claimed_batons
from coordinator_core.session import liveness as _liveness

pytestmark = [pytest.mark.cadence]

#: `liveness.abandonment_basis`'s own vocabulary (`no-sid`/`live`/
#: `archive-record`/`live-dir-signals`/`unknown`) plus the two call-site
#: values `adjudicate_claimed_batons` mints before ever reaching
#: `abandonment_basis` (`no-sid` for an unresolvable holder, `live` for one
#: `session_live` already confirmed) — the one vocabulary this sweep reports.
_RAW_BASIS_VALUES = (
    "no-sid",
    "live",
    "archive-record",
    "live-dir-signals",
    "unknown",
)


def _repo_root() -> Path:
    # This test file lives at <root>/coordinator_core/session/tests/.
    return Path(__file__).resolve().parents[3]


def test_every_claimed_baton_resolves_into_exactly_one_named_bucket():
    root = _repo_root()
    exit_code, report = adjudicate_claimed_batons(repo_root=root)

    assert exit_code == 0, report  # APPLY_EXIT_OK -- a completed sweep, never a transport failure

    if report["claimed_count"] == 0:
        pytest.skip("live corpus carries no `status: claimed` handoffs -- nothing to adjudicate")

    for row in report["rows"]:
        assert row["raw_basis"] in _RAW_BASIS_VALUES, (
            f"{row['path']}: raw_basis {row['raw_basis']!r} is not one of {_RAW_BASIS_VALUES}"
        )


def test_no_non_live_holder_reads_as_live():
    """The PROPERTY this instrument exists to check: no holder in the
    abandonment population (not confirmed live via `session_live`) may
    receive the same answer (`"live"`) as a genuinely live holder -- that
    indistinguishability is exactly the defect the plan's Problem statement
    names (an abandoned holder's claim reading identically to a healthy
    one's)."""
    root = _repo_root()
    exit_code, report = adjudicate_claimed_batons(repo_root=root)
    assert exit_code == 0, report

    if report["claimed_count"] == 0:
        pytest.skip("live corpus carries no `status: claimed` handoffs -- nothing to adjudicate")

    for row in report["rows"]:
        sid = row["claimed_by"]
        if not sid:
            assert row["raw_basis"] == "no-sid"
            continue
        really_live = _liveness.session_live(sid, str(root))
        if row["raw_basis"] == "live":
            assert really_live, (
                f"{row['path']}: reported 'live' but session_live({sid!r}) is False"
            )
        else:
            assert not really_live, (
                f"{row['path']}: session_live({sid!r}) is True but reported "
                f"raw_basis {row['raw_basis']!r} -- a live holder must never resolve "
                "into a non-'live' basis"
            )
