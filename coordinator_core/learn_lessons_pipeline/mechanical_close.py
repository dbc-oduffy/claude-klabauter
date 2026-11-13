"""coordinator_core.learn_lessons_pipeline.mechanical_close -- contract for closing
admitted lesson rows with no LLM (exact-duplicate dedupe, already-covered discard).

`close_candidates` is read-only: it classifies, it never writes a ledger, archive or
claim file. The closing write belongs to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

Verdict = Literal["dedupe", "discard"]


@dataclass(frozen=True)
class MechanicalVerdict:
    """One row closable without an LLM.

    `covered_by` is the repo-relative path of the surviving row or covering file;
    `reason` is the one-line paper-trail text written with the closure.
    """

    row_id: str
    verdict: Verdict
    covered_by: str
    reason: str


def close_candidates(manifest: Any, repo_root: Path) -> list[MechanicalVerdict]:
    """Return a verdict for every manifest entry that can close mechanically.

    Contract stub: returns [] until the deterministic rules land.
    """
    return []
