"""Machine-local per-run index: what each run withholds, prints, declares and is bound to. One file per run.

Layout: `<repo>/.coordinator-local/halt-withheld/<run_id>.json` (git-ignored), keys `paths` (the paths a
still-halted run withholds), `sizing_rel`, `prints` (path -> blob id at the halt), `declared` (the run's
staged write footprint) and `at_ns` (when the entry was written), all repo-relative posix; the last four
are omitted when empty. Per-run files make a write or `remove_run` one atomic replace or unlink, so
concurrent peer sessions never lose each other's update. Not DoE's `state/withheld-paths` PM-hold
records. `entries` is the one reader: every run's entry from a single directory listing, running
`rebuild` once under the `.built-v2` sentinel so runs recorded before the fields existed enter it.
`<repo>/.coordinator-local/halt-withheld-cache.json` persists every parsed entry with the `(mtime_ns, size)` it
was parsed under, so a cold process reads one file and re-parses only the run files whose stat moved.
Stdlib plus the atomic writer; no subprocess, no git.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator, Mapping, NamedTuple, Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.git import call_io_scope
from coordinator_core.git.git_objects import PACK_READ_DEADLINE_SECS as READ_DEADLINE_SECS
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import STATUS_HALTED
from coordinator_core.ops.dispatch_emit.halted_run import HALTED_FILE, _read, _withheld_of

INDEX_DIR = ".coordinator-local/halt-withheld"
SENTINEL = ".built-v2"
_PRINTS_FILE = "withheld-prints.json"
_MANIFEST_FILE = "manifest.json"
_clock = time.monotonic  # one in-flight read cannot be pre-empted; the bound is between files


class RunEntry(NamedTuple):
    """One run's index entry; every field is empty/None when the run never recorded it."""

    paths: tuple
    sizing_rel: Optional[str]
    prints: dict
    declared: tuple
    at_ns: int


def _dir(repo_root: Path) -> Path:
    return Path(repo_root) / INDEX_DIR


def _norm(path: str) -> str:
    if not ("\\" in path or "//" in path or "/." in path or path.startswith(".") or path.endswith("/")):
        return path  # already canonical: skips the PurePosixPath build the hot loops would repeat
    posix = PurePosixPath(path.replace("\\", "/")).as_posix()
    return posix[2:] if posix.startswith("./") else posix


def _norm_all(paths: Iterable) -> tuple:
    return tuple(sorted({_norm(p) for p in paths if isinstance(p, str) and p}))


def _run_file(repo_root: Path, run_id: str) -> Path:
    if not safe_id(run_id):
        raise ValueError(f"unsafe run id: {run_id!r}")
    return _dir(repo_root) / f"{run_id}.json"


_PARSED = "halt-index-entry"
_FRESH = "halt-index-fresh"


_STAT_CACHE: dict = {}  # abs file path -> (st_mtime_ns, st_size, RunEntry | None); process-wide
# index dir path -> (dir st_mtime_ns, {run id: RunEntry}, unreadable run ids); process-wide. Valid while the dir mtime is
# unchanged: every writer replaces by rename or unlinks, which moves it. An in-place rewrite by a peer is
# not seen. A listing taken within the settle window of the dir mtime is not kept (coarse-clock race).
_SNAPSHOT: dict = {}
_SNAPSHOT_SETTLE_NS = 50_000_000


def _drop_snapshot(repo_root: Path) -> None:
    _SNAPSHOT.pop(os.fspath(_dir(repo_root)), None)


def _parse(file, st=None) -> Optional[RunEntry]:
    """The entry in `file`: parsed once per open `call_io_scope`, and across scopes while the file's
    (mtime_ns, size) is unchanged. `st` is a stat the caller already holds (a scandir entry's).
    `_store` and `remove_run` drop the path; a missing file reads None and is never cached."""
    key = os.fspath(file)
    return call_io_scope.memo(_PARSED, key, lambda: _parse_stat_keyed(file, key, st))


def _parse_stat_keyed(file, key: str, st) -> Optional[RunEntry]:
    try:
        st = st if st is not None else os.stat(key)
    except OSError:
        _STAT_CACHE.pop(key, None)
        return None
    sig = (st.st_mtime_ns, st.st_size)
    hit = _STAT_CACHE.get(key)
    if hit is not None and hit[:2] == sig:
        return hit[2]
    entry = _parse_file(file)
    _STAT_CACHE[key] = (*sig, entry)
    _PARSE_MISSES[0] += 1
    return entry


