"""Per-round publish directories: a `run_scratch` claim under machine-local ``paths.scratch_root``.

A publish round creates every directory it needs (mirror clone, materialize and commit
clones) inside one ``<scratch_root>/<repo>/publish-round/round.<stamp>.<pid>`` claim and
finishes it on every exit, SIGTERM/SIGHUP and failure included. A SIGKILLed round is removed
by the next claim's sweep or by `reap_dead_rounds`.

Invariants: the round root is never registered under ``repos.*`` (an ephemeral clone must not
resolve as an engine clone); an unset ``paths.scratch_root`` is a typed refusal, never a
drive-literal fallback; the root is read in-process, never through a machine-local spawn.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
from pathlib import Path
from collections.abc import Generator
from typing import Iterator, List, Optional

from coordinator_core import machine_resolver, run_scratch

SCRATCH_ROOT_KEY = "paths.scratch_root"
ROUND_ENV = "COORDINATOR_PUBLISH_ROUND_DIR"
ROUND_KIND = "publish-round"
ROUND_SLUG = "round"
MIRROR_PREFIX = "mirror-"
_ROUND_RE = re.compile(r"^round\.\d{8}T\d{6}Z\.\d+$")
_REPO_DIR_NAME = Path(__file__).resolve().parent.parent.name


class RoundNotOpenError(RuntimeError):
    """`mirror_dest` was called outside an open publish round."""


def round_root() -> Path:
    """``<paths.scratch_root>/<repo dir name>/publish-round``; raises `SettingsKeyMissing` naming the key when unset."""
    return Path(machine_resolver.require_key(SCRATCH_ROOT_KEY)) / _REPO_DIR_NAME / ROUND_KIND


@contextlib.contextmanager
def round_dir() -> Generator[Path]:
    """Yield a fresh `run_scratch` claim as `ROUND_ENV`, finished on every exit including SIGTERM/SIGHUP."""
    machine_resolver.require_key(SCRATCH_ROOT_KEY)
    path = run_scratch.claim(_REPO_DIR_NAME, ROUND_KIND, ROUND_SLUG)

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
        run_scratch.finish(path)


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


def _scan_rounds(root: Path) -> Iterator[tuple[Path, bool]]:
    """Yield ``(round dir, owner alive)``; a round without a readable claim mark reads as alive."""
    try:
        with os.scandir(root) as it:
            entries = list(it)
    except OSError:
        return
    for entry in entries:
        if _ROUND_RE.match(entry.name) and entry.is_dir(follow_symlinks=False):
            mark = run_scratch.read_mark(entry.path)
            yield Path(entry.path), mark is None or run_scratch.owner_alive(mark)


def live_round_mirrors() -> List[Path]:
    """Mirror clones inside rounds whose owner pid is alive; empty when the key is unset."""
    try:
        root = round_root()
    except machine_resolver.SettingsKeyMissing:
        return []
    out: List[Path] = []
    for rnd, alive in _scan_rounds(root):
        if not alive:
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
    """Sweep the publish-round kind dir (dead owners, stale markless claims); returns removed paths, never raises."""
    try:
        root = round_root()
    except machine_resolver.SettingsKeyMissing:
        return []
    try:
        return [Path(p) for p in run_scratch.sweep_kind(root, run_scratch.KEEP_DEFAULT, None)]
    except OSError:
        return []
