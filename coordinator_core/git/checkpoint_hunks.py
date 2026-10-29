"""Checkpoint own-hunk staging: the blobs that carry only the checkpointing run's hunks of a path.

A checkpoint commit names its plan in a `Checkpoint-Plan:` line. `checkpoint_run_id` resolves the
live run of the committing session that works that plan; `checkpoint_own_blobs` then splits each
listed path with the same attribution `land_run` uses (`run_hunk_claims.own_hunks`) and returns
`supplied_blobs` for `commit_paths`. Zero git spawns; every unknown withholds, nothing raises.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence


def _norm(path: str) -> str:
    return str(path).replace("\\", "/")


def _sizing_plan(root: Path, sizing_rel: object) -> str:
    import yaml

    if not isinstance(sizing_rel, str) or not sizing_rel:
        return ""
    try:
        sizing = yaml.safe_load((root / sizing_rel).read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, yaml.YAMLError):
        return ""
    plan = sizing.get("plan") if isinstance(sizing, dict) else None
    return _norm(plan) if isinstance(plan, str) else ""


def _message_plan(message: str) -> str:
    from coordinator_core.git.checkpoint_guard import _PLAN_RE

    m = _PLAN_RE.search(message or "")
    return m.group("v") if m else ""


def checkpoint_run_id(root: Path, message: str, session_id: Optional[str]) -> Optional[str]:
    """The run id of the one live run of `session_id` whose sizing's plan is the message's
    `Checkpoint-Plan:`; None when the message names no plan, the session is unknown, or zero or
    several runs match."""
    try:
        if not session_id:
            return None
        plan = _message_plan(message)
        if not plan:
            return None
        from coordinator_core.git.checkpoint_guard import checkpoint_plan_matches
        from coordinator_core.ops.dispatch_emit import run_token

        root = Path(root)
        found = [
            record["run_id"]
            for record in run_token.live_records(root)
            if record.get("session_id") == session_id
            and (own := _sizing_plan(root, record.get("sizing_rel")))
            and checkpoint_plan_matches(plan, own)
        ]
        return found[0] if len(found) == 1 else None
    except Exception:  # noqa: BLE001 -- unresolvable: the caller withholds
        return None


def checkpoint_own_blobs(
    root: Path, run_id: Optional[str], paths: Sequence[str], message: str = "",
) -> tuple[dict[str, str], list[str]]:
    """`(supplied_blobs, withheld)`: `{path: blob sha}` for each tracked path whose worktree carries
    foreign hunks beside the run's own (the blob is HEAD plus the own hunks only), and the paths
    held whole off the commit. A path with no diff, absent from HEAD, or wholly the run's own
    appears in neither. `message` supplies the `Checkpoint-Plan:` value that scopes the peer-hold
    read. Never raises; unknown (no run, unreadable diff, unattributed or overlapping hunk) withholds."""
    root = Path(root)
    wanted = [_norm(p) for p in paths]
    blobs: dict[str, str] = {}
    withheld: list[str] = []
    try:
        from coordinator_core.ops.dispatch_emit import own_hunk_stage, peer_hunk_gate, run_hunk_claims

        try:
            diffs = own_hunk_stage.read_diff(root, wanted)
        except own_hunk_stage.OwnHunkRefused:
            return {}, sorted(set(wanted))
        pending = [p for p in wanted if p in diffs and diffs[p].hunks]
        if not pending:
            return {}, []
        if not run_id:
            return {}, sorted(set(pending))
        pairs = peer_hunk_gate.co_declared_peer_holds(
            root, run_id, pending, pending, own_plan=_message_plan(message) or None,
        )
        holders: dict[str, dict[str, bool]] = {}
        for path, sibling in pairs:
            holders.setdefault(_norm(path), {})[sibling] = True
        for path in dict.fromkeys(pending):
            diff = diffs[path]
            hunks = list(diff.hunks)
            split = run_hunk_claims.own_hunks(root, run_id, path, hunks, holders.get(path, {}))
            if split.overlapping or split.unattributed:
                withheld.append(path)
            elif len(split.own) == len(hunks):
                continue
            elif not split.own:
                withheld.append(path)
            else:
                blobs[path] = own_hunk_stage.own_blob(root, path, diff.head_bytes, split.own)
    except Exception:  # noqa: BLE001 -- unknown holds
        return {}, sorted(set(wanted))
    return blobs, sorted(set(withheld))