_CACHE_FILE = ".coordinator-local/halt-withheld-cache.json"
_PARSE_MISSES = [0]  # entries parsed from disk by this process; `entries` persists the cache when it moves
_LOADED: dict = {}  # repo root -> {index file name: [[mtime_ns, size], packed entry]}


def _cache_path(repo_root) -> str:
    return os.path.join(os.fspath(repo_root), _CACHE_FILE)


def _persisted(repo: str) -> dict:
    """The persisted cache rows, read once per process; empty when absent or malformed. Every use is gated
    by the index file's own `(mtime_ns, size)`, so a peer process's later write only costs a parse."""
    repo = str(Path(repo))
    held = _LOADED.get(repo)
    if held is None:
        held = {}
        try:
            with open(_cache_path(repo), "rb") as fh:
                data = json.loads(fh.read().decode("utf-8"))
            if isinstance(data, dict):
                held = data
        except (OSError, ValueError):
            pass
        _LOADED[repo] = held
    return held


def _pack(entry: RunEntry) -> list:
    return [list(entry.paths), entry.sizing_rel, entry.prints, list(entry.declared), entry.at_ns]


def _unpack(row) -> RunEntry:
    paths, sizing, prints, declared, at_ns = row
    return RunEntry(tuple(paths), sizing, dict(prints), tuple(declared), at_ns)


def _seed_from_persisted(root: str, index: str, listing: list) -> None:
    """Seed `_STAT_CACHE` from the persisted cache for each index file whose `(mtime_ns, size)` still
    matches, so a cold process opens one file instead of one per run. A missing or malformed cache seeds nothing."""
    try:
        rows = _persisted(root)
        for de in listing:
            row = rows.get(de.name)
            if row is None or not de.name.endswith(".json"):
                continue
            st = de.stat()
            if [st.st_mtime_ns, st.st_size] == row[0]:
                _STAT_CACHE.setdefault(os.path.join(index, de.name), (st.st_mtime_ns, st.st_size, _unpack(row[1])))
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return


def _persist(root: str, index: str) -> None:
    """Write every cached, readable index entry with the stat it was parsed under. Best effort: a failed
    write only leaves the next cold process parsing."""
    rows: dict = {}
    for key, hit in list(_STAT_CACHE.items()):
        if os.path.dirname(key) == index and key.endswith(".json") and hit[2] is not None:
            rows[os.path.basename(key)] = [[hit[0], hit[1]], _pack(hit[2])]
    _LOADED[str(Path(root))] = rows
    try:
        atomic_write_bytes(_cache_path(root), json.dumps(rows, separators=(",", ":")).encode("utf-8"))
    except OSError:
        pass


def _drop_cached(repo_root: Path, run_id: str) -> None:
    _STAT_CACHE.pop(os.fspath(_run_file(repo_root, run_id)), None)
    _drop_snapshot(repo_root)


