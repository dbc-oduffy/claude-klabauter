"""Halted-record run-dirt and tree eviction: the live-run and review/touch-record write sources,
the run-dirt withhold, and the plan that parks every run-owned unlanded write in the record.
Imports no sibling ``halted_record_*`` module."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from coordinator_core.ops.dispatch_emit import review_edits


def _live_run_ids(repo_root: Path) -> list:
    """Run ids holding a live or claimed token; a read failure names none."""
    from coordinator_core.ops.dispatch_emit import abandoned_runs

    try:
        return [r["run_id"] for r in abandoned_runs._token_records(repo_root)]
    except Exception:
        return []


def _review_written(worktree_root: Path, session_id: Optional[str], run_id: str) -> Optional[dict]:
    """Review-stage writes since the run started; None when the source cannot answer."""
    from coordinator_core.ops.dispatch_emit.terminal_commit import _run_started_at

    try:
        return review_edits.review_writes(worktree_root, session_id, _run_started_at(worktree_root, run_id))
    except Exception:
        return None


#: ``withheld`` reason for a path this run wrote that stays uncommitted and no other leg named.
RUN_DIRT_UNCOMMITTED = "run-dirt-uncommitted"


def _own_attributed(worktree_root: Path, run_id: str) -> set:
    """Paths the run's own manifest ``write_baseline`` declares; empty when unreadable."""
    from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha

    try:
        return set(hha._baseline(worktree_root, run_id) or ())
    except Exception:  # noqa: BLE001 -- unknown attributes nothing
        return set()


def _run_written(
    worktree_root: Path, session_id: Optional[str], run_id: str, live_run_ids: Optional[list] = None,
) -> list:
    """Worktree-relative paths the session or any agent it dispatched wrote since the run started,
    from the engine's touch records (executors included, unlike the review-stage source). Empty
    when the source cannot answer: unknown never names a path, so a peer's dirt is never claimed.

    Trap: a touch record is keyed by session, not run, so while another run is live a touched path
    may be that run's. Then only a path in this run's own ``write_baseline`` is named; the rest stay
    in the tree (``live_run_ids`` carries the live runs; None or only this run means no contention)."""
    import os

    from coordinator_core.ops.dispatch_emit.terminal_commit import _run_started_at
    from coordinator_core.session import core, touch_record

    try:
        since = _run_started_at(worktree_root, run_id)
        hub = core.sessions_dir(str(worktree_root))
        if not session_id or since is None or not hub:
            return []
        session_dir = os.path.join(hub, session_id)
        sinks = [session_dir]
        try:
            ledger = Path(session_dir, "dispatched-agents.txt").read_text(encoding="utf-8")
        except FileNotFoundError:
            ledger = ""
        for row in ledger.splitlines():
            fields = row.split("\t")
            agent_dir = os.path.join(hub, ".agents", fields[0]) if fields[0] else None
            if agent_dir and len(fields) >= 3 and touch_record.agent_owner_sid(agent_dir)[0] == session_id:
                sinks.append(agent_dir)
        found: set = set()
        for sink in dict.fromkeys(sinks):
            for member in touch_record.discover_family(touch_record.sink_path(sink)):
                try:
                    raw = member.read_bytes()
                except FileNotFoundError:
                    continue
                for line in touch_record.iter_complete_lines(raw):
                    try:
                        event = touch_record.decode_line(line)
                    except touch_record.MalformedRecordLine:
                        continue
                    if (
                        event.verb == touch_record.VERB_TOUCH and event.kind != touch_record.KIND_READ
                        and event.timestamp >= since
                    ):
                        rel = review_edits.norm(event.path)
                        if not review_edits.out_of_worktree(rel) and not rel.startswith("scratch/"):
                            found.add(rel)
        if any(r != run_id for r in live_run_ids or ()):
            found &= _own_attributed(worktree_root, run_id)
        return sorted(found)
    except Exception:  # noqa: BLE001 -- an unreadable source names no path
        return []


