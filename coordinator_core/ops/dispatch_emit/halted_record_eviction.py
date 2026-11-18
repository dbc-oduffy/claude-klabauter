"""Halted-record run-dirt and tree eviction: the live-run and review/touch-record write sources,
the run-dirt withhold, and the plan that parks every run-owned unlanded write in the record.
Imports no sibling ``halted_record_*`` module."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

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


def _run_written(worktree_root: Path, session_id: Optional[str], run_id: str) -> list:
    """Worktree-relative paths the session or any agent it dispatched wrote since the run started,
    from the engine's touch records (executors included, unlike the review-stage source). Empty
    when the source cannot answer: unknown never names a path, so a peer's dirt is never claimed."""
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
        return sorted(found)
    except Exception:  # noqa: BLE001 -- an unreadable source names no path
        return []


def _withhold_run_dirt(withheld: dict, written: list, changed: set, landed: set) -> None:
    """Add to ``withheld`` each path in ``written`` that is still changed against HEAD, not landed
    by this commit and not already named; never a path the run did not write."""
    for p in written:
        if p in changed and p not in landed and p not in withheld:
            withheld[p] = RUN_DIRT_UNCOMMITTED


#: ``withheld`` reason for a run-owned ``.py`` whose bytes were moved into the halted record because
#: the tree would not import with them.
EVICTED_NOT_IMPORTABLE = "evicted-not-importable"

#: ``withheld`` reason for any other run-owned unlanded path whose bytes were moved into the halted record.
EVICTED_PARKED = "evicted-parked"

#: Withheld reasons that mark a path as not this run's: never judged for eviction, never reset.
_NOT_RUN_OWNED = frozenset({"peer-held", "peer-claim-indeterminate", "sibling-claimed", "foreign-halted-withheld"})


class TreeEviction:
    """A planned eviction of unimportable run-owned bytes. ``plan`` mutates nothing on disk; ``apply``
    copies the bytes into the record and resets the tree. Trap: run ``apply`` only after
    ``_record_kept``, which fingerprints the run's bytes, not HEAD's."""

    def __init__(
        self, root: Path, record_dir_rel: str, head_blobs: dict, origin: dict, unowned: dict,
        run_id: Optional[str] = None, withheld: Optional[dict] = None, blob_shas: Optional[dict] = None,
    ) -> None:
        self.root, self.record_dir_rel = root, record_dir_rel
        self.head_blobs, self.origin, self.unowned = head_blobs, origin, unowned
        self.run_id = run_id
        self.withheld = withheld
        self.blob_shas = blob_shas or {}
        self.refused: dict = {}
        self.units: list = []
        self.record_paths: list = []
        self.paths: Optional[list] = None

    @property
    def evicted(self) -> list:
        return sorted(p for p in self.head_blobs if p not in self.refused)

    def kept_view(self, withheld: dict) -> dict:
        """``withheld`` with each evicted path back at its pre-eviction reason, so a kept chunk stays kept."""
        return {**withheld, **self.origin}

    def apply(self) -> None:
        from coordinator_core.ops.dispatch_emit import halt_tree_evict

        halt_tree_evict.evict(
            self.root, self.root / self.record_dir_rel, sorted(self.head_blobs), self.head_blobs,
            run_id=self.run_id, refused=self.refused, units=self.units,
        )
        self.unowned = {**self.unowned, **self.refused}
        if self.withheld is not None:
            for rel in self.refused:
                if rel in self.origin:
                    self.withheld[rel] = self.origin[rel]
                else:
                    self.withheld.pop(rel, None)
        if self.paths is not None:
            for rel in self.refused:
                blob = f"{self.record_dir_rel}/{halt_tree_evict.BLOBS_DIR}/{self.blob_shas.get(rel)}"
                if blob in self.record_paths and blob in self.paths:
                    self.paths.remove(blob)
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


def _import_units(root: Path, own_py: list) -> list:
    """Groups of ``own_py`` paths linked by `halt_tree_check._importers`' stem rule (one file's text
    names another's module stem, either direction), transitively; groups of one are omitted."""
    stems: dict = {}
    for p in own_py:
        pp = Path(p)
        stem = pp.parent.name if pp.stem == "__init__" else pp.stem
        if stem:
            stems.setdefault(stem, []).append(p)
    parent = {p: p for p in own_py}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for q in own_py:
        try:
            text = (root / q).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for stem in set(re.findall(r"\w+", text)) & stems.keys():
            for p in stems[stem]:
                parent[find(p)] = find(q)
    groups: dict = {}
    for p in own_py:
        groups.setdefault(find(p), []).append(p)
    return [sorted(g) for g in groups.values() if len(g) > 1]


def plan_tree_eviction(
    root: Path, record_dir_rel: str, run_written: list, changed: set, write_set: list,
    withheld: dict, paths: list, run_id: Optional[str] = None,
) -> Optional[TreeEviction]:
    """Park every run-owned, changed, unlanded path (any extension) in the halted record. A ``.py``
    that ``halt_tree_check.broken_paths`` judges unimportable (with its write-set importers) is named
    in ``withheld`` as ``EVICTED_NOT_IMPORTABLE``; every other parked path as ``EVICTED_PARKED``. Paths
    under the record dir and peer-owned paths (``_NOT_RUN_OWNED``) are never parked. The record's
    ``evicted.json`` plus its blobs are appended to ``paths`` (in place). Call after
    ``_withhold_run_dirt`` and before the nothing-to-commit return. None when nothing is parked."""
    from coordinator_core.authoring_leaks import HeadView
    from coordinator_core.ops.dispatch_emit import halt_tree_check, halt_tree_evict, kept_work

    if not (root / record_dir_rel).is_dir():
        return None
    landed = set(paths)
    own = [
        p for p in run_written
        if p in changed and p not in landed and withheld.get(p) not in _NOT_RUN_OWNED
        and not p.startswith(record_dir_rel + "/")
    ]
    if not own:
        return None
    own_py = [p for p in own if p.endswith(".py")]
    broken = halt_tree_check.broken_paths(root, own_py, write_set) if own_py else {}
    targets = own
    unowned: dict = {}
    prints: dict = {}
    for p in targets:
        try:
            prints[p] = kept_work.blob_sha((root / p).read_bytes())
        except OSError:
            continue
    if not prints:
        return None
    head = HeadView(root, prints)
    head_blobs = {p: head.head_bytes(p) for p in prints}
    origin = {p: withheld[p] for p in prints if p in withheld}
    for p in prints:
        withheld[p] = EVICTED_NOT_IMPORTABLE if p in broken else EVICTED_PARKED
    appended: list = []
    for rel in (f"{record_dir_rel}/{halt_tree_evict.EVICTED_FILE}",
                *(f"{record_dir_rel}/{halt_tree_evict.BLOBS_DIR}/{sha}" for sha in prints.values())):
        if rel not in paths:
            paths.append(rel)
            appended.append(rel)
    eviction = TreeEviction(root, record_dir_rel, head_blobs, origin, unowned, run_id, withheld, prints)
    eviction.units = _import_units(root, [p for p in own_py if p in prints])
    eviction.record_paths, eviction.paths = appended, paths
    return eviction
