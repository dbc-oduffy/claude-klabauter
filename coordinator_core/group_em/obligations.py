"""Reader and intake appender over the DoE-owned next-move ledger (roadmap
`gem-01`, baton `gem-14`, chunk C1).

PURPOSE. `send_pass.undischarged_obligations` already reads the ledger
DoE-claude writes (`state/subagent-share/<sid>/next-move-ledger.jsonl`) and
returns a *count*. What the standing watch (chunk C2) needs is the *names*
behind that count -- `for_peer` below returns the rows themselves, not a
scalar. And the ledger has almost nothing to count: measured before this
chunk, 183/1579 share dirs and 2/26 live sessions carried a ledger at all,
because nothing on this plane ever wrote to it.

**This module does not write the ledger.** `next-move-ledger.jsonl` is
DoE-claude's, and it is read-modify-whole-file-rewrite (`_write_records` in
`coordinator/hooks/scripts/_next_move_ledger.py`, resolved via
`repos.doe_claude`, never a hardcoded drive path) -- atomic for
one writer, lossy for two. An earlier revision of this chunk had claude-klabauter
append to it directly, on the mistaken assurance that session directories
are "disjoint by construction"; that assurance was about directories, not
about two planes racing to rewrite the SAME peer's file on the SAME `Stop`
event, and the director review caught it before it shipped. See the C1
dispatch brief for the full account -- recorded there rather than quietly
corrected, because the shape of the error is the lesson.

Instead this module appends to a file only this plane writes --
`state/subagent-share/<session-id>/obligations-inbound.jsonl` -- which DoE's
own drain claims, folds, and deletes. One writer per file, one rewrite path,
no shared file, no lock. Contract, producer-facing:
`coordinator/docs/wiki/obligations-inbound-intake.md` in `repos.doe_claude`
(landed sha `764e6c198` on DoE's `work/machine-a/2026-08-22to31`). Built
against that page, not against their module -- the module is the consumer's
implementation, not the contract.

Row shape (`record` below): `schema` (must be 1), `session_id` (must equal
the directory the file sits in), `op` (`open`/`progress`/`blocked`/
`discharge`), `obligation_id`, `emitted_at` (provenance only, never used for
ordering), `seam`+`next_action` (required for `op=open`),
`blocked_on_session_id`+optional `blocked_on_name` (required/optional for
`op=blocked`), and `producer` -- optional on their side, MANDATORY here: an
unattributed row in a two-producer file is exactly the archaeology this
field exists to prevent.

Negative-spec, load-bearing: `for_peer` preserves the `None`-vs-`[]`
distinction `undischarged_obligations` already draws. `None` means no ledger
file exists at all -- a producer coverage gap, never evidence the peer owes
nothing. `[]` means a ledger exists and every record in it is
discharged/fired. Closing the coverage gap must not make absence
unrepresentable; a change that makes every peer read `[]` (or `0`) would
re-create the original bug inverted.

Spec backlink: docs/plans/2026-08-31-the-group-em-tick-carries-standing-obligations.md
chunk C1.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from coordinator_core.session import subagent_share

#: Valid intake ops, mirroring DoE's own closed `_INTAKE_OPS` vocabulary at
#: the landed sha. Kept as our own tuple rather than importing theirs --
#: this plane conforms to the wiki contract, not to a cross-repo import.
_INTAKE_OPS = ("open", "progress", "blocked", "discharge")

_INTAKE_SCHEMA = 1

# The filenames and the share-directory join live in `session.subagent_share`.
# This module used to retype both AND reach into `send_pass`'s private
# namespace for the join -- one string typo apart from reading a different
# file than the module writing it.
#
# Review: overengineering-reviewer (finding #2, minor, accepted) -- call
# sites below now name `subagent_share.<name>` directly rather than rebinding
# aliases, which restored the private-looking-but-foreign symbol the
# consolidation existed to remove.


def for_peer(repo_root: str, session_id: str) -> Optional[list[dict[str, Any]]]:
    """This peer's open, unfired obligation records, or `None` with no ledger.

    Mirrors `send_pass.undischarged_obligations`'s own read exactly (same
    path, same "discharged_at is None and not fired" predicate, same
    malformed-line-skips-not-crashes degrade) but returns the records
    themselves rather than a count -- the NAMES behind the count, which
    nothing before this chunk exposed. `None` (no ledger file) and `[]` (a
    ledger that owes nothing right now) are deliberately distinct; see the
    module docstring's negative spec.
    """
    if not subagent_share.safe_session_id(session_id):
        return None
    path = subagent_share.ledger_path(repo_root, session_id)
    if not os.path.exists(path):
        return None
    records: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("discharged_at") is None and not record.get("fired"):
                    records.append(record)
    except OSError:
        return None
    return records


def _validate_row(row: dict[str, Any]) -> Optional[str]:
    """None if `row` is well-formed against the wiki contract, else a short
    reason -- checked here so this plane never appends a row its own producer
    already knows the consumer will quarantine."""
    if row.get("schema") != _INTAKE_SCHEMA:
        return "unsupported schema"
    if not isinstance(row.get("session_id"), str) or not row["session_id"]:
        return "missing session_id"
    op = row.get("op")
    if op not in _INTAKE_OPS:
        return "unknown op"
    if not isinstance(row.get("obligation_id"), str) or not row["obligation_id"]:
        return "missing obligation_id"
    if op == "open":
        for field_name in ("seam", "next_action"):
            value = row.get(field_name)
            if not isinstance(value, str) or not value:
                return "op=open missing " + field_name
    if op == "blocked":
        blocked_on = row.get("blocked_on_session_id")
        if not isinstance(blocked_on, str) or not blocked_on:
            return "op=blocked missing blocked_on_session_id"
    return None


def record(
    repo_root: str,
    session_id: str,
    op: str,
    obligation_id: str,
    *,
    seam: Optional[str] = None,
    next_action: Optional[str] = None,
    blocked_on_session_id: Optional[str] = None,
    blocked_on_name: Optional[str] = None,
    producer: str = "coordinator_core",
    now: Optional[float] = None,
) -> bool:
    """Append one intake row: what THIS session owes, written by the session
    that knows -- the sole writer of `obligations-inbound.jsonl` for its own
    `session_id`. `False` on any invalid input or write failure; never
    raises (callers on the hook path require fail-soft).

    Never writes `next-move-ledger.jsonl` -- see the module docstring. This
    is the INTAKE APPENDER, not a ledger writer: DoE's own drain claims,
    folds, and deletes what lands here.
    """
    if not subagent_share.safe_session_id(session_id):
        return False
    if not isinstance(producer, str) or not producer:
        return False
    row: dict[str, Any] = {
        "schema": _INTAKE_SCHEMA,
        "session_id": session_id,
        "op": op,
        "obligation_id": obligation_id,
        "emitted_at": _now_iso(now),
        "producer": producer,
    }
    if seam is not None:
        row["seam"] = seam
    if next_action is not None:
        row["next_action"] = next_action
    if blocked_on_session_id is not None:
        row["blocked_on_session_id"] = blocked_on_session_id
    if blocked_on_name is not None:
        row["blocked_on_name"] = blocked_on_name

    if _validate_row(row) is not None:
        return False

    path = subagent_share.intake_path(repo_root, session_id)
    line = json.dumps(row, sort_keys=True)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        return False
    return True


def _now_iso(now: Optional[float] = None) -> str:
    epoch = time.time() if now is None else now
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
