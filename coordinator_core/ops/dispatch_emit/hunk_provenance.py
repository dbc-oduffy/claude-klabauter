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
    settled: Optional[dict] = None,
) -> list:
    """`[landing_integrity.Finding(path, FOREIGN_HUNK_UNPRODUCED, detail)]` per path whose committed
    blob holds a hunk no row of `run_id` produced; the detail names the probable owner (first
    co-declaring sibling run, else `unknown`). Paths with no change against HEAD yield nothing.
    With `settled` given, a path whose every probable owner is not live (`_non_live`) yields no
    finding: `settled[path] = {"blob", "owner", "lines"}` carries the sha of HEAD plus only the run's
    own hunks (the peer's stay dirty in the worktree) and the peer it names.
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
            bad, own, head_bytes = _unproduced(Path(root), run_id, path)
        except Exception as exc:  # noqa: BLE001 -- unknown holds
            detail = f"provenance unreadable: {type(exc).__name__}: {exc}"
            if advisories is not None and _run_produced(Path(root), run_id, path):
                advisories.append({"path": path, "detail": detail})
            else:
                out.append(Finding(path, FOREIGN_HUNK_UNPRODUCED, detail))
            continue
        if bad:
            owners = _owners(Path(root), run_id, path, own_plan)
            owner = owners[0] if owners else UNKNOWN_OWNER
            if settled is not None and head_bytes is not None and owners and all(
                _non_live(Path(root), o) for o in owners
            ):
                try:
                    blob = _settled_blob(Path(root), path, head_bytes, own)
                except Exception:  # noqa: BLE001 -- an unsettleable path keeps the finding
                    blob = None
                if blob is not None:
                    settled[path] = {"blob": blob, "owner": owner, "lines": bad}
                    continue
            out.append(Finding(
                path,
                FOREIGN_HUNK_UNPRODUCED,
                f"committed lines {bad} were not produced by run {run_id}; probable owner {owner}",
            ))
    return out


def _owners(root: Path, run_id: str, path: str, own_plan: Optional[str]) -> list:
    """Every run co-declaring `path` that holds it against `run_id`, in hold order; [] when none or
    unreadable (owner naming is best effort)."""
    try:
        from coordinator_core.ops.dispatch_emit import peer_hunk_gate

        holds = peer_hunk_gate.co_declared_peer_holds(root, run_id, [path], [path], own_plan=own_plan)
        return [str(holder) for _p, holder in holds]
    except Exception:  # noqa: BLE001
        return []


def _non_live(root: Path, owner: str) -> bool:
    """The owner run is halted, or its emitting session is recorded and not live. An owner with no
    receipt is unknown, not dead; never raises (unknown is live)."""
    try:
        from coordinator_core.ops import warp_run_deliverable
        from coordinator_core.ops.dispatch_emit import halted_run
        from coordinator_core.session.liveness import session_live

        if warp_run_deliverable._halted(root, owner):
            return True
        emitter = halted_run.recorded_emitter(root, owner)
        return emitter is not None and not session_live(emitter)
    except Exception:  # noqa: BLE001
        return False


def _settled_blob(root: Path, path: str, head_bytes: bytes, own: list) -> str:
    """Blob sha of HEAD with only the `own` opcodes `(i1, i2, j1, j2)` applied."""
    from coordinator_core.ops.dispatch_emit import co_declared_settle
    from coordinator_core.ops.dispatch_emit import run_hunk_claims as rhc

    hunks = [
        rhc.Hunk(
            path, i1 if i2 == i1 else i1 + 1, i2 - i1, j1 if j2 == j1 else j1 + 1, j2 - j1,
        )
        for i1, i2, j1, j2 in own
    ]
    return co_declared_settle.settled_blob(root, path, head_bytes, hunks)


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


def _unproduced(root: Path, run_id: str, path: str) -> tuple:
    """`(ranges, own, head_bytes)`: committed-side line ranges ("3-4, 9") of hunks no row produced
    ("" when all are produced); the produced hunks as `(i1, i2, j1, j2)` opcodes; the HEAD blob bytes
    (None when the path is absent from HEAD)."""
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
        return "", [], None
    form = kept_work.checkin_form(root, path, raw)
    if form is None:
        raise ValueError("worktree bytes have no checkin form")
    committed = form.splitlines(keepends=True)

    entry = head_blobs(root, [path]).get(path)
    head: list = []
    head_bytes: Optional[bytes] = None
    if entry is not None:
        obj = git_objects.read_object(common, entry[1])
        if obj is None or obj[0] != "blob":
            raise ValueError(f"HEAD blob {entry[1]} unreadable")
        head_bytes = obj[1]
        head = head_bytes.splitlines(keepends=True)
    hunks = rhc.changed_opcodes(head, committed)
    if not hunks:
        return "", [], head_bytes

    committed_sha = kept_work.blob_sha(form)
    for kept in kept_work.kept_fingerprints(record_dir(root, run_id)).get(path, ()):
        if kept_work.matches_committed(root, path, kept, committed_sha):
            return "", [], head_bytes

    base = hha._baseline(root, run_id)
    if base is None or path not in base:
        if _is_run_own_artifact(root, run_id, path):
            return "", [], head_bytes
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
    own: list = []
    for _tag, i1, i2, j1, j2 in hunks:
        lo, hi = (2 * j1 + 1, 2 * j1 + 1) if j2 == j1 else (2 * (j1 + 1), 2 * j2)
        if any(a <= hi and lo <= b for a, b in delta):
            own.append((i1, i2, j1, j2))
        else:
            ranges.append(f"{j1 + 1}-{j2}" if j2 > j1 + 1 else str(j1 + 1))
    return ", ".join(ranges), own, head_bytes
