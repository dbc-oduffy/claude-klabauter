"""Which live Claude Code sessions loaded their plugin from a given plugin cache path.

A session binds the coordinator plugin at launch; deleting or rewriting that tree strands it.
`write_binding` records, per session, the CLAUDE_PLUGIN_ROOT it started with (SessionStart hook);
`sessions_bound_to` joins those bindings to `harness_registry.snapshot()` and
`core.stable_pid_alive` to name the live sessions under a cache path.

Negative spec: no process-list scan, no child process, no third liveness read, no waiter that
deletes on exit. A caller that finds an empty list deletes on that run; otherwise it holds.
"""
from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from coordinator_core._settings_home import claude_config_dir
from coordinator_core.install.write_surface import (
    ShapedClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)
from coordinator_core.session import core, harness_registry

BINDINGS_DIRNAME = ".coordinator-session-plugin-roots"

WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="plugin-cache-sessions",
    source_module="coordinator_core.install.plugin_cache_sessions",
    clauses=(
        ShapedClause(
            discovered_by="write_binding",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path=f"<claude_home>/{BINDINGS_DIRNAME}/<session-id>",
                reason="write_binding: records the plugin root one session started from, plus a .since stamp",
            ),
        ),
        ShapedClause(
            discovered_by="prune_dead_bindings",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path=f"<claude_home>/{BINDINGS_DIRNAME}/<session-id>",
                effect="delete",
                reason="prune_dead_bindings: removes binding files of unregistered sessions older than min_age_s",
            ),
            effect="delete",
        ),
    ),
)
_SINCE_NAME = ".since"

BASIS_BOUND = "bound"
BASIS_PREDATES = "predates-bindings"
BASIS_UNKNOWN = "liveness-unknown"


def bindings_dir(claude_home: Path) -> Path:
    """Directory holding one binding file per session under `claude_home`."""
    return claude_home / BINDINGS_DIRNAME


@dataclass(frozen=True)
class BoundSession:
    """A live session that may be running from a cache path; `basis` says why it is held."""

    session_id: str
    name: str | None
    pid: int
    start_epoch: float
    plugin_root: str | None
    basis: str


def _home(claude_home: Path | None) -> Path:
    return claude_home if claude_home is not None else claude_config_dir()


def _valid_session_id(session_id: object) -> bool:
    return (
        isinstance(session_id, str)
        and bool(session_id)
        and "/" not in session_id
        and "\\" not in session_id
        and ".." not in session_id
        and session_id != _SINCE_NAME
    )


def _atomic_write(path: Path, body: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_binding(
    session_id: str, plugin_root: str, *, claude_home: Path | None = None
) -> None:
    """Record `plugin_root` as the root `session_id` started from. Idempotent; never raises."""
    try:
        if not _valid_session_id(session_id) or not plugin_root:
            return
        directory = bindings_dir(_home(claude_home))
        directory.mkdir(parents=True, exist_ok=True)
        since = directory / _SINCE_NAME
        if not since.exists():
            try:
                with open(since, "x", encoding="utf-8", newline="\n") as handle:
                    handle.write(f"{int(time.time())}\n")
            except FileExistsError:
                pass
        target = directory / session_id
        body = Path(plugin_root).as_posix() + "\n"
        try:
            if target.read_text(encoding="utf-8") == body:
                return
        except OSError:
            pass
        _atomic_write(target, body)
    except Exception:
        return


def _read_since(directory: Path) -> float | None:
    try:
        return float((directory / _SINCE_NAME).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _norm(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _is_under(candidate: str, root: str) -> bool:
    return candidate == root or candidate.startswith(root.rstrip(os.sep) + os.sep)


def _sentinel() -> BoundSession:
    return BoundSession("", None, 0, 0.0, None, BASIS_UNKNOWN)


def sessions_bound_to(
    cache_path: Path, *, claude_home: Path | None = None
) -> list[BoundSession]:
    """Live sessions whose binding is at or under `cache_path`, or that cannot be ruled out.

    Never spawns a process; never raises (an unexpected error returns one liveness-unknown
    sentinel so a caller holds rather than deletes).
    """
    try:
        directory = bindings_dir(_home(claude_home))
        records = harness_registry.snapshot()
        if not records:
            return []
        if core._psutil() is None:
            return [
                BoundSession(sid, rec.name, rec.pid, rec.start_epoch, None, BASIS_UNKNOWN)
                for sid, rec in records.items()
            ]
        root = _norm(cache_path)
        since = _read_since(directory)
        found: list[BoundSession] = []
        for sid, rec in records.items():
            if not core.stable_pid_alive(
                rec.pid, stored_start_epoch=str(int(rec.start_epoch))
            ):
                continue
            bound_root: str | None = None
            if _valid_session_id(sid):
                try:
                    text = (directory / sid).read_text(encoding="utf-8").strip()
                except OSError:
                    text = ""
                bound_root = text or None
            if bound_root is not None:
                if _is_under(_norm(bound_root), root):
                    found.append(
                        BoundSession(sid, rec.name, rec.pid, rec.start_epoch, bound_root, BASIS_BOUND)
                    )
            elif since is None or rec.start_epoch < since:
                found.append(
                    BoundSession(sid, rec.name, rec.pid, rec.start_epoch, None, BASIS_PREDATES)
                )
        return found
    except Exception:
        return [_sentinel()]


def relaunch_lines(
    cache_path: Path, sessions: list[BoundSession], *, step: str
) -> list[str]:
    """Operator-facing lines naming each held session; empty in, empty out."""
    if not sessions:
        return []
    lines = [f"{len(sessions)} live session(s) run from {Path(cache_path).as_posix()}"]
    for s in sessions:
        lines.append(f"  {s.name or '-'} pid {s.pid} {s.session_id or '-'} ({s.basis})")
    lines.append(f"relaunch these sessions, then re-run {step}")
    return lines


def prune_dead_bindings(
    *, claude_home: Path | None = None, min_age_s: int = 86400
) -> int:
    """Remove binding files of unregistered sessions older than `min_age_s`; return the count."""
    try:
        directory = bindings_dir(_home(claude_home))
        if not directory.is_dir():
            return 0
        registered = harness_registry.snapshot()
        cutoff = time.time() - min_age_s
        removed = 0
        for entry in directory.iterdir():
            if entry.name == _SINCE_NAME or entry.name.startswith(".tmp-"):
                continue
            if entry.name in registered:
                continue
            try:
                if entry.stat().st_mtime >= cutoff:
                    continue
                entry.unlink()
                removed += 1
            except OSError:
                continue
        return removed
    except Exception:
        return 0