def _withhold_run_dirt(withheld: dict, written: list, changed: set, landed: set) -> None:
    """Add to ``withheld`` each path in ``written`` that is still changed against HEAD, not landed
    by this commit and not already named; never a path the run did not write."""
    for p in written:
        if p in changed and p not in landed and p not in withheld:
            withheld[p] = RUN_DIRT_UNCOMMITTED


#: Halt kind of a verify that ran zero tests (`clean_overlay_verify.ZERO_RUN_TEST_INFRA`); never evicts.
TEST_INFRA_HALT = "test_infra_failure"

#: ``withheld`` reason for any other run-owned unlanded path whose bytes were moved into the halted record.
EVICTED_PARKED = "evicted-parked"

#: Withheld reasons that mark a path as not this run's: never judged for eviction, never reset.
_NOT_RUN_OWNED = frozenset({"peer-held", "peer-claim-indeterminate", "sibling-claimed", "foreign-halted-withheld"})


class TreeEviction:
    """A planned parking of run-owned bytes. ``plan`` mutates nothing on disk; ``apply`` copies each
    path's tree bytes into the record and never writes or unlinks a worktree file. Run ``apply`` after
    ``_record_kept``, which fingerprints the run's bytes."""

    def __init__(
        self, root: Path, record_dir_rel: str, evicted_paths: Iterable[str], origin: dict,
        withheld: Optional[dict] = None, unimportable: Optional[Iterable[str]] = None,
    ) -> None:
        self.root, self.record_dir_rel = root, record_dir_rel
        self.evicted_paths = sorted(evicted_paths)
        self.origin = origin
        self.withheld = withheld
        self._unimportable = set(unimportable or ())
        self.record_paths: list = []
        self.paths: Optional[list] = None

    @property
    def evicted(self) -> list:
        return list(self.evicted_paths)

    @property
    def unimportable(self) -> list:
        """Evicted paths the halt import check judged broken: a reply diagnostic only (the withheld
        reason stays ``EVICTED_PARKED``), read by ``land_run_halt`` and ``halted_record_commit``."""
        return sorted(p for p in self.evicted if p in self._unimportable)

    def kept_view(self, withheld: dict) -> dict:
        """``withheld`` with each evicted path back at its pre-eviction reason, so a kept chunk stays kept."""
        return {**withheld, **self.origin}

    def apply(self) -> None:
        from coordinator_core.ops.dispatch_emit import halt_tree_evict

        halt_tree_evict.evict(self.root, self.root / self.record_dir_rel, self.evicted_paths)
        if self.paths is not None:
            for rel in self.record_paths:
                if not (self.root / rel).exists() and rel in self.paths:
                    self.paths.remove(rel)


def record_touched_blobs(
    root: Path, record_dir_rel: str, run_written: list, paths: list, dirty: Optional[list] = None,
) -> list:
    """Write ``<record_dir>/touched-blobs.json`` (``{path: {base, run}}``) for every path in
    ``run_written`` and append its repo-relative path to ``paths``. ``base`` is the manifest
    ``write_baseline`` blob, else the HEAD blob from one ``HeadView`` over the whole set; ``run`` is the
    worktree bytes' blob (None when deleted). Returns the ``dirty`` paths no run manifest declares
    (``stranded_sibling.ownerless``). Never raises: an unwritable record names no claim."""
    try:
        from coordinator_core.authoring_leaks import HeadView
        from coordinator_core.ops.dispatch_emit import kept_work, stranded_sibling
        from coordinator_core.ops.dispatch_emit.halted_record_request import _manifest_baseline

        record_dir = root / record_dir_rel
        owned = sorted({p for p in run_written if not p.startswith(record_dir_rel + "/")})
        ownerless = stranded_sibling.ownerless(root, [p for p in (dirty or ()) if not p.startswith(record_dir_rel + "/")])
        if not record_dir.is_dir() or not owned:
            return ownerless
        baseline = _manifest_baseline(record_dir) or {}
        head = HeadView(root, [p for p in owned if p not in baseline])
        touched = {}
        for p in owned:
            if p in baseline:
                base = baseline[p]
            else:
                entry = head.entry(p)
                base = entry[1] if entry else None
            touched[p] = {"base": base, "run": kept_work.fingerprint(root, p)}
        out = stranded_sibling.write(record_dir, touched)
        rel = f"{record_dir_rel}/{stranded_sibling.TOUCHED_FILE}"
        if out is not None and rel not in paths:
            paths.append(rel)
        return ownerless
    except Exception:  # noqa: BLE001 -- the record is advisory; a halt never fails on it
        return []


