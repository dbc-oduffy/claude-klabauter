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


def _is_test_path(path: str) -> bool:
    parts = _norm(path).split("/")
    name = parts[-1]
    return (
        any(p in ("tests", "test", "__tests__") for p in parts[:-1])
        or name.startswith("test_")
        or name.endswith(("_test.py", ".test.ts", ".test.tsx", ".test.js", ".spec.ts", ".spec.js"))
    )


def tests_of_withheld_code(
    root: Path, message: str, paths: Sequence[str], withheld: Sequence[str],
) -> list[str]:
    """The `paths` that are test files of a checkpoint row whose non-test write is in `withheld`:
    a row lands whole or its tests wait with its withheld code. Rows come from the message subject,
    their `writes` from the `Checkpoint-Plan:` spine. Zero git spawns; an unreadable plan returns
    `[]`. Never raises."""
    try:
        from coordinator_core.git.checkpoint_guard import checkpoint_row_ids
        from coordinator_core.ops.dispatch_emit.spine_read import load_rows_memo

        plan, ids = _message_plan(message), set(checkpoint_row_ids((message or "").split("\n", 1)[0]))
        if not plan or not ids or not withheld:
            return []
        rows = getattr(load_rows_memo((Path(root) / plan).read_text(encoding="utf-8")), "rows", None) or []
        held, present = {_norm(p) for p in withheld}, {_norm(p) for p in paths}
        extra: set[str] = set()
        for row in rows:
            writes = row.get("writes") if isinstance(row, dict) else None
            if str(row.get("id") if isinstance(row, dict) else "") not in ids or not isinstance(writes, list):
                continue
            norm = [_norm(w) for w in writes if isinstance(w, str)]
            if any(w in held and not _is_test_path(w) for w in norm):
                extra.update(w for w in norm if _is_test_path(w) and w in present and w not in held)
        return sorted(extra)
    except Exception:  # noqa: BLE001 -- unreadable plan: nothing extra is withheld
        return []


NO_PRINT_REASON = "no write-time print of this run: the run's own write of this path was not recorded"


def _hunk_label(hunk: object) -> str:
    return f"@@ -{hunk.old_start},{hunk.old_len} @@"  # type: ignore[attr-defined]


def _nonrun_reason(nonrun: Sequence[object]) -> str:
    labels = [f"{_hunk_label(h)} (line {h.old_start})" for h in nonrun]  # type: ignore[attr-defined]
    return "hunk a non-run edit wrote after this run's last recorded write: " + ", ".join(labels)


def _withheld_reason(split: object, hunks: Sequence[object]) -> str:
    overlapping = getattr(split, "overlapping", None) or {}
    unattributed = getattr(split, "unattributed", None) or []
    if overlapping:
        held = [f"{h} of run {run}" for run, hs in overlapping.items() for h in map(_hunk_label, hs)]
        return "hunk overlaps a peer run's: " + ", ".join(held)
    if unattributed:
        return "hunk no run baseline attributes to this run: " + ", ".join(map(_hunk_label, unattributed))
    nonrun = getattr(split, "nonrun", None) or []
    if nonrun:
        return _nonrun_reason(nonrun)
    return "no hunk is this run's: " + ", ".join(map(_hunk_label, hunks))


def checkpoint_own_blobs(
    root: Path, run_id: Optional[str], paths: Sequence[str], message: str = "",
    reasons: Optional[dict[str, str]] = None,
) -> tuple[dict[str, str], list[str]]:
    """`(supplied_blobs, withheld)`: `{path: blob sha}` for each tracked path whose worktree carries
    foreign hunks beside the run's own (the blob is HEAD plus the own hunks only), and the paths
    held whole off the commit. A path with no diff, absent from HEAD, or wholly the run's own
    appears in neither. A diffed path the run has no write-time print for (`run_write_prints`) is
    withheld whole and named: its hunks cannot be told from a peer's. `message` supplies the
    `Checkpoint-Plan:` value that scopes the peer-hold read. A `reasons` dict, when given, receives
    `{path: why}` for each withheld path, naming the foreign hunk where one is known. Never raises;
    unknown (no run, unreadable diff, unattributed or overlapping hunk) withholds."""
    root = Path(root)
    why = reasons if reasons is not None else {}
    wanted = [_norm(p) for p in paths]
    blobs: dict[str, str] = {}
    withheld: list[str] = []
    try:
        from coordinator_core.ops.dispatch_emit import (
            own_hunk_stage, peer_hunk_gate, run_hunk_claims, run_write_prints,
        )

        try:
            diffs = own_hunk_stage.read_diff(root, wanted)
        except own_hunk_stage.OwnHunkRefused:
            why.update({p: "diff unreadable" for p in wanted})
            return {}, sorted(set(wanted))
        pending = [p for p in wanted if p in diffs and diffs[p].hunks]
        if not pending:
            return {}, []
        if not run_id:
            why.update({p: "run unresolved: no live run of this session works the Checkpoint-Plan" for p in pending})
            return {}, sorted(set(pending))
        prints = run_write_prints.read(root, run_id) or {}
        for path in [p for p in dict.fromkeys(pending) if p not in prints]:
            withheld.append(path)
            why[path] = NO_PRINT_REASON
        pending = [p for p in pending if p in prints]
        if not pending:
            return {}, sorted(set(withheld))
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
            if split.overlapping or split.unattributed or not split.own:
                withheld.append(path)
                why[path] = _withheld_reason(split, hunks)
            elif len(split.own) != len(hunks):
                if split.nonrun:
                    why[path] = _nonrun_reason(split.nonrun)
                blobs[path] = own_hunk_stage.own_blob(root, path, diff.head_bytes, split.own)
    except Exception:  # noqa: BLE001 -- unknown holds
        return {}, sorted(set(wanted))
    return blobs, sorted(set(withheld))
