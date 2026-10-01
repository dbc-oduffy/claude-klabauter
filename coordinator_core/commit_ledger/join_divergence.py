"""
coordinator_core.commit_ledger.join_divergence -- pure comparison of the
claim-derived ledger join and the Deliverable-Id trailer join, one outcome
per commit.

Takes two already-resolved sha -> deliverable-id maps and a sha sequence,
and buckets every sha into agree / disagree / ledger_only / trailer_only /
neither. `basis` states both coverage figures in plain language, because a
bare disagreement count of 0 reads as agreement when the two joins rarely
overlap.

Negative spec: no I/O, no git, no subprocess, no pathlib, no threshold, no
verdict. The report carries no exit code, no pass/fail field and no refusal
arm; a caller cannot mistake it for a gate. Reading the ledger and the
trailers is the caller's remit.
"""

from typing import Dict, List, Mapping, NamedTuple, Optional, Sequence

AGREE, DISAGREE, LEDGER_ONLY, TRAILER_ONLY, NEITHER = (
    "agree",
    "disagree",
    "ledger_only",
    "trailer_only",
    "neither",
)
OUTCOMES = (AGREE, DISAGREE, LEDGER_ONLY, TRAILER_ONLY, NEITHER)

DISAGREEMENT_CAP = 20


class ShaJoin(NamedTuple):
    sha: str
    ledger_deliverable: Optional[str]
    trailer_deliverable: Optional[str]
    outcome: str


class DivergenceReport(NamedTuple):
    counts: Dict[str, int]
    disagreements: List[ShaJoin]
    commits_examined: int
    basis: str


def _outcome(ledger: Optional[str], trailer: Optional[str]) -> str:
    if ledger is not None and trailer is not None:
        return AGREE if ledger == trailer else DISAGREE
    if ledger is not None:
        return LEDGER_ONLY
    if trailer is not None:
        return TRAILER_ONLY
    return NEITHER


def _basis(examined: int, counts: Dict[str, int]) -> str:
    agree = counts[AGREE]
    disagree = counts[DISAGREE]
    overlap = agree + disagree
    ledger_cov = overlap + counts[LEDGER_ONLY]
    trailer_cov = overlap + counts[TRAILER_ONLY]
    head = (
        f"{examined} commits examined; the ledger join covered {ledger_cov}, "
        f"the Deliverable-Id trailer join covered {trailer_cov}."
    )
    if overlap == 0:
        return (
            f"{head} No commit carries both joins, so nothing about "
            "agreement is known."
        )
    return f"{head} Of the {overlap} commits carrying both, {agree} agree and {disagree} disagree."


def compare(
    shas: Sequence[str],
    ledger_by_sha: Mapping[str, str],
    trailer_by_sha: Mapping[str, str],
) -> DivergenceReport:
    """Assign each sha in `shas` one outcome; `disagreements` keeps the first
    DISAGREEMENT_CAP disagree rows in `shas` order."""
    counts: Dict[str, int] = {key: 0 for key in OUTCOMES}
    disagreements: List[ShaJoin] = []
    for sha in shas:
        ledger = ledger_by_sha.get(sha)
        trailer = trailer_by_sha.get(sha)
        outcome = _outcome(ledger, trailer)
        counts[outcome] += 1
        if outcome == DISAGREE and len(disagreements) < DISAGREEMENT_CAP:
            disagreements.append(ShaJoin(sha, ledger, trailer, outcome))
    examined = len(shas)
    return DivergenceReport(counts, disagreements, examined, _basis(examined, counts))
