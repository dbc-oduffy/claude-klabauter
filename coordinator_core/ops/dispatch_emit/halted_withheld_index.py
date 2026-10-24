"""Machine-local index of the paths still-halted runs withhold: one file per run.

Layout: `<repo>/.coordinator-local/halt-withheld/<run_id>.json` (git-ignored), `{"paths": [...]}`
of repo-relative posix paths. Per-run files make `add_run` / `remove_run` a single atomic write or
unlink, so concurrent peer sessions never lose each other's update. Not DoE's `state/withheld-paths`
PM-hold records. `withheld_by_path` runs `rebuild` once, guarded by the `.built` sentinel, so halts
recorded before the index existed enter it. Stdlib plus the atomic writer; no subprocess, no git.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from typing import Iterable

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import STATUS_HALTED
from coordinator_core.ops.dispatch_emit.halted_run import HALTED_FILE, _read, halted_withheld

INDEX_DIR = ".coordinator-local/halt-withheld"
SENTINEL = ".built"


def _dir(repo_root: Path) -> Path:
    return Path(repo_root) / INDEX_DIR


def _norm(path: str) -> str:
    posix = PurePosixPath(path.replace("\\", "/")).as_posix()
    return posix[2:] if posix.startswith("./") else posix


def _run_file(repo_root: Path, run_id: str) -> Path:
    if not safe_id(run_id):
        raise ValueError(f"unsafe run id: {run_id!r}")
    return _dir(repo_root) / f"{run_id}.json"


def add_run(repo_root: Path, run_id: str, paths: Iterable[str]) -> None:
    """Replace the run's entry with `paths` (never merged); an empty set removes the entry."""
    normed = sorted({_norm(p) for p in paths if isinstance(p, str) and p})
    if not normed:
        remove_run(repo_root, run_id)
        return
    payload = json.dumps({"paths": normed}, indent=2) + "\n"
    atomic_write_bytes(_run_file(repo_root, run_id), payload.encode("utf-8"))


def remove_run(repo_root: Path, run_id: str) -> None:
    """Drop the run's entry; absent is a no-op."""
    try:
        _run_file(repo_root, run_id).unlink()
    except OSError:
        pass


def rebuild(repo_root: Path) -> None:
    """Add every `halted` marker under `state/warp` that carries a non-empty withheld set, then
    write the sentinel. Add-only, so a halt recorded mid-scan is never undone."""
    root = Path(repo_root)
    warp = root / RECORD_DIR_ROOT
    try:
        names = sorted(os.listdir(warp))
    except OSError:
        return
    for name in names:
        record_dir = warp / name
        marker = _read(record_dir) if (record_dir / HALTED_FILE).is_file() else None
        if marker is None or marker.get("status") != STATUS_HALTED:
            continue
        withheld = halted_withheld(record_dir)
        if withheld:
            add_run(root, name, withheld)
    atomic_write_bytes(_dir(root) / SENTINEL, b"")


def _paths_of(file: Path) -> list[str]:
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw = data.get("paths") if isinstance(data, dict) else None
    return [p for p in raw if isinstance(p, str)] if isinstance(raw, list) else []


def withheld_by_path(repo_root: Path) -> dict[str, str]:
    """`{repo-relative posix path: run id}` over every indexed run; a path held by two runs maps to
    the lexically last run id. Runs `rebuild` once when the sentinel is absent."""
    root = Path(repo_root)
    index = _dir(root)
    if not (index / SENTINEL).exists():
        rebuild(root)
    try:
        names = sorted(os.listdir(index))
    except OSError:
        return {}
    out: dict[str, str] = {}
    for name in names:
        if not name.endswith(".json"):
            continue
        run_id = name[: -len(".json")]
        for path in _paths_of(index / name):
            out[path] = run_id
    return out