def _parse_file(file) -> Optional[RunEntry]:
    try:
        data = json.loads(Path(file).read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    paths = data.get("paths")
    declared = data.get("declared")
    prints = data.get("prints")
    sizing = data.get("sizing_rel")
    at_ns = data.get("at_ns")
    return RunEntry(
        _norm_all(paths) if isinstance(paths, list) else (),
        sizing if isinstance(sizing, str) and sizing else None,
        {_norm(k): v for k, v in prints.items() if isinstance(k, str) and k and isinstance(v, str) and v}
        if isinstance(prints, dict)
        else {},
        _norm_all(declared) if isinstance(declared, list) else (),
        at_ns if isinstance(at_ns, int) and not isinstance(at_ns, bool) else 0,
    )


def _store(repo_root: Path, run_id: str, entry: RunEntry, *, bulk: bool = False) -> None:
    """Write `entry`, or drop the file when it holds neither paths nor declared. `bulk` (a `rebuild` of
    a directory made for it) creates the file by `_publish_bare` and seeds the parse cache with `entry`."""
    if not bulk:  # `rebuild` drops the scope's parses once, and a replaced file's cached stat itself
        call_io_scope.invalidate(_PARSED)
        _drop_cached(repo_root, run_id)
        forget_fresh()
    if not entry.paths and not entry.declared:
        remove_run(repo_root, run_id)
        return
    body: dict = {"paths": list(entry.paths)}
    if entry.sizing_rel:
        body["sizing_rel"] = entry.sizing_rel
    if entry.prints:
        body["prints"] = dict(sorted(entry.prints.items()))
    if entry.declared:
        body["declared"] = list(entry.declared)
    body["at_ns"] = entry.at_ns
    data = (json.dumps(body, separators=(",", ":")) + "\n").encode("utf-8")  # no indent: the C encoder, not the Python one
    file = _run_file(repo_root, run_id)
    st = _publish_bare(os.fspath(file), data) if bulk else None
    if st is not None:
        _STAT_CACHE[os.fspath(file)] = (st.st_mtime_ns, st.st_size, entry)  # a drifted stamp only re-parses
        return
    if bulk:  # a peer created the file after `rebuild` listed the dir: keep its paths (add-only)
        prior = _parse(file)
        if prior is not None:
            merged = tuple(dict.fromkeys((*prior.paths, *entry.paths)))
            _store(repo_root, run_id, entry._replace(paths=merged))
            return
    atomic_write_bytes(file, data)


def _publish_bare(file: str, data: bytes) -> Optional[os.stat_result]:
    """Create `file` holding `data` by one exclusive open, with none of `atomic_write_bytes`' temp, sweep
    or rename; its flushed `fstat`. None when the file exists or the directory is absent: the caller replaces atomically.
    Trap: the file is briefly partial. Safe only for `rebuild`, which runs while the sentinel is absent
    (`fresh_entry` reads no entry then) and whose other readers take a partial file as unreadable, so the
    run's marker is read in its place (`sibling_claims._candidates_read`)."""
    try:
        fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
    except OSError:  # exists, or the directory is absent: nothing of ours to remove
        return None
    try:
        view = memoryview(data)
        while view:  # `os.write` may write short
            view = view[os.write(fd, view):]
        return os.fstat(fd)
    except OSError:
        try:
            os.remove(file)  # our own partial file, never a peer's
        except OSError:
            pass
        return None
    finally:
        os.close(fd)


def _clean_sizing(sizing_rel: Optional[str]) -> Optional[str]:
    return _norm(sizing_rel) if isinstance(sizing_rel, str) and sizing_rel else None


def add_run(
    repo_root: Path,
    run_id: str,
    paths: Iterable[str],
    *,
    sizing_rel: Optional[str] = None,
    prints: Optional[Mapping[str, str]] = None,
    declared: Optional[Iterable[str]] = None,
    at_ns: Optional[int] = None,
) -> None:
    """Replace the run's `paths` (never merged). `sizing_rel`, `prints` and `declared` replace the
    stored value when given and keep it when None; `prints` keeps only keys in `paths`. The entry is
    removed when it ends with neither paths nor declared."""
    _add_run(repo_root, run_id, paths, sizing_rel, prints, declared, at_ns)


def _add_run(
    repo_root: Path, run_id: str, paths: Iterable[str], sizing_rel: Optional[str],
    prints: Optional[Mapping[str, str]], declared: Optional[Iterable[str]], at_ns: Optional[int],
    bulk: bool = False,
) -> None:
    prior = _parse(_run_file(repo_root, run_id)) if None in (sizing_rel, prints, declared) else None
    normed = _norm_all(paths)
    if prints is None:
        kept = prior.prints if prior else {}
    else:
        kept = {_norm(k): v for k, v in prints.items() if isinstance(k, str) and k and isinstance(v, str) and v}
    entry = RunEntry(
        normed,
        _clean_sizing(sizing_rel) if sizing_rel is not None else (prior.sizing_rel if prior else None),
        {k: v for k, v in kept.items() if k in normed},
        _norm_all(declared) if declared is not None else (prior.declared if prior else ()),
        at_ns if at_ns is not None else time.time_ns(),
    )
    _store(repo_root, run_id, entry, bulk=bulk)


def record_declared(
    repo_root: Path, run_id: str, declared: Iterable[str], *, sizing_rel: Optional[str] = None
) -> None:
    """Set the run's `declared` footprint (and `sizing_rel` when given), keeping its paths and prints.
    An empty footprint on a run with no paths removes the entry."""
    file = _run_file(repo_root, run_id)
    prior = _parse(file)
    entry = RunEntry(
        prior.paths if prior else (),
        _clean_sizing(sizing_rel) if sizing_rel is not None else (prior.sizing_rel if prior else None),
        prior.prints if prior else {},
        _norm_all(declared),
        prior.at_ns if prior and prior.at_ns else time.time_ns(),
    )
    _store(repo_root, run_id, entry)


def remove_run(repo_root: Path, run_id: str) -> None:
    """Drop the run's entry; absent is a no-op."""
    call_io_scope.invalidate(_PARSED)
    _drop_cached(repo_root, run_id)
    forget_fresh()
    try:
        _run_file(repo_root, run_id).unlink()
    except OSError:
        pass


def _read_prints(record_dir) -> dict:
    try:
        raw = call_io_scope.read_bytes(os.path.join(os.fspath(record_dir), _PRINTS_FILE))
        data = json.loads(raw.decode("utf-8")) if raw is not None else None
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _manifest_declared(record_dir) -> tuple:
    """The run's staged footprint from `manifest.json`, read through the open `call_io_scope` so the
    claim pass's own manifest read of the same file is a hit."""
    from coordinator_core.ops.dispatch_emit.live_run_writes import manifest_footprint

    try:
        raw = call_io_scope.read_bytes(os.path.join(os.fspath(record_dir), _MANIFEST_FILE))
        manifest = json.loads(raw.decode("utf-8")) if raw is not None else None
    except (OSError, ValueError):
        return ()
    return _norm_all(manifest_footprint(manifest)) if isinstance(manifest, dict) else ()


def rebuild(repo_root: Path) -> None:
    """Index every `halted` marker under `state/warp` that carries withheld paths or a staged footprint
    (paths, sizing_rel, prints from `withheld-prints.json`, declared from `manifest.json`, `at_ns` the
    marker's mtime), then write the sentinel. Add-only, so a halt recorded mid-scan is never undone."""
    root = Path(repo_root)
    warp = os.fspath(root / RECORD_DIR_ROOT)
    try:
        names = sorted(os.listdir(warp))
    except OSError:
        return
    try:
        existing = set(os.listdir(_dir(root)))  # names only
    except OSError:
        existing = set()
    for name in names:
        record_dir = os.path.join(warp, name)
        marker = _read(Path(record_dir))
        if marker is None or marker.get("status") != STATUS_HALTED or not safe_id(name):
            continue
        withheld = _withheld_of(marker)
        declared = _manifest_declared(record_dir)
        if not withheld and not declared:
            continue
        try:
            at_ns = os.stat(os.path.join(record_dir, HALTED_FILE)).st_mtime_ns
        except OSError:
            at_ns = 0
        sizing = marker.get("sizing_rel")
        if f"{name}.json" in existing:
            prior = _parse(_run_file(root, name))
            if prior is not None:
                withheld = tuple(dict.fromkeys((*prior.paths, *withheld)))
            _drop_cached(root, name)
        _add_run(
            root, name, withheld, sizing if isinstance(sizing, str) else "",
            _read_prints(record_dir), declared, at_ns, bulk=True,
        )
    call_io_scope.invalidate(_PARSED)
    forget_fresh()
    _drop_snapshot(root)
    atomic_write_bytes(_dir(root) / SENTINEL, b"")
    for _ in _scan(_dir(root)):  # parses what the bulk write left unkeyed
        pass
    _persist(os.fspath(root), os.fspath(_dir(root)))


def entries(repo_root: Path, unreadable: Optional[set] = None) -> dict:
    """`{run id: RunEntry}` over every readable run file, from one directory listing; a malformed file
    is skipped. Runs `rebuild` once when the sentinel is absent. Cost is one parse per run file, and a
    declared-only entry is dropped only by `record_declared([])`/`remove_run`, so callers must not assume
    the listing is bounded to live runs. `unreadable`, when given, receives the run ids of the files that
    did not parse."""
    root = Path(repo_root)
    index = _dir(root)
    key = os.fspath(index)
    snap = _SNAPSHOT.get(key)
    if snap is not None:
        try:
            if os.stat(key).st_mtime_ns == snap[0]:
                if unreadable is not None:
                    unreadable.update(snap[2])
                return dict(snap[1])  # the sentinel was present when this was kept; its removal moves the mtime
        except OSError:
            pass
        _SNAPSHOT.pop(key, None)
    if not (index / SENTINEL).exists():
        rebuild(root)
    try:
        dir_ns = os.stat(key).st_mtime_ns
    except OSError:
        return {}
    out: dict = {}
    bad: set = set()
    state: list = []
    misses = _PARSE_MISSES[0]
    for run_id, entry in _scan(index, state):
        if entry is not None:
            out[run_id] = entry
        else:
            bad.add(run_id)
    if unreadable is not None:
        unreadable.update(bad)
    if not state and time.time_ns() - dir_ns > _SNAPSHOT_SETTLE_NS:
        _SNAPSHOT[key] = (dir_ns, dict(out), frozenset(bad))
    else:
        _SNAPSHOT.pop(key, None)
    if _PARSE_MISSES[0] != misses:  # after the settle decision: the write must not age the listing
        _persist(os.path.dirname(os.path.dirname(key)), key)
    return out


def _scan(index: Path, state: Optional[list] = None) -> Iterator[tuple]:
    """`(run id, entry)` per `*.json` in `index`, from one scandir whose entries carry the stat the
    parse cache keys on; stops at the read deadline (a stalled disk: the rest read as unreadable),
    appending "truncated" to `state` when it does."""
    try:
        listing = list(os.scandir(index))
    except OSError:
        return
    root = os.fspath(index)
    live = {os.path.join(root, d.name) for d in listing}
    for stale in [k for k in _STAT_CACHE if os.path.dirname(k) == root and k not in live]:
        _STAT_CACHE.pop(stale, None)  # a file deleted out of band would otherwise stay cached forever
    repo = os.path.dirname(os.path.dirname(root))
    _seed_from_persisted(repo, root, listing)
    deadline = _clock() + READ_DEADLINE_SECS
    for de in sorted(listing, key=lambda d: d.name):
        name = de.name
        if name.endswith(".json"):
            if _clock() >= deadline:
                if state is not None:
                    state.append("truncated")
                return
            try:
                st = de.stat()
            except OSError:
                continue
            yield name[: -len(".json")], _parse(os.path.join(root, name), st)


def run_entry(repo_root: Path, run_id: str) -> Optional[RunEntry]:
    """The run's index entry (paths, prints, declared), or None when absent or unreadable."""
    return _parse(_run_file(repo_root, run_id))


def fresh_entry(repo_root: Path, run_id: str) -> Optional[RunEntry]:
    """The run's entry when it names withheld paths and `halted.json` has not been rewritten since the
    entry was written (marker mtime < `at_ns`), so the marker still reads `halted` with those paths.
    None on a missing sentinel, entry or marker, or a rewritten marker: the caller opens the marker.
    Trap: never proof a marker is readable by anything but its own parse; a corrupt marker with a
    fresh entry is answered from the entry."""
    if not safe_id(run_id):
        return None
    root = os.fspath(repo_root)
    return call_io_scope.memo(_FRESH, (root, run_id), lambda: _fresh_entry(root, run_id))


def forget_fresh() -> None:
    """Drop the scope's `fresh_entry` answers: a run's marker or index entry was just rewritten."""
    call_io_scope.invalidate(_FRESH)


def _fresh_entry(root: str, run_id: str) -> Optional[RunEntry]:
    index = os.fspath(_dir(Path(root)))  # same spelling as `entries`, so the memoised parse is shared
    try:
        if not call_io_scope.memo(_PARSED, "sentinel:" + index, lambda: os.path.exists(os.path.join(index, SENTINEL))):
            return None
        entry = _parse(os.path.join(index, run_id + ".json"))
        if entry is None or not entry.paths:
            return None
        if os.stat(os.path.join(root, RECORD_DIR_ROOT, run_id, HALTED_FILE)).st_mtime_ns >= entry.at_ns:
            return None
        return entry
    except (OSError, ValueError):
        return None


def run_ids(repo_root: Path) -> list[str]:
    """Sorted run ids whose entry holds withheld paths; empty when the index is unreadable. A run
    indexed only for its declared footprint is not listed."""
    return [run_id for run_id, entry in _scan(_dir(Path(repo_root))) if entry is not None and entry.paths]


def withheld_by_path(repo_root: Path) -> dict[str, str]:
    """`{repo-relative posix path: run id}` over every indexed run's withheld paths; a path held by two
    runs maps to the lexically last run id. Runs `rebuild` once when the sentinel is absent."""
    out: dict[str, str] = {}
    for run_id, entry in entries(repo_root).items():
        for path in entry.paths:
            out[path] = run_id
    return out
