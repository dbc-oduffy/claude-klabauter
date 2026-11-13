"""Hunk provenance: does every hunk a landing commits for a declared path trace to this run?

`unproduced_hunks` compares the to-be-committed blob (worktree in checkin form) with HEAD and
keeps a hunk only when this run produced it: the blob equals a `kept.json` fingerprint, or the
hunk lies inside the run's `write_baseline`-to-committed delta. A hunk already in the baseline
has no producing row. Zero spawn; unknown (unreadable baseline, coarse delta) holds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional, Sequence

FOREIGN_HUNK_UNPRODUCED = "foreign-hunk-unproduced"
UNATTRIBUTABLE_LINES = "all (provenance unreadable)"
UNKNOWN_OWNER = "unknown"
NO_BASELINE_OWNER = "no-write-baseline"


def is_no_baseline_detail(detail: str) -> bool:
    """True when a `foreign-hunk-settled` provenance detail names `NO_BASELINE_OWNER`; the detail text
    is built at the emit site in `terminal_commit_refusals` as `... belong to run {owner} (not live)...`."""
    return f"run {NO_BASELINE_OWNER} " in (detail or "")


class NoWriteBaseline(ValueError):
    """The run has no write_baseline for a dirty path it neither declared nor produced; carries the
    HEAD blob sha (None when the path is absent from HEAD) so a caller can land HEAD for it."""

    def __init__(self, head_sha: Optional[str]) -> None:
        super().__init__("run has no write_baseline for the path")
        self.head_sha = head_sha


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
    own hunks (the peer's stay dirty in the worktree) and the peer it names. A tracked path the run
    has no write_baseline for, never declared and did not produce settles to its HEAD blob with owner
    `NO_BASELINE_OWNER`: excluded from the landing, left dirty.
    Never raises: a read failure is a finding, except on a path the run's own record produced
    (its write_baseline names it, or it is a run-own artifact): there the unattributable read is
    appended to `advisories` as `{"path", "detail"}` and yields no finding. DR-467: a path the run
    neither baselined nor declared in its manifest rows stays a finding. Trap: a peer write
    landing after this run's stage inside the run's own delta span is indistinguishable here."""
    from coordinator_core.ops.dispatch_emit.landing_targeted_tests import Finding

    out: list = []
    for raw in paths:
        path = str(raw or "").replace("\\", "/")
        if not path:
            continue
        try:
            bad, own, head_bytes = _unproduced(Path(root), run_id, path)
        except Exception as exc:  # noqa: BLE001 -- unknown holds
            if (
                isinstance(exc, NoWriteBaseline) and settled is not None and exc.head_sha
                and not _run_produced(Path(root), run_id, path)
            ):
                settled[path] = {"blob": exc.head_sha, "owner": NO_BASELINE_OWNER, "lines": ""}
                continue
            detail = f"provenance unreadable: {type(exc).__name__}: {exc}"
            if advisories is not None and _run_produced(Path(root), run_id, path):
                holder = _halted_holder(Path(root), run_id, own_plan, path)
                if holder is None:
                    advisories.append({"path": path, "detail": detail})
                    continue
                head_sha = _head_blob_sha(Path(root), path) if settled is not None else None
                if head_sha:
                    settled[path] = {"blob": head_sha, "owner": holder, "lines": UNATTRIBUTABLE_LINES}
                else:
                    out.append(Finding(path, FOREIGN_HUNK_UNPRODUCED, f"{detail}; run {holder} withholds the path"))
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


def _halted_holder(root: Path, run_id: str, own_plan: Optional[str], path: str) -> Optional[str]:
    """The halted peer run that still withholds `path`, else None (also None when the hold cannot be read)."""
    try:
        from coordinator_core.ops.dispatch_emit import land_run

        holder = land_run.foreign_halted_withheld(root, run_id, own_plan, [path]).get(path)
        return None if holder in (None, land_run.UNREADABLE_HALT_RECORD) else str(holder)
    except Exception:  # noqa: BLE001 -- an unreadable hold is no named holder
        return None


def _head_blob_sha(root: Path, path: str) -> Optional[str]:
    from coordinator_core.git.git_state import head_blobs

    entry = head_blobs(root, [path]).get(path)
    return entry[1] if entry is not None else None


