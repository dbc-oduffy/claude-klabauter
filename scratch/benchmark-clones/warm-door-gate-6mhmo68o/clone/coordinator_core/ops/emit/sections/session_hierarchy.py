"""Section porter — SessionHierarchy (envelope key: ``session_hierarchies``).

Emits one SessionHierarchy record per entry found in
``$(coordinator_state_root --central)/session-hierarchy.*.json``. Each source file may
hold a single JSON object or a JSON array of entries; the ``system`` sub-block is flattened
to top-level for emission consistency. Entries missing a ``session_id``, with a
``session_type`` outside the enum, a non-string ``workstream``, or a
``provenance_completeness`` / ``completeness`` value outside its enum are quarantined; a
file that fails to parse or is not an object/array is quarantined as a whole. Graceful
absent — no matching files → ``([], [])``, never a failure.

``session_id`` is the natural key of ``session_hierarchies`` and MUST be unique within a
single emission (ratified contract answer, 2026-07-26 — see
``cross-repo/inbox/2026-07-26-example-cockpit-repo-em-claude-klabauter-duplicate-session-hierarchy-entries.md``,
reporting two downstream consumers silently keeping different rows for one duplicated
``session_id`` decided purely by iteration order). The uniqueness check runs across ALL
source files in one ``collect()`` call — it does not reset per file — and fires only on an
entry that has already cleared every other validation gate above, so a duplicate that would
also fail e.g. the ``session_type`` enum is quarantined for that pre-existing reason, never
shadowed by the duplicate check. The first-admitted entry wins (deterministic: files are
walked in ``sorted(glob.glob(...))`` order, entries in file order — no new sort or tiebreak
is introduced for this). Every subsequent entry sharing that ``session_id`` is quarantined
into ``malformed`` rather than silently dropped.

Unreadable state dir vs. genuinely absent (state/audits/2026-07-22 silent-success audit):
``glob.glob()``'s selector silently swallows ``PermissionError`` while walking (an
unreadable dir yields an empty match list, no exception) — so a permission-denied
``central_state_root`` would otherwise be indistinguishable from "no session-hierarchy
files exist here" and wrongly collapse to the same graceful-absent ``([], [])`` shape.
``central_state_root`` is probed via ``os.scandir`` before trusting the glob; a scan
failure is routed into the malformed bucket with a ``"state directory unreadable"``
reason (this section's existing malformed-quarantine channel), never the graceful-absent
shape.

Port of: emit-cockpit-snapshot.sh (DoE 07eedcfb, 2026-07-19) § SECTION 8.13 —
  SessionHierarchy (the embedded python3 heredoc). Byte/semantic parity port —
  the heredoc's glob, flatten, enum-quarantine, and record shape are reproduced
  in-process here.
Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § P16
"""

from __future__ import annotations

import glob
import json
import logging
import os

from coordinator_core.ops.emit.context import EmitContext

_LOG = logging.getLogger(__name__)

# Enum gates (bash:2211-2213). ``None`` is admitted for the two completeness enums because
# the flattened system block may legitimately omit them (absent → null passes through).
_VALID_SESSION_TYPES = {"session", "workstream", "blitz"}
_VALID_PROV_COMPLETENESS = {"complete", "unknown", None}
_VALID_HIER_COMPLETENESS = {"complete", "partial", "unknown", None}


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    """Build SessionHierarchy records + quarantine list (parity: bash SECTION 8.13 heredoc)."""
    valid: list[dict] = []
    malformed: list[dict] = []
    admitted_session_ids: set[str] = set()

    state_dir = str(ctx.central_state_root)
    # Bash guards `[[ -d "$_SES_HIER_DIR" ]]`; an absent dir yields no glob matches, so the
    # loop below simply produces empty arrays — same graceful-absent outcome.
    #
    # NOTE: probes state_dir via os.scandir before trusting glob.glob() — glob's selector
    # silently swallows PermissionError while walking (an unreadable dir yields an empty
    # match list, no exception), which would otherwise be indistinguishable from "no
    # session-hierarchy files exist here" and wrongly return the graceful-absent ([], [])
    # shape for what is actually a scan failure.
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
            # Review: code-reviewer — this is the one malformed-entry shape in this file
            # that never carries session_id (by construction: it fails before any file is
            # even opened, so no session_id has been read yet). Every other malformed
            # entry below carries a session_id when one is known — a consumer scanning
            # malformed_records.session_hierarchies for a uniform shape should not assume
            # session_id is always present.
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

        # Accept both array-of-records and single-record formats (bash:2227-2234).
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

            # Flatten system block (bash:2254-2257).
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
                # Flattened from system block.
                "created_by_session": sys_block.get("created_by_session", None),
                "provenance_completeness": prov_completeness,
                "capture_source": sys_block.get("capture_source", None),
                "completeness": hier_completeness,
                "provenance": ctx.provenance("coordinator_artifact", path=rel_path, derivation="parsed"),
            })

    return valid, malformed
