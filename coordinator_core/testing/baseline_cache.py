"""baseline-cache: one read-only extracted tree per commit, shared by every baseline run.

Entry: <toplevel>/scratch/baseline-cache/<sha40>/, built in <sha40>.partial-<pid> and published by
`os.replace` (a lost race discards the partial). Names avoid `test-base-`, so the export reapers
never touch the cache; it is NOT counted by `export_lifecycle.check_export_cap` and is bounded
only by CACHE_ENTRY_CAP entries (least recently used, by the `<sha40>.used` touch file, go first).
A run holds `<sha40>.lease-<pid>` for its whole life; an entry with a live lease is never evicted.
The extractor is injected: this module spawns no git.

Traps:
- DATA_DIRS links point at the LIVE tree. Every walk here stops at a link (`_is_link`); following
  one would mark live files read-only or delete them.
- Windows ignores the read-only attribute on directories and POSIX leaves them writable, so a
  runner can still CREATE a file in the entry. `check_drift` compares every directory's
  `st_mtime_ns` to the value recorded at publish; any new or removed file changes its parent's.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from coordinator_core.testing.baseline_export import _is_link, _make_writable, remove

CACHE_ENTRY_CAP = 3
CACHE_DIRNAME = "baseline-cache"
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_STALE_RE = re.compile(r"([0-9a-f]{40})\.(?:partial|evicting|dirs\.tmp)-(\d+)")
_MANIFEST_WAIT_S = 3.0
_NO_WRITE = ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)

_lease_lock = threading.Lock()
_leases_held: dict[Path, int] = {}
_last_touch_ns = 0


def cache_root(toplevel: os.PathLike[str] | str) -> Path:
    """<toplevel>/scratch/baseline-cache."""
    return Path(toplevel) / "scratch" / CACHE_DIRNAME


def entry_path(toplevel: os.PathLike[str] | str, sha40: str) -> Path:
    return cache_root(toplevel) / sha40


def _pid_alive(pid: int) -> bool | None:
    from coordinator_core.testing import suite_mutex

    return suite_mutex._pid_alive(pid)


def _touch_used(entry: Path) -> None:
    """Stamp the entry's recency; strictly increasing so LRU order survives coarse filesystem clocks."""
    global _last_touch_ns
    used = entry.parent / f"{entry.name}.used"
    with _lease_lock:
        _last_touch_ns = max(time.time_ns(), _last_touch_ns + 1)
        stamp = _last_touch_ns
    with contextlib.suppress(OSError):
        if not used.exists():
            used.touch()
        os.utime(used, ns=(stamp, stamp))


def _used_ns(root: Path, sha: str) -> int:
    try:
        return os.stat(root / f"{sha}.used").st_mtime_ns
    except OSError:
        return 0


def _walk(tree: Path) -> Iterator[tuple[str, list[str]]]:
    """(directory, regular-file paths) for `tree`, never descending or yielding a link."""
    for cur, dirnames, filenames in os.walk(tree, topdown=True, followlinks=False):
        dirnames[:] = [d for d in dirnames if not _is_link(os.path.join(cur, d))]
        yield cur, [os.path.join(cur, f) for f in filenames if not _is_link(os.path.join(cur, f))]


def _seal(tree: Path) -> None:
    """Mark every regular file read-only."""
    for _cur, files in _walk(tree):
        for path in files:
            with contextlib.suppress(OSError):
                os.chmod(path, os.stat(path).st_mode & _NO_WRITE)


def _unseal_and_remove(path: Path) -> bool:
    for cur, files in _walk(path):
        _make_writable(cur)
        for f in files:
            _make_writable(f)
    return remove(path)