def peer_hunks_present(root: Path, holder: str, path: str) -> bool:
    """True when a hunk of `holder`'s kept blobs for `path` (its text against HEAD) is in the worktree
    file, or the evidence cannot be read. A run with no kept blob for `path` leaves no evidence: False."""
    try:
        from coordinator_core.git import git_objects
        from coordinator_core.git.git_dir import resolve_git_common_dir
        from coordinator_core.ops.dispatch_emit import kept_work
        from coordinator_core.ops.dispatch_emit import run_hunk_claims as rhc
        from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

        blobs = kept_work.kept_fingerprints(record_dir(root, holder)).get(path)
        if not blobs:
            return False
        common = resolve_git_common_dir(root)
        form = kept_work.checkin_form(root, path, (root / path).read_bytes())
        entry = _head_blob_sha(root, path)
        head_obj = git_objects.read_object(common, entry) if entry else None
        if form is None or (entry and head_obj is None):
            return True
        work = form.splitlines(keepends=True)
        head = head_obj[1].splitlines(keepends=True) if head_obj else []
        for blob in blobs:
            obj = git_objects.read_object(common, blob)
            lifted = kept_work.checkin_form(root, path, obj[1]) if obj is not None else None
            if lifted is None:
                continue
            kept = lifted.splitlines(keepends=True)
            for _tag, i1, i2, j1, j2 in rhc.changed_opcodes(head, kept):
                added = kept[j1:j2]
                if added and _occurrences(work, added) > _occurrences(head, added):
                    return True
                if not added and head[i1:i2] and _occurrences(work, head[i1:i2]) < _occurrences(head, head[i1:i2]):
                    return True
        return False
    except Exception:  # noqa: BLE001 -- unreadable evidence holds
        return True


def _occurrences(lines: list, block: list) -> int:
    n = len(block)
    return sum(1 for k in range(len(lines) - n + 1) if lines[k : k + n] == block)


def landable_under_hold(
    root: Path, run_id: str, own_plan: Optional[str], held: Mapping[str, str],
    split_only: frozenset = frozenset(),
) -> set:
    """The subset of `held` (`{path: halted holder run id}`, each path the landing run declares) this
    landing can still carry: the holder's recorded hunks are not in the file, or the run's own hunks
    split off as a settled blob. Any other path stays withheld, because its worktree bytes would carry
    the holder's hunks. A `split_only` path (a reviewer flagged its peer hunks out of the ask) is
    carried only through the split blob: an unrecorded peer hunk does not release it."""
    keep: set = set()
    for path, holder in held.items():
        if path not in split_only and not peer_hunks_present(root, holder, path):
            keep.add(path)
            continue
        settled: dict = {}
        found = unproduced_hunks(root, run_id, [path], own_plan=own_plan, advisories=[], settled=settled)
        entry = settled.get(path)
        if not found and entry is not None and entry["lines"] not in ("", UNATTRIBUTABLE_LINES):
            keep.add(path)
    return keep


def peer_split(
    root: Path, run_id: str, paths: Sequence[str], *, own_plan: Optional[str] = None,
) -> tuple:
    """`(split, unsettled)` over `paths` the run declares. `split[path] = {"blob", "owner", "lines"}`: the sha
    of HEAD plus only the run's own hunks, for a path whose worktree bytes also carry hunks outside the run's
    `write_baseline` delta (a peer's), whatever the peer's liveness. `unsettled[path] = {"owner", "lines"}`:
    a path carrying a peer's hunks (or whose provenance is unreadable while a peer co-declares it) that cannot
    be split, naming the probable owner run. A path with no peer evidence appears in neither. Zero spawn;
    never raises."""
    root = Path(root)
    split: dict = {}
    unsettled: dict = {}
    for raw in paths:
        path = str(raw or "").replace("\\", "/")
        if not path:
            continue
        try:
            bad, own, head_bytes = _unproduced(root, run_id, path)
        except Exception:  # noqa: BLE001 -- unreadable provenance names a peer only when one co-declares the path
            owner = _peer_owner(root, run_id, own_plan, path)
            if owner is not None and not _own_chain(root, run_id, owner):
                unsettled[path] = {"owner": owner, "lines": UNATTRIBUTABLE_LINES}
            continue
        if not bad:
            continue
        owner = _peer_owner(root, run_id, own_plan, path)
        if owner is not None and _own_chain(root, run_id, owner):
            continue
        owner = owner or UNKNOWN_OWNER
        blob = None
        if head_bytes is not None:
            try:
                blob = _settled_blob(root, path, head_bytes, own)
            except Exception:  # noqa: BLE001 -- an unsettleable path is named, not split
                blob = None
        if blob is not None:
            split[path] = {"blob": blob, "owner": owner, "lines": bad}
        else:
            unsettled[path] = {"owner": owner, "lines": bad}
    return split, unsettled