def _on_baseline(root: Path, record_dir: Path, paths: list) -> list:
    """``paths`` minus each whose manifest ``write_baseline`` blob is not HEAD's blob now: a later
    commit or peer moved it past the run's baseline, so the bytes are not provably the run's change.
    A path outside the baseline, or no readable baseline, is kept."""
    if not paths:
        return paths
    try:
        from coordinator_core.ops.dispatch_emit.halt_tree_evict import _current_heads
        from coordinator_core.ops.dispatch_emit.halted_record_request import _manifest_baseline

        baseline = _manifest_baseline(record_dir)
        if not baseline:
            return paths
        heads = _current_heads(root, [p for p in paths if p in baseline])
    except Exception:  # noqa: BLE001 -- an unreadable baseline fences nothing
        return paths
    return [p for p in paths if p not in baseline or baseline[p] == heads.get(p)]


def plan_tree_eviction(
    root: Path, record_dir_rel: str, run_written: list, changed: set, write_set: list,
    withheld: dict, paths: list, credited: Optional[set] = None, *, halt_kind: Optional[str] = None,
) -> Optional[TreeEviction]:
    """Park every run-owned, changed, unlanded path (any extension) in the halted record; the tree keeps
    its bytes. A ``.py`` that ``halt_tree_check.broken_paths`` judges unimportable (with its write-set
    importers) is listed in ``TreeEviction.unimportable``; every parked path is named in ``withheld`` as
    ``EVICTED_PARKED``. Paths under the record dir and peer-owned paths (``_NOT_RUN_OWNED``) are never
    parked. The record's ``evicted.json`` plus its blobs are appended to ``paths`` (in place). Call after
    ``_withhold_run_dirt`` and before the nothing-to-commit return. None when nothing is parked, and always None
    for a ``test_infra_failure`` halt: the tests never judged the bytes (zero ran).
    ``credited`` (when given) bounds eviction to the run's declared or touch-recorded paths: a path
    found only by the tree mtime sweep is a peer's live dirt until declared, and is never recorded."""
    from coordinator_core.ops.dispatch_emit import halt_tree_check, halt_tree_evict, kept_work

    if halt_kind == TEST_INFRA_HALT or not (root / record_dir_rel).is_dir():
        return None
    landed = set(paths)
    own = [
        p for p in run_written
        if p in changed and p not in landed and withheld.get(p) not in _NOT_RUN_OWNED
        and not p.startswith(record_dir_rel + "/") and (credited is None or p in credited)
    ]
    own = _on_baseline(root, root / record_dir_rel, own)
    prints: dict = {}
    for p in own:
        try:
            prints[p] = kept_work.blob_sha((root / p).read_bytes())
        except OSError:
            continue
    if not prints:
        return None
    checked_py = [p for p in prints if p.endswith(".py")]
    broken = halt_tree_check.broken_paths(root, checked_py, write_set) if checked_py else {}
    origin = {p: withheld[p] for p in prints if p in withheld}
    for p in prints:
        withheld[p] = EVICTED_PARKED
    appended: list = []
    for rel in (f"{record_dir_rel}/{halt_tree_evict.EVICTED_FILE}",
                *(f"{record_dir_rel}/{halt_tree_evict.BLOBS_DIR}/{sha}" for sha in prints.values())):
        if rel not in paths:
            paths.append(rel)
            appended.append(rel)
    eviction = TreeEviction(root, record_dir_rel, prints, origin, withheld, unimportable=broken)
    eviction.record_paths, eviction.paths = appended, paths
    return eviction
