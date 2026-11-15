"""Hunk provenance: does every hunk a landing commits for a declared path trace to this run?

`unproduced_hunks` compares the to-be-committed blob (worktree in checkin form) with HEAD and
keeps a hunk only when this run produced it: the blob equals a `kept.json` fingerprint, or the
hunk lies inside the run's `write_baseline`-to-committed delta, or a `kept.json` blob of the run
carries the hunk and no other holding run's blob does. A hunk already in the baseline otherwise
has no producing row. Zero spawn; unknown (unreadable baseline, coarse delta) holds.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator, Mapping, Optional, Sequence

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


class RunInvariants:
    """What a run owns, declares and baselined: read once per instance, on first use, however many
    paths ask. A read that raised re-raises the same error on every later ask, so a per-path
    caller keeps its per-path failure semantics. Scoped to one landing by the caller; never cached
    across landings."""

    def __init__(self, root: Path, run_id: str) -> None:
        self.root = Path(root)
        self.run_id = run_id
        self._memo: dict = {}

    def _once(self, key: object, fn):
        if key not in self._memo:
            try:
                self._memo[key] = (True, fn())
            except Exception as exc:  # noqa: BLE001 -- replayed to every asker
                self._memo[key] = (False, exc)
        ok, value = self._memo[key]
        if not ok:
            raise value
        return value

    def own_artifacts(self):
        from coordinator_core.ops.dispatch_emit import run_artifact_ownership

        return self._once("own", lambda: run_artifact_ownership.own_artifact_paths(self.root, self.run_id))

    def manifest(self) -> dict:
        from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha

        return self._once("manifest", lambda: hha._manifest(self.root, self.run_id))

    def baseline(self) -> Optional[dict]:
        from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha

        def build():
            with hha.pinned_manifest(self.root, self.run_id, self.manifest()):
                return hha._baseline(self.root, self.run_id)

        return self._once("baseline", build)

    def declared(self) -> frozenset:
        """Normalised manifest `rows[].writes` paths."""
        from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha

        def build():
            rows = self.manifest().get("rows")
            return frozenset(
                hha._norm(w)
                for row in (rows if isinstance(rows, list) else ())
                for w in ((row.get("writes") if isinstance(row, dict) else None) or ())
                if isinstance(w, str)
            )

        return self._once("declared", build)

    def kept(self, run: Optional[str] = None) -> dict:
        """`kept_work.kept_fingerprints` of `run` (default this run), read once per run."""
        from coordinator_core.ops.dispatch_emit import kept_work
        from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

        target = run or self.run_id
        return self._once(("kept", target), lambda: kept_work.kept_fingerprints(record_dir(self.root, target)))

    def predecessors(self) -> tuple:
        """Earlier runs of this run's lineage: those bound to its sizing, plus `ask_stage._lineage_kin`
        (the `resumed_from` chain, earlier-sizing runs, an unbound first-fire ask). Empty when unreadable."""

        def build() -> tuple:
            out: dict = {}
            try:
                out.update(dict.fromkeys(self.own_artifacts().predecessors))
            except Exception:  # noqa: BLE001 -- unreadable lineage names no predecessor
                pass
            try:
                from coordinator_core.ops.dispatch_emit.ask_stage import _lineage_kin

                out.update(dict.fromkeys(_lineage_kin(self.root, self.run_id)))
            except Exception:  # noqa: BLE001
                pass
            return tuple(out)

        return self._once("predecessors", build)

    def own_kept(self) -> dict:
        """`kept(path)` blobs of this run and its same-sizing predecessors: the bytes a predecessor fire
        wrote are this run's work. A predecessor whose kept record cannot be read contributes nothing."""

        def build() -> dict:
            out: dict = {k: set(v) for k, v in self.kept().items()}
            for pred in self.predecessors():
                try:
                    for rel, blobs in self.kept(pred).items():
                        out.setdefault(rel, set()).update(blobs)
                except Exception:  # noqa: BLE001 -- unreadable evidence is none
                    continue
            return {k: frozenset(v) for k, v in out.items()}

        return self._once("own_kept", build)


_ACTIVE: ContextVar[Optional[RunInvariants]] = ContextVar("hunk_provenance_invariants", default=None)


def run_invariants(root: Path, run_id: str) -> RunInvariants:
    """A fresh `RunInvariants` for `run_id`; pass it as `invariants=` or open `invariants_scope`."""
    return RunInvariants(root, run_id)