def _own_chain(root: Path, run_id: str, owner: str) -> bool:
    """`owner` is `run_id`'s own chain (the ask run a warp run was chained from, or a resume ancestor):
    its hunks are this run's work, so splitting them out leaves a candidate missing half the change.
    Never raises; unreadable lineage is not kin."""
    try:
        from coordinator_core.ops.dispatch_emit.ask_stage import _chain_kin

        return owner in _chain_kin(root, run_id, [owner])
    except Exception:  # noqa: BLE001
        return False


def _peer_owner(root: Path, run_id: str, own_plan: Optional[str], path: str) -> Optional[str]:
    """First co-declaring peer run holding `path` against `run_id`, else the halted run withholding it, else None."""
    owners = _owners(root, run_id, path, own_plan)
    return owners[0] if owners else _halted_holder(root, run_id, own_plan, path)


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


def _declares(root: Path, run_id: str, path: str) -> bool:
    """The run's manifest `rows[].writes` names `path`; a run with no baseline for it still owns
    the edits it declared. False on any read failure."""
    try:
        from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha

        rows = hha._manifest(root, run_id).get("rows")
        for row in rows if isinstance(rows, list) else ():
            writes = row.get("writes") if isinstance(row, dict) else None
            if any(isinstance(w, str) and hha._norm(w) == path for w in writes or ()):
                return True
        return False
    except Exception:  # noqa: BLE001 -- unknown is not declared
        return False


def _run_produced(root: Path, run_id: str, path: str) -> bool:
    """The run's own record produced `path`: its write_baseline names it, its manifest declares it,
    or it is a run-own artifact. False on any read failure (unknown is not attributable)."""
    try:
        from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha

        base = hha._baseline(root, run_id)
        if base is not None and path in base:
            return True
        return _declares(root, run_id, path) or _is_run_own_artifact(root, run_id, path)
    except Exception:  # noqa: BLE001 -- unknown stays a finding
        return False


def _recorded_own_write(root: Path, run_id: str, path: str) -> bool:
    """The run's own record (`run-outcome.json` review slice and test edits, `halted.json` withheld
    keys) lists `path` as written by this run, so a hunk its `write_baseline` already carries is the
    run's edit, not a peer's. Path-level evidence: it cannot split a peer hunk the baseline also holds.
    Trap: run ask-20261113T043855-021188 re-staged (`stage-receipt.json` 04:47, `manifest.json`
    rewritten after executor C1 wrote) so its `write_baseline` blob already held the run's
    `except PermissionError` edit and the baseline-to-committed delta was empty; the manifest alone
    cannot say whether a baseline hunk predates the run. False on any read failure."""
    try:
        from coordinator_core.ops.dispatch_emit import write_baseline
        from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

        return path in write_baseline.prior_pass_written(root, record_dir(root, run_id), include_predecessors=False)
    except Exception:  # noqa: BLE001 -- unknown is not own
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
        raise NoWriteBaseline(entry[1] if entry is not None else None)
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
    wrote_it = _recorded_own_write(root, run_id, path)

    ranges: list = []
    own: list = []
    for _tag, i1, i2, j1, j2 in hunks:
        lo, hi = (2 * j1 + 1, 2 * j1 + 1) if j2 == j1 else (2 * (j1 + 1), 2 * j2)
        if wrote_it or any(a <= hi and lo <= b for a, b in delta):
            own.append((i1, i2, j1, j2))
        else:
            ranges.append(f"{j1 + 1}-{j2}" if j2 > j1 + 1 else str(j1 + 1))
    return ", ".join(ranges), own, head_bytes
