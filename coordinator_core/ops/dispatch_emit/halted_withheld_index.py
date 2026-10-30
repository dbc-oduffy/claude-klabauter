"""Machine-local per-run index: what each run withholds, prints, declares and is bound to. One file per run.

Layout: `<repo>/.coordinator-local/halt-withheld/<run_id>.json` (git-ignored), keys `paths` (the paths a
still-halted run withholds), `sizing_rel`, `prints` (path -> blob id at the halt), `declared` (the run's
staged write footprint) and `at_ns` (when the entry was written), all repo-relative posix; the last four
are omitted when empty. Per-run files make a write or `remove_run` one atomic replace or unlink, so
concurrent peer sessions never lose each other's update. Not DoE's `state/withheld-paths` PM-hold
records. `entries` is the one reader: every run's entry from a single directory listing, running
`rebuild` once under the `.built-v2` sentinel so runs recorded before the fields existed enter it.
Stdlib plus the atomic writer; no subprocess, no git.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, NamedTuple, Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import STATUS_HALTED
from coordinator_core.ops.dispatch_emit.halted_run import HALTED_FILE, _read, _withheld_of

INDEX_DIR = ".coordinator-local/halt-withheld"
SENTINEL = ".built-v2"
_PRINTS_FILE = "withheld-prints.json"
_MANIFEST_FILE = "manifest.json"


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


def _parse(file: Path) -> Optional[RunEntry]:
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
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


def _store(repo_root: Path, run_id: str, entry: RunEntry) -> None:
    """Write `entry`, or drop the file when it holds neither paths nor declared."""
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
    atomic_write_bytes(_run_file(repo_root, run_id), (json.dumps(body, indent=2) + "\n").encode("utf-8"))


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
    _store(repo_root, run_id, entry)


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
    try:
        _run_file(repo_root, run_id).unlink()
    except OSError:
        pass


def _read_prints(record_dir: Path) -> dict:
    try:
        data = json.loads((record_dir / _PRINTS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _manifest_declared(record_dir: Path) -> tuple:
    from coordinator_core.ops.dispatch_emit.live_run_writes import manifest_footprint

    try:
        manifest = json.loads((record_dir / _MANIFEST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    return _norm_all(manifest_footprint(manifest)) if isinstance(manifest, dict) else ()


def rebuild(repo_root: Path) -> None:
    """Index every `halted` marker under `state/warp` that carries withheld paths or a staged footprint
    (paths, sizing_rel, prints from `withheld-prints.json`, declared from `manifest.json`, `at_ns` the
    marker's mtime), then write the sentinel. Add-only, so a halt recorded mid-scan is never undone."""
    root = Path(repo_root)
    warp = root / RECORD_DIR_ROOT
    try:
        names = sorted(os.listdir(warp))
    except OSError:
        return
    for name in names:
        record_dir = warp / name
        marker = _read(record_dir) if (record_dir / HALTED_FILE).is_file() else None
        if marker is None or marker.get("status") != STATUS_HALTED or not safe_id(name):
            continue
        withheld = _withheld_of(marker)
        declared = _manifest_declared(record_dir)
        if not withheld and not declared:
            continue
        try:
            at_ns = (record_dir / HALTED_FILE).stat().st_mtime_ns
        except OSError:
            at_ns = 0
        sizing = marker.get("sizing_rel")
        prior = _parse(_run_file(root, name))
        if prior is not None:
            withheld = tuple(dict.fromkeys((*prior.paths, *withheld)))
        add_run(
            root, name, withheld,
            sizing_rel=sizing if isinstance(sizing, str) else "",
            prints=_read_prints(record_dir), declared=declared, at_ns=at_ns,
        )
    atomic_write_bytes(_dir(root) / SENTINEL, b"")


def entries(repo_root: Path) -> dict:
    """`{run id: RunEntry}` over every readable run file, from one directory listing; a malformed file
    is skipped. Runs `rebuild` once when the sentinel is absent. Cost is one parse per run file, and a
    declared-only entry is dropped only by `record_declared([])`/`remove_run`, so callers must not assume
    the listing is bounded to live runs."""
    root = Path(repo_root)
    index = _dir(root)
    if not (index / SENTINEL).exists():
        rebuild(root)
    try:
        names = sorted(os.listdir(index))
    except OSError:
        return {}
    out: dict = {}
    for name in names:
        if name.endswith(".json"):
            entry = _parse(index / name)
            if entry is not None:
                out[name[: -len(".json")]] = entry
    return out


def run_ids(repo_root: Path) -> list[str]:
    """Sorted run ids whose entry holds withheld paths; empty when the index is unreadable. A run
    indexed only for its declared footprint is not listed."""
    root = Path(repo_root)
    try:
        names = sorted(os.listdir(_dir(root)))
    except OSError:
        return []
    out = []
    for name in names:
        if name.endswith(".json"):
            entry = _parse(_dir(root) / name)
            if entry is not None and entry.paths:
                out.append(name[: -len(".json")])
    return out


def withheld_by_path(repo_root: Path) -> dict[str, str]:
    """`{repo-relative posix path: run id}` over every indexed run's withheld paths; a path held by two
    runs maps to the lexically last run id. Runs `rebuild` once when the sentinel is absent."""
    out: dict[str, str] = {}
    for run_id, entry in entries(repo_root).items():
        for path in entry.paths:
            out[path] = run_id
    return out