@contextmanager
def invariants_scope(root: Path, run_id: str, invariants: Optional[RunInvariants] = None) -> Iterator[RunInvariants]:
    """Within the block every provenance helper for `run_id` under `root` shares one `RunInvariants`
    (the one given, else the enclosing scope's, else a new one)."""
    inv = invariants or _active(root, run_id) or RunInvariants(root, run_id)
    token = _ACTIVE.set(inv)
    try:
        yield inv
    finally:
        _ACTIVE.reset(token)


def _active(root: Path, run_id: str) -> Optional[RunInvariants]:
    inv = _ACTIVE.get()
    return inv if inv is not None and inv.run_id == run_id and inv.root == Path(root) else None


def _inv(root: Path, run_id: str) -> RunInvariants:
    """The enclosing scope's invariants for this run, else a one-shot instance (no sharing)."""
    return _active(root, run_id) or RunInvariants(root, run_id)


def unproduced_hunks(
    root: Path,
    run_id: str,
    paths: Sequence[str],
    *,
    own_plan: Optional[str],
    advisories: Optional[list] = None,
    settled: Optional[dict] = None,
    invariants: Optional[RunInvariants] = None,
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
    landing after this run's stage inside the run's own delta span is indistinguishable here.
    `invariants` (see `run_invariants`) is shared across calls of one landing; absent, the enclosing
    `invariants_scope` or a per-call instance serves."""
    with invariants_scope(root, run_id, invariants):
        return _unproduced_hunks(root, run_id, paths, own_plan, advisories, settled)


def _unproduced_hunks(
    root: Path, run_id: str, paths: Sequence[str], own_plan: Optional[str],
    advisories: Optional[list], settled: Optional[dict],
) -> list:
    from coordinator_core.ops.dispatch_emit.landing_targeted_tests import Finding

    out: list = []
    for raw in paths:
        path = str(raw or "").replace("\\", "/")
        if not path:
            continue
        try:
            bad, own, head_bytes = _provenance(Path(root), run_id, path, own_plan)
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
        for removed, added in _kept_blocks(root, common, path, head, blobs):
            if added and _occurrences(work, list(added)) > _occurrences(head, list(added)):
                return True
            if not added and removed and _occurrences(work, list(removed)) < _occurrences(head, list(removed)):
                return True
        return False
    except Exception:  # noqa: BLE001 -- unreadable evidence holds
        return True


def _kept_blocks(root: Path, common: Path, path: str, head: list, blobs: Sequence[str]) -> set:
    """`{(removed, added)}` line-tuple blocks of each kept blob in `blobs` against `head`, in checkin
    form. A blob that cannot be read or lifted contributes nothing."""
    from coordinator_core.git import git_objects
    from coordinator_core.ops.dispatch_emit import kept_work
    from coordinator_core.ops.dispatch_emit import run_hunk_claims as rhc

    out: set = set()
    for blob in blobs:
        obj = git_objects.read_object(common, blob)
        lifted = kept_work.checkin_form(root, path, obj[1]) if obj is not None else None
        if lifted is None:
            continue
        kept = lifted.splitlines(keepends=True)
        for _tag, i1, i2, j1, j2 in rhc.changed_opcodes(head, kept):
            out.add((tuple(head[i1:i2]), tuple(kept[j1:j2])))
    return out


def _own_hunk_evidence(
    root: Path, common: Path, run_id: str, path: str, head: list, own_plan: Optional[str],
) -> tuple:
    """`(mine, peers)`: kept-blob blocks of `run_id` for `path`, and of every other run holding it
    (co-declaring owners, the halted holder). Never raises: unreadable evidence is empty."""
    inv = _inv(root, run_id)

    def blocks(run: Optional[str]) -> set:
        try:
            blobs = (inv.kept(run) if run else inv.own_kept()).get(path)
            return _kept_blocks(root, common, path, head, sorted(blobs)) if blobs else set()
        except Exception:  # noqa: BLE001 -- unreadable evidence is none
            return set()

    mine = blocks(None)
    if not mine:
        return mine, set()
    kin = {run_id, *inv.predecessors()}
    others = [o for o in _owners(root, run_id, path, own_plan) if o not in kin]
    holder = _halted_holder(root, run_id, own_plan, path)
    if holder is not None and holder not in kin:
        others.append(holder)
    peers: set = set()
    for other in others:
        peers |= blocks(other)
    return mine, peers


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
    with invariants_scope(root, run_id) as inv:
        for path, holder in held.items():
            if path not in split_only and not peer_hunks_present(root, holder, path):
                keep.add(path)
                continue
            settled: dict = {}
            found = unproduced_hunks(
                root, run_id, [path], own_plan=own_plan, advisories=[], settled=settled, invariants=inv,
            )
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
    with invariants_scope(root, run_id):
        return _peer_split(Path(root), run_id, paths, own_plan)


def _peer_split(root: Path, run_id: str, paths: Sequence[str], own_plan: Optional[str]) -> tuple:
    split: dict = {}
    unsettled: dict = {}
    for raw in paths:
        path = str(raw or "").replace("\\", "/")
        if not path:
            continue
        try:
            bad, own, head_bytes = _provenance(root, run_id, path, own_plan)
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
    return _inv(root, run_id).own_artifacts().covers(path)


def _declares(root: Path, run_id: str, path: str) -> bool:
    """The run's manifest `rows[].writes` names `path`; a run with no baseline for it still owns
    the edits it declared. False on any read failure."""
    try:
        return path in _inv(root, run_id).declared()
    except Exception:  # noqa: BLE001 -- unknown is not declared
        return False


def _run_produced(root: Path, run_id: str, path: str) -> bool:
    """The run's own record produced `path`: its write_baseline names it, its manifest declares it,
    or it is a run-own artifact. False on any read failure (unknown is not attributable)."""
    try:
        base = _inv(root, run_id).baseline()
        if base is not None and path in base:
            return True
        return _declares(root, run_id, path) or _is_run_own_artifact(root, run_id, path)
    except Exception:  # noqa: BLE001 -- unknown stays a finding
        return False


def _provenance(root: Path, run_id: str, path: str, own_plan: Optional[str]) -> tuple:
    """`_unproduced`, handing `own_plan` over only when set so the plan-less call keeps its 3-arg shape."""
    if own_plan is None:
        return _unproduced(root, run_id, path)
    return _unproduced(root, run_id, path, own_plan)


def _unproduced(root: Path, run_id: str, path: str, own_plan: Optional[str] = None) -> tuple:
    """`(ranges, own, head_bytes)`: committed-side line ranges ("3-4, 9") of hunks no row produced
    ("" when all are produced); the produced hunks as `(i1, i2, j1, j2)` opcodes; the HEAD blob bytes
    (None when the path is absent from HEAD). A hunk outside the baseline-to-committed delta is produced
    only when a kept blob of `run_id` carries its exact block against HEAD and no kept blob of another
    run holding the path does; whole-file kept blobs carry peer hunks too, so any other hunk stays
    unattributed."""
    from coordinator_core.git import git_objects
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_state import head_blobs
    from coordinator_core.ops.dispatch_emit import kept_work
    from coordinator_core.ops.dispatch_emit import run_hunk_claims as rhc

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
    inv = _inv(root, run_id)
    for kept in inv.own_kept().get(path, ()):
        if kept_work.matches_committed(root, path, kept, committed_sha):
            return "", [], head_bytes

    base = inv.baseline()
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
    outside = [
        h for h in hunks
        if not any(a <= _span(h)[1] and _span(h)[0] <= b for a, b in delta)
    ]
    mine: set = set()
    peers: set = set()
    if outside:
        mine, peers = _own_hunk_evidence(root, common, run_id, path, head, own_plan)

    ranges: list = []
    own: list = []
    for hunk in hunks:
        _tag, i1, i2, j1, j2 = hunk
        block = (tuple(head[i1:i2]), tuple(committed[j1:j2]))
        if hunk not in outside or (block in mine and block not in peers):
            own.append((i1, i2, j1, j2))
        else:
            ranges.append(f"{j1 + 1}-{j2}" if j2 > j1 + 1 else str(j1 + 1))
    return ", ".join(ranges), own, head_bytes


def _span(hunk: tuple) -> tuple:
    """The doubled-index interval of a committed-side hunk, comparable with `rhc._delta_spans`."""
    _tag, _i1, _i2, j1, j2 = hunk
    return (2 * j1 + 1, 2 * j1 + 1) if j2 == j1 else (2 * (j1 + 1), 2 * j2)
