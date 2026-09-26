
from __future__ import annotations

import glob
import json
import logging
import os

from coordinator_core.ops.emit.context import EmitContext

_LOG = logging.getLogger(__name__)

_VALID_SESSION_TYPES = {"session", "workstream", "blitz"}
_VALID_PROV_COMPLETENESS = {"complete", "unknown", None}
_VALID_HIER_COMPLETENESS = {"complete", "partial", "unknown", None}


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    valid: list[dict] = []
    malformed: list[dict] = []
    admitted_session_ids: set[str] = set()

    state_dir = str(ctx.central_state_root)
    # Bash guards `[[ -d "$_SES_HIER_DIR" ]]`; an absent dir yields no glob matches, so the
    if os.path.isdir(state_dir):
        try:
            with os.scandir(state_dir) as it:
                next(iter(it), None)
        except OSError as exc:
            _LOG.warning(
                "session_hierarchy: cannot scan central_state_root %s — %s; routing to "
                "malformed bucket instead of the graceful-absent ([], []) shape",
                state_dir,
                exc,
            )
            malformed.append({
                "path": f"state/{os.path.basename(state_dir) or state_dir}",
                "reason": f"state directory unreadable: {exc}",
            })
            return valid, malformed

    pattern = os.path.join(state_dir, "session-hierarchy.*.json")
    for fpath in sorted(glob.glob(pattern)):
        fname = os.path.basename(fpath)
        rel_path = f"state/{fname}"
        try:
            with open(fpath, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError, ValueError):
            malformed.append({"path": rel_path, "reason": "json parse error"})
            continue

        if isinstance(data, dict):
            entries = [data]
        elif isinstance(data, list):
            entries = data
        else:
            malformed.append({"path": rel_path, "reason": "expected object or array"})
            continue

        for entry in entries:
            if not isinstance(entry, dict):
                malformed.append({"path": rel_path, "reason": "entry not a JSON object"})
                continue
            session_id = entry.get("session_id")
            if not session_id:
                malformed.append({"path": rel_path, "reason": "missing session_id"})
                continue
            if entry.get("session_type") not in _VALID_SESSION_TYPES:
                malformed.append({
                    "path": rel_path, "session_id": session_id,
                    "reason": "session_type missing or outside enum",
                })
                continue
            workstream = entry.get("workstream")
            if not isinstance(workstream, str) or not workstream:
                malformed.append({
                    "path": rel_path, "session_id": session_id,
                    "reason": "workstream missing",
                })
                continue

            sys_block = entry.get("system") or {}
            prov_completeness = sys_block.get("provenance_completeness", None)
            hier_completeness = sys_block.get("completeness", None)

            if prov_completeness not in _VALID_PROV_COMPLETENESS:
                malformed.append({
                    "path": rel_path, "session_id": session_id,
                    "reason": f"provenance_completeness outside enum: {prov_completeness}",
                })
                continue
            if hier_completeness not in _VALID_HIER_COMPLETENESS:
                malformed.append({
                    "path": rel_path, "session_id": session_id,
                    "reason": f"completeness outside enum: {hier_completeness}",
                })
                continue

            if session_id in admitted_session_ids:
                malformed.append({
                    "path": rel_path, "session_id": session_id,
                    "reason": "duplicate session_id — session_id must be unique within "
                              "session_hierarchies",
                })
                continue
            admitted_session_ids.add(session_id)

            valid.append({
                "repo": ctx.repo_name,
                "coordinator_root_path": ".",
                "session_id": session_id,
                "session_type": entry["session_type"],
                "workstream": workstream,
                "branch": entry.get("branch", None),
                "parent_session_id": entry.get("parent_session_id", None),
                "linked_handoffs": entry.get("linked_handoffs", None),
                "created_by_session": sys_block.get("created_by_session", None),
                "provenance_completeness": prov_completeness,
                "capture_source": sys_block.get("capture_source", None),
                "completeness": hier_completeness,
                "provenance": ctx.provenance("coordinator_artifact", path=rel_path, derivation="parsed"),
            })

    return valid, malformed
