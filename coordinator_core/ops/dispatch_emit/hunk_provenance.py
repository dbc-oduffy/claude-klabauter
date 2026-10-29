"""Hunk provenance: does every hunk a landing commits for a declared path trace to this run?

`unproduced_hunks` compares the to-be-committed blob (worktree in checkin form) with HEAD and
keeps a hunk only when this run produced it: the blob equals a `kept.json` fingerprint, or the
hunk lies inside the run's `write_baseline`-to-committed delta. A hunk already in the baseline
has no producing row. Zero spawn; unknown (unreadable baseline, coarse delta) holds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

FOREIGN_HUNK_UNPRODUCED = "foreign-hunk-unproduced"
UNKNOWN_OWNER = "unknown"


def unproduced_hunks(
    root: Path,
    run_id: str,
    paths: Sequence[str],
    *,
    own_plan: Optional[str],
    advisories: Optional[list] = None,
) -> list:
    """`[landing_integrity.Finding(path, FOREIGN_HUNK_UNPRODUCED, detail)]` per path whose committed
    blob holds a hunk no row of `run_id` produced; the detail names the probable owner (first
    co-declaring sibling run, else `unknown`). Paths with no change against HEAD yield nothing.
    Never raises: a read failure is a finding, except on a path the run's own record produced
    (its write_baseline names it, or it is a run-own artifact): there the unattributable read is
    appended to `advisories` as `{"path", "detail"}` and yields no finding. DR-467: a path the run
    never baselined stays a finding. Trap: a peer write landing after this run's stage
    inside the run's own delta span is indistinguishable here."""
    from coordinator_core.ops.dispatch_emit.landing_targeted_tests import Finding

    out: list = []
    for raw in paths:
        path = str(raw or "").replace("\\", "/")
        if not path:
            continue
        try:
            bad = _unproduced(Path(root), run_id, path)
        except Exception as exc:  # noqa: BLE001 -- unknown holds
            detail = f"provenance unreadable: {type(exc).__name__}: {exc}"
            if advisories is not None and _run_produced(Path(root), run_id, path):
                advisories.append({"path": path, "detail": detail})
            else:
                out.append(Finding(path, FOREIGN_HUNK_UNPRODUCED, detail))
            continue
        if bad:
            owner = _owner(Path(root), run_id, path, own_plan)
            out.append(Finding(
                path,
                FOREIGN_HUNK_UNPRODUCED,
                f"committed lines {bad} were not produced by run {run_id}; probable owner {owner}",
            ))
    return out


def _owner(root: Path, run_id: str, path: str, own_plan: Optional[str]) -> str:
    try:
        from coordinator_core.ops.dispatch_emit import peer_hunk_gate

        holds = peer_hunk_gate.co_declared_peer_holds(root, run_id, [path], [path], own_plan=own_plan)
        for _p, holder in holds:
            return str(holder)
    except Exception:  # noqa: BLE001 -- owner naming is best effort
        pass
    return UNKNOWN_OWNER


def _is_run_own_artifact(root: Path, run_id: str, path: str) -> bool:
    """A stage, not a row, wrote the run's record dir, delivery note and sizings: no baseline exists
    for them and the run is their producer."""
    from coordinator_core.ops.dispatch_emit import run_artifact_ownership

    return run_artifact_ownership.own_artifact_paths(root, run_id).covers(path)


def _run_produced(root: Path, run_id: str, path: str) -> bool:
    """The run's own record produced `path`: its write_baseline names it, or it is a run-own artifact.
    False on any read failure (unknown is not attributable)."""
    try:
        from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha

        base = hha._baseline(root, run_id)
        if base is not None and path in base:
            return True
        return _is_run_own_artifact(root, run_id, path)
    except Exception:  # noqa: BLE001 -- unknown stays a finding
        return False


def _unproduced(root: Path, run_id: str, path: str) -> str:
    """Committed-side line ranges ("3-4, 9") of hunks no row produced; "" when all are produced."""
    from coordinator_core.git import git_objects
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_state import head_blobs
    from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha
    from coordinator_core.ops.dispatch_emit import kept_work
    from coordinator_core.ops.dispatch_emit import run_hunk_claims as rhc
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    common = resolve_git_common_dir(root)
    try:
        raw = (root / path).read_bytes()
    except FileNotFoundError:
        return ""
    form = kept_work.checkin_form(root, path, raw)
    if form is None:
        raise ValueError("worktree bytes have no checkin form")
    committed = form.splitlines(keepends=True)

    entry = head_blobs(root, [path]).get(path)
    head: list = []
    if entry is not None:
        obj = git_objects.read_object(common, entry[1])
        if obj is None or obj[0] != "blob":
            raise ValueError(f"HEAD blob {entry[1]} unreadable")
        head = obj[1].splitlines(keepends=True)
    hunks = rhc.changed_opcodes(head, committed)
    if not hunks:
        return ""

    committed_sha = kept_work.blob_sha(form)
    for kept in kept_work.kept_fingerprints(record_dir(root, run_id)).get(path, ()):
        if kept_work.matches_committed(root, path, kept, committed_sha):
            return ""

    base = hha._baseline(root, run_id)
    if base is None or path not in base:
        if _is_run_own_artifact(root, run_id, path):
            return ""
        raise ValueError("run has no write_baseline for the path")
    old: list = []
    if base[path] is not None:
        obj = git_objects.read_object(common, base[path])
        if obj is None or obj[0] != "blob":
            raise ValueError(f"baseline blob {base[path]} unreadable")
        base_form = kept_work.checkin_form(root, path, obj[1])
        if base_form is None:
            raise ValueError("baseline bytes have no checkin form")
        old = base_form.splitlines(keepends=True)
    if rhc.exceeds_match_bound(old, committed):
        raise ValueError("baseline delta is too coarse to attribute")
    delta = rhc._delta_spans(old, committed)

    ranges: list = []
    for _tag, _i1, _i2, j1, j2 in hunks:
        lo, hi = (2 * j1 + 1, 2 * j1 + 1) if j2 == j1 else (2 * (j1 + 1), 2 * j2)
        if not any(a <= hi and lo <= b for a, b in delta):
            ranges.append(f"{j1 + 1}-{j2}" if j2 > j1 + 1 else str(j1 + 1))
    return ", ".join(ranges)
