"""Per-round publish directories under machine-local ``paths.scratch_root``.

A publish round creates every directory it needs (mirror clone, materialize and commit
clones) inside one ``publish-round-<pid>-<hex>`` dir and removes it on every exit,
SIGTERM/SIGHUP and failure included. A SIGKILLed round is reaped by `reap_dead_rounds`.

Invariants: the round root is never registered under ``repos.*`` (an ephemeral clone must not
resolve as an engine clone); an unset ``paths.scratch_root`` is a typed refusal, never a
drive-literal fallback; the root is read in-process, never through a machine-local spawn.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import stat
import sys
from pathlib import Path
from collections.abc import Generator
from typing import Iterator, List, Optional

from coordinator_core import machine_resolver

SCRATCH_ROOT_KEY = "paths.scratch_root"
ROUND_ENV = "COORDINATOR_PUBLISH_ROUND_DIR"
ROUND_PREFIX = "publish-round-"
MIRROR_PREFIX = "mirror-"
_ROUND_RE = re.compile(r"^publish-round-(\d+)-[0-9a-f]+$")
_REPO_DIR_NAME = Path(__file__).resolve().parent.parent.name


class RoundNotOpenError(RuntimeError):
    """`mirror_dest` was called outside an open publish round."""


def _clear_readonly_and_retry(func, path, _exc) -> None:
    """rmtree error hook: a Windows read-only git object is made writable and the op retried once."""
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    func(path)


def _rmtree(path: Path) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, ignore_errors=False, onexc=_clear_readonly_and_retry)
    else:
        shutil.rmtree(path, ignore_errors=False, onerror=_clear_readonly_and_retry)


def _remove_round(path: Path) -> None:
    """Remove a round dir; a held handle leaves a partial dir for the next reap, never raises."""
    try:
        _rmtree(path)
    except OSError:
        shutil.rmtree(path, ignore_errors=True)


def round_root() -> Path:
    """``<paths.scratch_root>/<repo dir name>``; raises `SettingsKeyMissing` naming the key when unset."""
    return Path(machine_resolver.require_key(SCRATCH_ROOT_KEY)) / _REPO_DIR_NAME


def _pid_alive(pid: int) -> bool:
    from coordinator_core.group_em import session_registry

    return bool(session_registry.pid_alive(pid))


@contextlib.contextmanager
def round_dir() -> Generator[Path]:
    """Yield a fresh pid-stamped round dir, removed on every exit; SIGTERM/SIGHUP unwind via SystemExit.

    Sets `ROUND_ENV` for child processes for the duration. A SIGKILL leaves the dir for `reap_dead_rounds`.
    """
    import secrets

    root = round_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{ROUND_PREFIX}{os.getpid()}-{secrets.token_hex(4)}"
    path.mkdir()

    def _unwind(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    saved = {}
    for name in ("SIGTERM", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            saved[signum] = signal.signal(signum, _unwind)
        except (ValueError, OSError):
            continue
    prior_env = os.environ.get(ROUND_ENV)
    os.environ[ROUND_ENV] = str(path)
    try:
        yield path
    finally:
        if prior_env is None:
            os.environ.pop(ROUND_ENV, None)
        else:
            os.environ[ROUND_ENV] = prior_env
        for signum, prior in saved.items():
            try:
                signal.signal(signum, prior)
            except (ValueError, OSError):
                pass
        _remove_round(path)


def current_round() -> Optional[Path]:
    """The open round's dir from `ROUND_ENV`, or None when unset or the dir is gone."""
    raw = os.environ.get(ROUND_ENV)
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_dir() else None


def mirror_dest(key: str) -> Path:
    """``<round>/mirror-<key>``; raises `RoundNotOpenError` outside an open round."""
    rnd = current_round()
    if rnd is None:
        raise RoundNotOpenError(
            f"no open publish round ({ROUND_ENV} unset); run through coordinator-publish"
        )
    return rnd / f"{MIRROR_PREFIX}{key}"


def is_round_path(path: os.PathLike | str) -> bool:
    """True when ``path`` is a round dir or inside one; a launcher or resolver refuses such a runtime-script candidate."""
    try:
        root = round_root().resolve()
        resolved = Path(path).resolve()
    except (machine_resolver.SettingsKeyMissing, OSError):
        return False
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        return False
    return bool(rel.parts) and _ROUND_RE.match(rel.parts[0]) is not None


def _scan_rounds(root: Path) -> Iterator[tuple[Path, int]]:
    try:
        with os.scandir(root) as it:
            entries = list(it)
    except OSError:
        return
    for entry in entries:
        m = _ROUND_RE.match(entry.name)
        if m and entry.is_dir(follow_symlinks=False):
            yield Path(entry.path), int(m.group(1))


def live_round_mirrors() -> List[Path]:
    """Mirror clones inside rounds whose owner pid is alive; empty when the key is unset."""
    try:
        root = round_root()
    except machine_resolver.SettingsKeyMissing:
        return []
    out: List[Path] = []
    for rnd, pid in _scan_rounds(root):
        if not _pid_alive(pid):
            continue
        try:
            with os.scandir(rnd) as it:
                out.extend(
                    Path(e.path)
                    for e in it
                    if e.name.startswith(MIRROR_PREFIX) and e.is_dir(follow_symlinks=False)
                )
        except OSError:
            continue
    return out


def reap_dead_rounds() -> List[Path]:
    """Remove round dirs whose owner pid is dead; best effort, returns the removed paths, never raises."""
    try:
        root = round_root()
    except machine_resolver.SettingsKeyMissing:
        return []
    removed: List[Path] = []
    for rnd, pid in _scan_rounds(root):
        try:
            if _pid_alive(pid):
                continue
            _remove_round(rnd)
            if not rnd.exists():
                removed.append(rnd)
        except OSError:
            continue
    return removed
