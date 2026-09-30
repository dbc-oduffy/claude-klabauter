"""coordinator_core.hooks.repin_cloud_engine_root — SessionStart(*) op:
re-points `/root/engine-current` (the symlink `scripts/cloud_setup.py` pins
`COORDINATOR_ENGINE_ROOT` at, and every engine CLI shim execs through) from
the frozen `/root/klabauter` clone onto a fresher per-session checkout, when
one is mounted.

Design agreed on claude-klabauter#67 (comments 5785027514, 5785078234) between
the claude-klabauter and DoE EMs. `scripts/cloud_setup.py` runs ONCE, before
`/home/user` is mounted, and pins the symlink at the frozen clone — this hook
is the only surface that runs AFTER the platform mounts a fresh
`claude-klabauter` checkout and can safely re-point onto it.

Inert unless `CLAUDE_CODE_REMOTE=true` — a workstation session must never
touch `/root/engine-current`. Re-points only when the discovered checkout
carries a readable, non-empty `coordinator_core/_engine_stamp` AND its publish
instant (`_engine_published_at`, `skew.read_engine_published_at`) is no older
than the frozen root's. The stamp's content (`sha:<source-commit>`) names a
build but orders nothing, and ordering two shas needs git, which this budget
forbids; the stamp's mtime is checkout time in a git checkout, so it would
read an older pinned ref as fresher. The sibling publish instant is written by
the round itself and survives clone, so it orders builds. Either root lacking
a readable publish instant is UNKNOWN order: the link stays put.

No network, no git subprocess, no spawn: `resolve_checkout` scans
`/home/user`'s immediate children with `os.scandir` (case-insensitive
basename match), and every read is a single stat or small file read. Any failure
(missing checkout, unreadable stamp, symlink error) leaves the existing link
untouched and is swallowed — at most one line to stderr — never raised,
never blocking the session (module docstring's own fail-open contract,
matching every other `hooks.session_start_*` op in this package).

Negative-spec:
    Never re-points onto an unstamped checkout, one strictly older than the
    frozen root, or one whose order against the frozen root is unknown,
    however plausible its name.
    Never orders builds by file mtime.
    Never touches `/root/engine-current` when `CLAUDE_CODE_REMOTE` is unset
    or not the literal string `"true"`.
    Never shells out — `git`, `os.system`, `subprocess` are absent from this
    module on purpose; see `docs/decisions/DR-344-the-brightline-process-
    budget-for-claude-klabauter.md`.

Spec backlink: claude-klabauter#67 (comments 5785027514, 5785078234)
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.ipc import register_op
from coordinator_core.warm.skew import read_engine_published_at

#: The stable symlink `scripts/cloud_setup.py` creates and pins
#: `COORDINATOR_ENGINE_ROOT` at. Every engine CLI shim execs through this
#: path, never through the frozen clone or a per-session checkout directly.
DEFAULT_LINK_PATH = Path("/root/engine-current")

#: This script's own frozen clone (`scripts/cloud_setup.py :: CLONES["klabauter"]["dest"]`),
#: restated as a literal for the reason `cloud_setup.py`'s own
#: `FRESH_ENGINE_CHECKOUT_PATH` gives: a hook body is a standalone process,
#: it cannot import `scripts.cloud_setup`.
DEFAULT_FROZEN_ROOT = Path("/root/klabauter")

DEFAULT_SEARCH_PARENT = Path("/home/user")

FRESH_CHECKOUT_BASENAME = "claude-klabauter"

_STAMP_REL = ("coordinator_core", "_engine_stamp")

REMOTE_ENV_VAR = "CLAUDE_CODE_REMOTE"
REMOTE_ENV_TRUE = "true"


def _stamp_path(root: Path) -> Path:
    return root.joinpath(*_STAMP_REL)


def _has_readable_nonempty_stamp(root: Path) -> bool:
    try:
        return _stamp_path(root).stat().st_size > 0
    except OSError:
        return False


def resolve_checkout(search_parent: Path) -> Optional[Path]:
    try:
        with os.scandir(search_parent) as entries:
            for entry in entries:
                try:
                    if entry.is_dir() and entry.name.lower() == FRESH_CHECKOUT_BASENAME:
                        return Path(entry.path)
                except OSError:
                    continue
    except OSError:
        return None
    return None


def _is_fresher_or_equal(checkout_at: datetime, frozen_at: Optional[datetime]) -> bool:
    if frozen_at is None:
        return False
    return checkout_at >= frozen_at


def _atomic_repoint(link_path: Path, target: Path) -> None:
    link_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=link_path.name + ".", suffix=".tmp", dir=str(link_path.parent)
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    tmp_path.unlink()
    os.symlink(str(target), str(tmp_path))
    os.replace(str(tmp_path), str(link_path))


def repin_cloud_engine_root(
    *,
    link_path: Path = DEFAULT_LINK_PATH,
    frozen_root: Path = DEFAULT_FROZEN_ROOT,
    search_parent: Path = DEFAULT_SEARCH_PARENT,
    env: Optional[dict] = None,
) -> dict:
    """Re-point `link_path` onto a fresher stamped checkout under
    `search_parent`, iff `CLAUDE_CODE_REMOTE=true` in `env`. Every parameter
    is injectable so tests never touch `/root`. Never raises — every failure
    is swallowed into the returned verdict dict, with at most a one-line
    stderr note.
    """
    env = os.environ if env is None else env
    verdict: dict = {"repinned": False, "reason": None}
    if env.get(REMOTE_ENV_VAR) != REMOTE_ENV_TRUE:
        verdict["reason"] = "not a remote session"
        return verdict
    try:
        checkout = resolve_checkout(search_parent)
        if checkout is None:
            verdict["reason"] = "no fresh checkout mounted"
            return verdict
        if not _has_readable_nonempty_stamp(checkout):
            verdict["reason"] = "checkout carries no readable engine stamp"
            return verdict
        checkout_at = read_engine_published_at(checkout)
        if checkout_at is None:
            verdict["reason"] = "checkout carries no readable publish instant"
            return verdict
        if not _is_fresher_or_equal(checkout_at, read_engine_published_at(frozen_root)):
            verdict["reason"] = "checkout is not provably as fresh as the frozen root"
            return verdict
        _atomic_repoint(link_path, checkout)
        verdict["repinned"] = True
        verdict["target"] = str(checkout)
        return verdict
    except Exception as e:  # noqa: BLE001 - fail open, one-line note, never raise
        print(f"[repin_cloud_engine_root] skipped: {type(e).__name__}: {e}", file=sys.stderr)
        verdict["reason"] = f"error: {type(e).__name__}"
        return verdict


@register_op("hooks.session_start_repin_cloud_engine_root")
def _handler(params: dict, repo_root=None) -> dict:
    try:
        repin_cloud_engine_root()
    except Exception:  # noqa: BLE001 - belt-and-braces; repin_cloud_engine_root itself never raises
        pass
    return no_advisory()
