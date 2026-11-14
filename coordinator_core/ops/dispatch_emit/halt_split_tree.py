"""Halt-time split-tree detector and live G-EM alert.

A package is split when its installed dependency tree (`node_modules/.package-lock.json`) is newer
than its build entry point, so the built artifact imports a tree it was not built against. Stat-only:
no npm, no tsc, no git. Heuristic: a later no-op `npm install` that touches the lockfile reads as split."""

from __future__ import annotations

import itertools
import json
import os
from pathlib import Path

#: Run/record/doc corpora grow without bound and never hold a buildable package.
_SKIP_DIRS = frozenset(
    {"node_modules", "state", "scratch", "archive", "docs", "tmp", "dist", "coordinator-sessions", "tasks", "__pycache__"}
)
_MAX_DEPTH = 2
#: Per-directory caps: total visits stay <= 1 + 32 + 32*32 whatever the tree holds. Trade-off: a directory
#: with more than `_MAX_ENTRIES_SCANNED` entries is judged on its first entries in scandir order, and every
#: dot-directory is skipped, so a package under `.tools/` is never detected.
_MAX_ENTRIES_SCANNED = 256
_MAX_DIRS_PER_LEVEL = 32
_DEFAULT_ENTRY = "dist/index.js"
_LOCK_REL = Path("node_modules") / ".package-lock.json"

#: Test seam: a callable `(holder_name, text)` replacing the detached unimatrix-send spawn.
_ALERT_SENDER = None


def _check_package(pkg_dir: Path) -> dict | None:
    lock = pkg_dir / _LOCK_REL
    try:
        tree_mtime = os.stat(lock).st_mtime
        with open(pkg_dir / "package.json", encoding="utf-8") as fh:
            pkg = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(pkg, dict):
        return None
    scripts = pkg.get("scripts")
    if not isinstance(scripts, dict) or not scripts.get("build"):
        return None
    main = pkg.get("main")
    entry = main if isinstance(main, str) and main else _DEFAULT_ENTRY
    try:
        build_mtime = os.stat(pkg_dir / entry).st_mtime
    except OSError:
        return None
    if tree_mtime <= build_mtime:
        return None
    name = pkg.get("name")
    return {
        "package_dir": pkg_dir.as_posix(),
        "package_name": name if isinstance(name, str) else "",
        "tree_mtime": tree_mtime,
        "build_mtime": build_mtime,
    }


def _walk(directory: Path, depth: int, out: list[dict]) -> None:
    hit = _check_package(directory)
    if hit is not None:
        out.append(hit)
    if depth >= _MAX_DEPTH:
        return
    try:
        with os.scandir(directory) as it:
            subdirs = sorted(
                e.path
                for e in itertools.islice(it, _MAX_ENTRIES_SCANNED)
                if e.name not in _SKIP_DIRS and not e.name.startswith(".") and e.is_dir(follow_symlinks=False)
            )[:_MAX_DIRS_PER_LEVEL]
    except OSError:
        return
    for sub in subdirs:
        _walk(Path(sub), depth + 1, out)


def find_split_trees(root: Path) -> list[dict]:
    """Packages under `root` (itself plus two levels) whose dependency lockfile is newer than the build
    entry point; each `{package_dir, package_name, tree_mtime, build_mtime}`. Never raises."""
    out: list[dict] = []
    try:
        _walk(Path(root), 0, out)
    except Exception:  # noqa: BLE001 -- detection is best-effort; the marker is the truth
        return []
    root_p = Path(root)
    for hit in out:
        try:
            hit["package_dir"] = Path(hit["package_dir"]).relative_to(root_p).as_posix()
        except ValueError:
            pass
    return out


def _alert_text(run_id: str, splits: list[dict]) -> str:
    parts = [f"{s['package_dir']} ({s['package_name']})" if s.get("package_name") else s["package_dir"] for s in splits]
    return f"LIVE BREAKAGE: run {run_id} halted with dependency tree newer than build in {', '.join(parts)}. Rebuild or restore."


def alert_live_gem(root: Path, run_id: str, splits: list[dict]) -> None:
    """Message the live Group EM about `splits`; no-op without a live holder. Never raises.

    Skipped under pytest unless `_ALERT_SENDER` is set, so a fixture halt never starts a real process."""
    try:
        from coordinator_core.group_em import nomination

        verdict = nomination.identify(str(root))
        if verdict.get("state") != "live":
            return
        name = (verdict.get("holder") or {}).get("name")
        if not name:
            return
        text = _alert_text(run_id, splits)
        if _ALERT_SENDER is not None:
            _ALERT_SENDER(name, text)
            return
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return
        from coordinator_core.ops.ceremony.detached_spawn import spawn_detached

        engine_root = Path(__file__).resolve().parents[3]
        script = engine_root / "coordinator" / "bin" / "unimatrix-send.py"
        spawn_detached(str(engine_root), str(script), ["--to", name, text])
    except Exception:  # noqa: BLE001 -- alerting is best-effort; the marker is the truth
        pass
