"""Materialise a git clone of the code roots at one commit and run an entry from it, so a
publish or percolate round never reads the live working tree. Stdlib only: callers load this
before the engine is bound."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_MARKER = "commit-clone.json"
_CACHE_DIRNAME = "claude-klabauter-commit-clone"
_RETENTION = 3
_SPARSE_ROOTS = ("coordinator", "coordinator_core", "setup")
_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def default_cache_root() -> Path:
    """`<managed temp>/claude-klabauter-commit-clone`; `temp_layout` is loaded by path (stdlib-only)."""
    path = Path(__file__).resolve().parents[3] / "coordinator_core" / "temp_layout.py"
    spec = importlib.util.spec_from_file_location("_commit_clone_temp_layout", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.managed_tempdir() / _CACHE_DIRNAME


def _read_marker(root: Path) -> dict | None:
    try:
        data = json.loads((root / ".git" / _MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def is_commit_clone(root: Path) -> bool:
    """True when `root` is a clone this module materialised (marker inside its `.git`)."""
    return _read_marker(Path(root)) is not None


def source_repo_root(root: Path) -> Path:
    """The live repo a commit clone was cloned from; `root` itself when it is not a clone.
    Read from the marker: the clone's alternates path sits two levels below the repo."""
    marker = _read_marker(Path(root))
    if marker and marker.get("source_root"):
        return Path(marker["source_root"])
    return Path(root)


def _git(cwd: Path, *args: str) -> None:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", creationflags=_NO_WINDOW,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()}")


def materialise_commit_clone(repo_root: Path, sha: str, cache_root: Path) -> Path:
    """`cache_root/<sha>`: a shared sparse clone of `repo_root` (code roots only) detached at
    `sha`. A warm hit spawns nothing; a cold build spawns three git processes. Raises
    RuntimeError when git refuses (unknown sha)."""
    repo_root, cache_root = Path(repo_root), Path(cache_root)
    dest = cache_root / sha
    marker = _read_marker(dest)
    if marker is not None and marker.get("sha") == sha:
        return dest
    cache_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{sha[:12]}-", dir=cache_root))
    try:
        _git(
            cache_root, "clone", "--shared", "--no-checkout", "--sparse", "--quiet",
            str(repo_root), str(staging),
        )
        _git(staging, "sparse-checkout", "set", "--cone", *_SPARSE_ROOTS)
        _git(staging, "checkout", "--quiet", "--detach", sha)
        (staging / ".git" / _MARKER).write_text(
            json.dumps({"sha": sha, "source_root": str(repo_root)}), encoding="utf-8"
        )
        try:
            os.replace(staging, dest)
        except OSError:
            if _read_marker(dest) is None:
                raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    _prune(cache_root, keep=dest)
    return dest


def _prune(cache_root: Path, keep: Path) -> None:
    """Remove sha-named clones beyond the newest `_RETENTION`; `keep` and staging dirs stay."""
    try:
        clones = [
            p for p in cache_root.iterdir()
            if p != keep and _FULL_SHA_RE.fullmatch(p.name) and p.is_dir()
        ]
        clones.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for stale in clones[max(_RETENTION - 1, 0):]:
        shutil.rmtree(stale, ignore_errors=True)


def run_entry_from_clone(clone: Path, entry_rel: str, argv: list[str]) -> int:
    """Run `clone/entry_rel` as `__main__` with the clone's roots first on `sys.path` and no
    live-tree path or cached `coordinator_core` module left to shadow it. Returns the exit code."""
    clone = Path(clone)
    entry = clone / entry_rel
    live = {Path(sys.argv[0]).resolve().parent} if sys.argv and sys.argv[0] else set()
    live.add(source_repo_root(clone) / "coordinator" / "bin")
    live.add(source_repo_root(clone))
    live.add(Path(__file__).resolve().parents[1])
    live.add(Path(__file__).resolve().parents[3])
    drop = {str(p) for p in live}
    clone_bin = clone / "coordinator" / "bin"
    sys.path[:] = [p for p in sys.path if p not in drop]
    sys.path[:0] = [str(clone_bin), str(clone)]
    for name in [m for m in sys.modules if m == "coordinator_core" or m.startswith("coordinator_core.")]:
        del sys.modules[name]
    print(f"running from commit clone {clone}", file=sys.stderr)
    saved_argv = sys.argv
    sys.argv = [str(entry), *argv[1:]] if argv else [str(entry)]
    try:
        runpy.run_path(str(entry), run_name="__main__")
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    finally:
        sys.argv = saved_argv
    return 0
