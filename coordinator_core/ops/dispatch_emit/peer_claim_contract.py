"""Path-level contract for a peer-held path at terminal commit.

A path a live peer holds lands whole or not at all; there is no hunk carry. A path the committing
run and an unlanded sibling both declare (same session included) is peer-held under this contract,
and the land_run gate refuses it as peer-hunks-co-declared. The functions read the
``{path: [sid]}`` contention map the gate already computed: no spawn, no claim read.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

CONTRACT = "path-level"
ROUTES = ("withhold", "peer-commits", "release")

WITHHOLD_CALL = "workflow.revise run_id={run_id} withhold_paths=[{{path, reason}}], then re-fire."
_PEER_COMMITS_CALL = "Wait for the peer to finish, or have it commit the path, then re-fire."
_RELEASE_CALL = "session-claim-cli release-artifact artifact <path>"


def group(holds: Iterable[tuple[str, str]]) -> list[dict]:
    """``[{"session", "paths"}]`` from ``(path, session)`` holds, sorted by session, paths sorted."""
    by_session: dict[str, set[str]] = {}
    for path, sid in holds:
        by_session.setdefault(sid, set()).add(path)
    return [{"session": sid, "paths": sorted(by_session[sid])} for sid in sorted(by_session)]


def refusal(holds: Iterable[tuple[str, str]], run_id: str) -> tuple[str, dict]:
    """The ``peer-claim`` refusal message and its reply fields for ``(path, session)`` holds."""
    pairs = list(holds)
    ordered = sorted(set(pairs))
    held = "; ".join(f"{p} held by {s}" for p, s in ordered)
    withhold = WITHHOLD_CALL.format(run_id=run_id)
    message = (
        f"{held}. {CONTRACT}: a peer-held path lands whole or not at all. "
        f"{_PEER_COMMITS_CALL} The holder frees a touch claim with `{_RELEASE_CALL}`. "
        f"Or leave them out: {withhold}"
    )
    fields = {
        "peer_claims": [{"path": p, "session": s} for p, s in pairs],
        "peer_claims_by_session": group(pairs),
        "peer_claim_contract": CONTRACT,
        "peer_claim_routes": [
            {"route": "withhold", "call": withhold},
            {"route": "peer-commits", "call": _PEER_COMMITS_CALL},
            {"route": "release", "call": _RELEASE_CALL},
        ],
    }
    return message, fields


def left_out(contested: Mapping[str, Sequence[str]]) -> list[dict]:
    """``[{"path", "sessions"}]`` for run-touched paths a peer's claim kept out, sorted by path."""
    return [{"path": p, "sessions": sorted(contested[p])} for p in sorted(contested)]
