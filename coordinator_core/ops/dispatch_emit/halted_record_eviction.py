"""Halted-record run-dirt and tree eviction: the live-run and review/touch-record write sources,
the run-dirt withhold, and the plan that parks every run-owned unlanded write in the record.
Imports no sibling ``halted_record_*`` module."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional

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


#: ``withheld`` reason for a run-owned ``.py`` whose bytes were moved into the halted record because
#: the tree would not import with them.
EVICTED_NOT_IMPORTABLE = "evicted-not-importable"
IMPORT_RISK_UNSCANNED = "import_risk_unscanned"

#: Halt kind of a verify that ran zero tests (`clean_overlay_verify.ZERO_RUN_TEST_INFRA`); never evicts.
TEST_INFRA_HALT = "test_infra_failure"

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


def _stem_index(paths: list) -> dict:
    """``{module stem: [paths]}``; a package ``__init__`` answers to its directory name."""
    stems: dict = {}
    for p in paths:
        pp = Path(p)
        stem = pp.parent.name if pp.stem == "__init__" else pp.stem
        if stem:
            stems.setdefault(stem, []).append(p)
    return stems


def _outside_link_gate(parked_py: list, parked_packages: dict) -> Callable[[str, bytes], bool]:
    """``(rel, data) -> bool``: False only for an outside file that provably links to no parked path.

    Every import target is built from dotted tokens in the text plus, for a relative import, the
    importer's own directory chain, so a parked module's stem (a package's directory name) must be a
    substring of the text or a directory of the importer. A file failing both skips the import regex and
    never enters the index, and the keep decision is unchanged. A root package or a non-word stem
    disables the gate."""
    stems = set(_stem_index(parked_py))
    if "" in parked_packages.values() or not stems or not all(
        s.isascii() and re.fullmatch(r"\w+", s) for s in stems
    ):
        return lambda rel, data: True
    needles = [s.encode() for s in sorted(stems)]  # substring, not word: a regex scan costs ~10x
    return lambda rel, data: not stems.isdisjoint(rel.split("/")[:-1]) or any(n in data for n in needles)


def _kept_by_outside_imports(root: Path, parked_py: list, outside: list, head) -> set:
    """Keep whole import units linked to dirty Python paths outside the eviction."""
    from coordinator_core.ops.dispatch_emit import test_reach
    from coordinator_core.ops.dispatch_emit.halt_tree_evict import _read
    from coordinator_core.ops.review_token_index import refresh_import_targets

    parked = set(parked_py)
    parked_packages = {
        p: p[:-len("__init__.py")] for p in parked if p == "__init__.py" or p.endswith("/__init__.py")
    }
    hit: set = set()
    base = len(str(root)) + 1
    admits = _outside_link_gate(parked_py, parked_packages)

    def read_linked(path: str) -> Optional[bytes]:
        data = _read(root, path[base:])
        return data if data is not None and admits(path[base:], data) else None

    indexed = refresh_import_targets(str(root), sorted(outside), read_linked)
    for q, imported in indexed.items():
        hit.update(imported & parked)
        hit.update(p for p, prefix in parked_packages.items()
                   if q.startswith(prefix) or any(dep.startswith(prefix) for dep in imported))

    outside_paths = set(outside)
    outside_modules = test_reach._module_map(outside_paths)
    for p in parked_py:
        # The reset may bring back an import that the current file removed.
        head_text = head.head_text(p)
        if (test_reach._imported_modules(root, p, outside_modules)
                or test_reach._package_inits(p, outside_paths)
                or (head_text and test_reach._imported_modules(root, p, outside_modules, text=head_text))):
            hit.add(p)
    for unit in _import_units(root, parked_py, head):
        if hit.intersection(unit):
            hit.update(unit)
    return hit


def _import_units(root: Path, own_py: list, head) -> list:
    """Transitive module-stem groups across live and reset bytes; omit singletons."""
    stems = _stem_index(own_py)
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
            text = ""
        reset = head.head_text(q) or ""
        for stem in set(re.findall(r"\w+", text + "\n" + reset)) & stems.keys():
            for p in stems[stem]:
                parent[find(p)] = find(q)
    groups: dict = {}
    for p in own_py:
        groups.setdefault(find(p), []).append(p)
    return [sorted(g) for g in groups.values() if len(g) > 1]


def _reset_changes_import_surface(current: bytes, base: Optional[bytes]) -> bool:
    """A reset can remove a binding or bring back an import absent from the live file."""
    import symtable

    from coordinator_core.ops.dispatch_emit.test_reach import _scan_imports

    if base is None:
        return True
    try:
        live = current.decode("utf-8")
        reset = base.decode("utf-8")
        names = [
            {symbol.get_name() for symbol in symtable.symtable(text, "<eviction>", "exec").get_symbols()
             if symbol.is_assigned() or symbol.is_imported()}
            for text in (live, reset)
        ]
        return bool(names[0] - names[1] or set(_scan_imports(reset)) - set(_scan_imports(live)))
    except (UnicodeError, SyntaxError, ValueError):
        return True


def plan_tree_eviction(
    root: Path, record_dir_rel: str, run_written: list, changed: set, write_set: list,
    withheld: dict, paths: list, run_id: Optional[str] = None, credited: Optional[set] = None,
    *, python_dirt: Optional[set[str]] = None, halt_kind: Optional[str] = None,
) -> Optional[TreeEviction]:
    """Park every run-owned, changed, unlanded path (any extension) in the halted record. A ``.py``
    that ``halt_tree_check.broken_paths`` judges unimportable (with its write-set importers) is named
    in ``withheld`` as ``EVICTED_NOT_IMPORTABLE``; every other parked path as ``EVICTED_PARKED``. Paths
    under the record dir and peer-owned paths (``_NOT_RUN_OWNED``) are never parked. The record's
    ``evicted.json`` plus its blobs are appended to ``paths`` (in place). A ``.py`` that a dirty ``.py``
    in ``changed`` outside the parked set (landed, peer-owned) names stays in the tree with its whole
    import unit, including when the run's scoped snapshot omitted the outside path: parking it would
    leave that importer importing nothing. Call after
    ``_withhold_run_dirt`` and before the nothing-to-commit return. None when nothing is parked, and always None
    for a ``test_infra_failure`` halt: the tests never judged the bytes (zero ran), so they stay in the tree.
    ``credited`` (when given) bounds eviction to the run's declared or touch-recorded paths: a path
    found only by the tree mtime sweep is a peer's live dirt until declared, and is never reset."""
    from coordinator_core.authoring_leaks import HeadView
    from coordinator_core.ops.dispatch_emit import halt_tree_check, halt_tree_evict, kept_work

    if halt_kind == TEST_INFRA_HALT or not (root / record_dir_rel).is_dir():
        return None
    landed = set(paths)
    own = [
        p for p in run_written
        if p in changed and p not in landed and withheld.get(p) not in _NOT_RUN_OWNED
        and not p.startswith(record_dir_rel + "/") and (credited is None or p in credited)
    ]
    if not own:
        return None
    own_py = [p for p in own if p.endswith(".py")]
    targets = own
    unowned: dict = {}
    prints: dict = {}
    python_bytes: dict[str, bytes] = {}
    for p in targets:
        try:
            data = (root / p).read_bytes()
            prints[p] = kept_work.blob_sha(data)
            if p.endswith(".py"):
                python_bytes[p] = data
        except OSError:
            continue
    parked_py = [p for p in own_py if p in prints]
    head = HeadView(root, prints)
    if any(_reset_changes_import_surface(python_bytes[p], head.head_bytes(p)) for p in parked_py):
        outside = [p for p in (python_dirt or set()) | changed
                   if p.endswith(".py") and p not in prints and not p.startswith(record_dir_rel + "/")]
        blocked = parked_py if python_dirt is None else (
            _kept_by_outside_imports(root, parked_py, outside, head) if outside else ()
        )
        for p in blocked:
            if python_dirt is None:
                withheld[p] = IMPORT_RISK_UNSCANNED
            del prints[p]
    if not prints:
        return None
    # Importability labels only describe paths that can actually be parked.
    checked_py = [p for p in parked_py if p in prints]
    broken = halt_tree_check.broken_paths(root, checked_py, write_set) if checked_py else {}
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
    eviction.units = _import_units(root, [p for p in own_py if p in prints], head)
    eviction.record_paths, eviction.paths = appended, paths
    return eviction
