"""Caller-facing contract of ``dispatch.land_credited``: op name, request, reply keys, requirement ids, holds hook.

Imports nothing beyond the stdlib so a caller reads the contract without loading the op module.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Final, Mapping

OP_NAME: Final = "dispatch.land_credited"

#: Requirement ids the old ``dispatch.land_run`` discharged, as listed in the rebuild plan's
#: "Requirements the old op discharges"; each is settled kept or dropped in the research doc.
REQUIREMENT_IDS: Final = ("a", "b", "c", "d", "e", "f", "g", "h")

#: Consumer: the contract and research-doc pin tests only. Id -> one-line name; ids ``a`` and ``h`` are the minimum land and are always kept.
REQUIREMENT_NAMES: Final = {
    "a": "credited rows' declared writes differing from HEAD",
    "b": "run_artifacts candidate paths",
    "c": "superseded predecessors' record dirs",
    "d": "reviewed undeclared edits (clause iv)",
    "e": "peer-held / never-commit / sibling-repo / EM-withheld holds named in withheld",
    "f": "halted-run holds",
    "g": "reach gate",
    "h": "commit via git.commit.commit_paths (zero spawns, CAS ref move)",
}

#: Requirements that are the minimum land and cannot be dropped.
MINIMUM_LAND_IDS: Final = ("a", "h")

#: ``refused`` values of a reply that landed nothing.
REFUSED_BAD_REQUEST: Final = "bad-request"
REFUSED_NOTHING_TO_LAND: Final = "nothing-to-land"
REFUSED_COMMIT: Final = "commit-refused"
REFUSED_LAND_DEADLINE: Final = "land-deadline"

#: Keys a caller reads from a reply.
REPLY_KEYS: Final = frozenset({
    "committed",
    "sha",
    "landed",
    "withheld",
    "refused",
    "error",
    "leg",
    "leg_timings",
    "leg_record",
})

#: ``holds(root, request) -> {path: reason}``: paths the landing leaves out and the reason each is withheld.
#: Provenance, not a blocker: a held path is named in ``withheld`` and never refuses the landing.
HoldsHook = Callable[[Path, "LandCreditedRequest"], dict[str, str]]


@dataclass(frozen=True)
class LandCreditedRequest:
    """Params of one call: the run to land, its plan, and the script whose credited rows name the paths."""

    run_id: str
    #: Read by the completion-receipt writer (``completion_receipt.build_run_receipts``) after the commit.
    plan_path: str
    script_path: str
    #: Read by ``build_run_receipts``; ``None`` = the run names no deliverable.
    deliverable_id: str | None = None


def parse_request(params: object) -> LandCreditedRequest | str:
    """The request in ``params``, else a message naming the first missing or non-string field."""
    if not isinstance(params, Mapping):
        return "params must be an object"
    values = {}
    for field in ("run_id", "plan_path", "script_path"):
        value = params.get(field)
        if not isinstance(value, str) or not value:
            return f"{field} must be a non-empty string"
        values[field] = value
    return LandCreditedRequest(**values)


def landed_reply(sha: str, landed: list[str], withheld: Mapping[str, str]) -> dict:
    """Reply of a commit that moved the ref."""
    return {"committed": True, "sha": sha, "landed": sorted(landed), "withheld": dict(withheld)}


def refused_reply(refused: str, error: str, withheld: Mapping[str, str] | None = None) -> dict:
    """Reply of a call that landed nothing."""
    return {
        "committed": False,
        "refused": refused,
        "error": error,
        "landed": [],
        "withheld": dict(withheld or {}),
    }
