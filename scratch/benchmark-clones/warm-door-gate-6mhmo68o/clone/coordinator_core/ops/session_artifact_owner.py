"""
coordinator_core.ops.session_artifact_owner — JSON-RPC "session.artifact_owner".

Purpose: expose `coordinator_core.session.artifact_owner.resolve_artifact_owner`
through the op registry, mirroring `coordinator_core.ops.session_resolve_address`'s
own registration convention — "who's on this?", keyed on an artifact path,
alongside the UUID-keyed resolver and the repo-keyed roster.

Self-registration: importing this module calls
register_op("session.artifact_owner", _session_artifact_owner) as a
side-effect. Registered in `coordinator_core/ops/__init__.py`'s
`_EAGER_OP_MODULES`, `coordinator_core/ops/_registry_map.py`'s
`OP_MODULE_MAP`, `coordinator_core/op_scopes.py` (scope "none" — this op's
own `artifact_path` param is caller-supplied, same story as
`session.peer_roster`'s `repo_root` param), and
`coordinator_core/authz/classification.py` (`OpClass.COMPUTE_ONLY`).

Spec backlink: `state/handoffs/2026-08-13-live-peer-roster.md`
§ "What this covers" amendment (L52-62).

Negative-spec:
    - Never falls back to a guessed owner or a collapsed boolean on any
      failure path — `resolve_artifact_owner` already degrades to
      `owners=[]` (with `file_error` set) on any read/parse failure per its
      own docstring; this veneer adds no second try/except.
    - Each owner's `outcome` is surfaced verbatim (`own_session` /
      `reachable` / `not_reachable` / `ambiguous`) — never renamed,
      flattened, or summarized into a boolean. A `not_reachable` entry in
      the response is "recorded owner, not currently reachable," never
      "unowned" (module docstring negative-spec on
      `coordinator_core.session.artifact_owner`).
    - It is a read only — no `SendMessage`, no scheduling, no work
      assignment (handoff Anti-scope: "It is a read, never work
      assignment").
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.ipc import register_op
from coordinator_core.session import artifact_owner


def _owner_resolution_to_dict(resolution: "artifact_owner.OwnerResolution") -> Dict[str, Any]:
    result = resolution.result
    return {
        "session_id": resolution.owner.session_id,
        "source_field": resolution.owner.source_field,
        "outcome": result.outcome,
        "resolved_session_id": result.session_id,
        "address": result.address,
        # Review: coordinator:code-reviewer -- AC2's claim_live/claim_stage
        # were computed in the dataclass but dropped at this JSON-RPC
        # boundary; only `source_field == "claim_dir"` populates either
        # (every other convention names no claim dir of its own to ask).
        "claim_live": resolution.owner.claim_live,
        "claim_stage": resolution.owner.claim_stage,
        "candidates": [
            {
                "session_id": c.session_id,
                "name": c.name,
                "ref": c.ref,
                "address": c.address,
            }
            for c in result.candidates
        ],
    }


@register_op("session.artifact_owner")
def _session_artifact_owner(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "session.artifact_owner" handler.

    Params:
        artifact_path (str, required) -- path to the artifact to inspect
            (e.g. a `state/handoffs/*.md` file). Used verbatim as given —
            no path resolution, no repo-root prefixing.

    Returns:
        {"artifact_path": str,
         "owners": [{"session_id", "source_field", "outcome",
                      "resolved_session_id", "address", "claim_live",
                      "claim_stage", "candidates"},
                     ...],
         "file_error": str|None}

    `claim_live`/`claim_stage` are populated ONLY for an owner whose
    `source_field == "claim_dir"` — the claim dir's own liveness verdict and
    stage (`brief`/`apply`), distinct from and never collapsed into the
    `outcome` field on the same owner. Every other convention
    (`claimed_by`, `authoring_session`, `created_by_session`,
    `agent_sessions`, `subagent_share_dir`) leaves both `null`: it names no
    claim dir of its own to ask.

    `owners` can be non-empty even when `file_error` is set (AC5): a
    `claim_dir` owner is keyed on the artifact's basename alone and resolves
    independently of a successful file read, so a missing/unreadable
    artifact with a live claim still reports that owner alongside
    `file_error`. `owners == []` with `file_error == None` is the distinct
    "read fine, no owner recorded" outcome; the two empty-looking cases are
    told apart only via `file_error`, never conflated. A missing/non-string
    `artifact_path` param degrades to the same `owners=[],
    file_error=<message>` shape rather than raising — this op sits on
    advisory read paths, matching `session.resolve_address` and
    `session.peer_roster`'s own degrade-not-raise discipline.
    """
    raw_path = params.get("artifact_path") if isinstance(params, dict) else None
    if not isinstance(raw_path, str) or not raw_path:
        return {
            "artifact_path": raw_path if isinstance(raw_path, str) else "",
            "owners": [],
            "file_error": "artifact_path param is required and must be a non-empty string",
        }

    result = artifact_owner.resolve_artifact_owner(raw_path)

    return {
        "artifact_path": result.artifact_path,
        "owners": [_owner_resolution_to_dict(o) for o in result.owners],
        "file_error": result.file_error,
    }