def _dir_mtimes(entry: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    for cur, _files in _walk(entry):
        out[Path(os.path.relpath(cur, entry)).as_posix()] = os.stat(cur).st_mtime_ns
    return out


def _manifest(entry: Path) -> Path:
    return entry.parent / f"{entry.name}.dirs"


def _write_manifest(entry: Path) -> None:
    tmp = entry.parent / f"{entry.name}.dirs.tmp-{os.getpid()}"
    tmp.write_text(json.dumps(_dir_mtimes(entry)), encoding="utf-8")
    os.replace(tmp, _manifest(entry))


def check_drift(entry: os.PathLike[str] | str) -> bool:
    """True when the entry no longer matches what was published (or cannot be verified)."""
    entry = Path(entry)
    try:
        recorded = json.loads(_manifest(entry).read_text(encoding="utf-8"))
        return _dir_mtimes(entry) != recorded
    except (OSError, ValueError):
        return True


@contextlib.contextmanager
def lease(entry: os.PathLike[str] | str) -> Iterator[Path]:
    """Hold <entry>.lease-<pid>; take it BEFORE `ensure_entry` so an evictor cannot slip between hit and lease."""
    entry = Path(entry)
    path = entry.parent / f"{entry.name}.lease-{os.getpid()}"
    entry.parent.mkdir(parents=True, exist_ok=True)
    with _lease_lock:
        if _leases_held.get(path, 0) == 0:
            path.touch()
        _leases_held[path] = _leases_held.get(path, 0) + 1
    try:
        yield path
    finally:
        with _lease_lock:
            _leases_held[path] -= 1
            if _leases_held[path] == 0:
                del _leases_held[path]
                with contextlib.suppress(OSError):
                    path.unlink()


def _live_leases(root: Path, sha: str, ignore_pid: int | None = None) -> list[Path]:
    """Lease files of `sha` whose pid may be alive; dead-pid leases are deleted."""
    live: list[Path] = []
    prefix = f"{sha}.lease-"
    for name in _listdir(root):
        if not name.startswith(prefix) or not name[len(prefix):].isdigit():
            continue
        if int(name[len(prefix):]) == ignore_pid:
            continue
        if _pid_alive(int(name[len(prefix):])) is False:
            with contextlib.suppress(OSError):
                (root / name).unlink()
        else:
            live.append(root / name)
    return live


def _listdir(root: Path) -> list[str]:
    try:
        return os.listdir(root)
    except OSError:
        return []


def _sweep_dead(root: Path) -> None:
    """Remove `.partial-`/`.evicting-`/`.dirs.tmp-` leftovers of dead pids."""
    for name in _listdir(root):
        m = _STALE_RE.fullmatch(name)
        if m and int(m.group(2)) != os.getpid() and _pid_alive(int(m.group(2))) is False:
            if (root / name).is_dir():
                _unseal_and_remove(root / name)
            else:
                with contextlib.suppress(OSError):
                    (root / name).unlink()


def _evict_one(root: Path, sha: str, ignore_pid: int | None = None) -> bool:
    """Rename the entry aside, then re-check leases: a run that leased after the first check wins."""
    entry = root / sha
    aside = root / f"{sha}.evicting-{os.getpid()}"
    try:
        os.replace(entry, aside)
    except OSError:
        return False
    if _live_leases(root, sha, ignore_pid):
        try:
            os.rename(aside, entry)
        except OSError:
            _unseal_and_remove(aside)
        return False
    _unseal_and_remove(aside)
    for suffix in (".used", ".dirs"):
        with contextlib.suppress(OSError):
            (root / f"{sha}{suffix}").unlink()
    return True


def evict(toplevel: os.PathLike[str] | str, keep: str) -> list[str]:
    """Evict least-recently-used unleased entries, never `keep`, until at or under CACHE_ENTRY_CAP; returns shas removed."""
    root = cache_root(toplevel)
    shas = [n for n in _listdir(root) if _SHA_RE.fullmatch(n) and (root / n).is_dir()]
    over = len(shas) - CACHE_ENTRY_CAP
    removed: list[str] = []
    for sha in sorted((s for s in shas if s != keep), key=lambda s: (_used_ns(root, s), s)):
        if over <= 0:
            break
        if not _live_leases(root, sha) and _evict_one(root, sha):
            removed.append(sha)
            over -= 1
    return removed


def _published(entry: Path) -> bool:
    """The entry exists with its manifest; a bare entry is a publisher mid-step, awaited briefly, else broken."""
    deadline = time.monotonic() + _MANIFEST_WAIT_S
    while True:
        if not entry.is_dir():
            return False
        if _manifest(entry).exists():
            return True
        if time.monotonic() >= deadline:
            _evict_one(entry.parent, entry.name, ignore_pid=os.getpid())
            return False
        time.sleep(0.05)


def ensure_entry(
    toplevel: os.PathLike[str] | str, sha40: str, extract: Callable[[Path], None]
) -> tuple[Path, bool]:
    """(entry, built_now): the read-only tree for `sha40`, calling `extract(partial_dir)` only on a miss."""
    root = cache_root(toplevel)
    root.mkdir(parents=True, exist_ok=True)
    _sweep_dead(root)
    entry = root / sha40
    if _published(entry):
        _touch_used(entry)
        return entry, False
    partial = root / f"{sha40}.partial-{os.getpid()}"
    if os.path.lexists(partial):
        _unseal_and_remove(partial)
    partial.mkdir()
    try:
        extract(partial)
        _seal(partial)
    except BaseException:
        _unseal_and_remove(partial)
        raise
    try:
        os.replace(partial, entry)
    except OSError:
        _unseal_and_remove(partial)
        if entry.is_dir():
            _touch_used(entry)
            return entry, False
        raise
    _write_manifest(entry)
    _touch_used(entry)
    evict(toplevel, keep=sha40)
    return entry, True
