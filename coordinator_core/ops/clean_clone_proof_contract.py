"""Wave-0 contract of the clean-clone proof: verdict shape, verdict path, red-at-sha criterion.

Pure constants and a frozen dataclass, no I/O. The runner (`RUNNER_MODULE`) writes the verdict and
amends the close job; the close-background leg only spawns that module, so both build against this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from coordinator_core.bug_blitz.contract import (
    OUTCOME_FAILED,
    OUTCOME_NO_RUNNER,
    OUTCOME_NOTHING_TO_RUN,
    OUTCOME_PASSED,
)

REASON_RED_AT_SHA = "red-at-sha"
CRITERION_STATUS_NOT_MET = "not_met"

VERDICT_PATH_TEMPLATE = "state/warp/clean-clone-proofs/{sha}.json"
RUNNER_MODULE = "coordinator_core.ops.clean_clone_proof"

VERDICT_OUTCOMES = (
    OUTCOME_PASSED,
    OUTCOME_FAILED,
    OUTCOME_NOTHING_TO_RUN,
    OUTCOME_NO_RUNNER,
)


@dataclass(frozen=True)
class CleanCloneVerdict:
    """What the tests covering `run_base_sha..sha` did in a clean clone at `sha`.

    `masked_paths` are the shared-tree uncommitted paths the clone lacked.
    """

    sha: str
    run_id: str
    run_base_sha: str
    selected: Tuple[str, ...]
    outcome: str
    failing_ids: Tuple[str, ...] = ()
    masked_paths: Tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {
            "sha": self.sha,
            "run_id": self.run_id,
            "run_base_sha": self.run_base_sha,
            "selected": list(self.selected),
            "outcome": self.outcome,
            "failing_ids": list(self.failing_ids),
            "masked_paths": list(self.masked_paths),
        }


def verdict_path(sha: str) -> str:
    """Repo-relative path of the verdict file for `sha`."""
    return VERDICT_PATH_TEMPLATE.format(sha=sha)


def criterion_for(verdict: CleanCloneVerdict) -> Optional[dict]:
    """The close-job criterion for a failed verdict; None for every other outcome."""
    if verdict.outcome != OUTCOME_FAILED:
        return None
    return {
        "status": CRITERION_STATUS_NOT_MET,
        "reason": REASON_RED_AT_SHA,
        "sha": verdict.sha,
        "failing": list(verdict.failing_ids),
        "masked_paths": list(verdict.masked_paths),
    }
