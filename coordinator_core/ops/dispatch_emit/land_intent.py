"""The durable land record of a keyed `dispatch.land_run` / `dispatch.terminal_commit` call.

The pool worker writes it at run-token claim-win and advances it across the commit
("claimed" -> "committing" -> "committed"+sha); the accept process's `AckStore` reads it
when the worker died and resolves it through `resolve`, so a reconcile poll answers
committed or never-ran rather than unknowable. Write and advance are one `os.replace`
each and never raise; cold callers (no dispatch key) write nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

PHASE_CLAIMED = "claimed"
PHASE_COMMITTING = "committing"
PHASE_COMMITTED = "committed"

# The record this worker process is advancing: (journal_dir, dispatch_key, record).
# One keyed op runs per pool worker at a time; `record` always replaces it.
_held: Optional[tuple] = None


def record(root: Path, params: dict, *, script_path: object = None, kind: str = "land_run") -> bool:
    """Write the "claimed" record for the dispatch this process runs; True when written.
    A no-op outside a keyed dispatch. Call only from the call that won the claim."""
    global _held
    _held = None
    from coordinator_core.warm import dispatch_ack, dispatch_fence

    held = dispatch_fence.current_dispatch()
    if held is None:
        return False
    claim_dir, key = held
    journal = dispatch_ack.journal_dir_beside(claim_dir)
    body = {
        "kind": kind,
        "repo_root": str(root),
        "run_id": params.get("run_id"),
        "token": params.get("run_token"),
        "script_path": script_path if script_path is not None else params.get("script_path"),
        "phase": PHASE_CLAIMED,
    }
    if not journal or not dispatch_ack.record_land_intent(journal, key, body):
        return False
    _held = (journal, key, body)
    return True


def mark(phase: str, sha: Optional[str] = None) -> None:
    """Advance this process's land record to `phase` (with `sha` once committed). A no-op
    when `record` wrote nothing; never raises."""
    global _held
    held = _held
    if held is None:
        return
    journal, key, body = held
    from coordinator_core.warm import dispatch_ack, dispatch_fence

    current = dispatch_fence.current_dispatch()
    if current is None or current[1] != key:
        _held = None  # a record from an earlier op in this worker never advances under another key
        return
    advanced = {**body, "phase": phase}
    if sha:
        advanced["sha"] = sha
    if dispatch_ack.record_land_intent(journal, key, advanced):
        _held = (journal, key, advanced)


def resolve(land_record: dict) -> Optional[dict]:
    """The accept process's verdict on a dead worker's record: `{"verdict": "committed", "sha"}`
    when the receipt or the record's sha is on HEAD, `{"verdict": "never-ran"}` only while the
    phase is "claimed" (no commit spawned), else None (unresolved: no re-run license)."""
    from coordinator_core.ops.dispatch_emit import land_run

    if not land_record.get("repo_root"):
        return None
    root = Path(str(land_record["repo_root"]))
    landed = land_run.already_landed_reply(
        root, land_record.get("run_id"), land_record.get("token"), land_record.get("script_path")
    )
    if landed is not None:
        return {"verdict": "committed", "sha": landed["sha"]}
    sha = land_record.get("sha")
    if isinstance(sha, str) and sha and land_run.landed_on_head(root, sha):
        return {"verdict": "committed", "sha": sha}
    if land_record.get("phase") == PHASE_CLAIMED:
        return {"verdict": "never-ran"}
    return None
