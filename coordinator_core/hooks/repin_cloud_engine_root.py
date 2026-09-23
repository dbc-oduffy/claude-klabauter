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
carries a readable, non-empty `coordinator_core/_engine_stamp` AND that
stamp's mtime is no older than the frozen root's. The stamp's content
(`sha:<source-commit>`, `coordinator_core/warm/skew.py :: write_engine_stamp`)
names a build but orders nothing, and ordering two shas needs git, which this
budget forbids. In a git checkout the mtime is checkout time, not build time,
so the comparison guarantees only "a stamped checkout mounted after the image
was baked" -- it cannot catch an environment that pins an older ref.

No network, no git subprocess, no spawn: `resolve_checkout` scans
`/home/user`'s immediate children with `os.scandir` (case-insensitive
basename match), and every stamp read is a single `Path.stat`. Any failure
(missing checkout, unreadable stamp, symlink error) leaves the existing link
untouched and is swallowed — at most one line to stderr — never raised,
never blocking the session (module docstring's own fail-open contract,
matching every other `hooks.session_start_*` op in this package).

Negative-spec:
    Never re-points onto an unstamped checkout, or one strictly older than
    the frozen root, however plausible its name.
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
from pathlib import Path
from typing import Optional

from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.ipc import register_op

#: The stable symlink `scripts/cloud_setup.py` creates and pins
#: `COORDINATOR_ENGINE_ROOT` at. Every engine CLI shim execs through this
#: path, never through the frozen clone or a per-session checkout directly.
DEFAULT_LINK_PATH = Path("/root/engine-current")  # abs-path-ok: single-host cloud VM (cloud_setup.py's own convention)

#: This script's own frozen clone (`scripts/cloud_setup.py :: CLONES["klabauter"]["dest"]`),
#: restated as a literal for the reason `cloud_setup.py`'s own
#: `FRESH_ENGINE_CHECKOUT_PATH` gives: a hook body is a standalone process,
#: it cannot import `scripts.cloud_setup`.
DEFAULT_FROZEN_ROOT = Path("/root/klabauter")  # abs-path-ok: single-host cloud VM (cloud_setup.py's own convention)

#: Where the platform mounts a fresh per-session engine checkout.
DEFAULT_SEARCH_PARENT = Path("/home/user")  # abs-path-ok: single-host cloud VM (cloud_setup.py's own convention)

#: Basename the fresh checkout is matched against, case-insensitively — the
#: platform's own capitalization of a mounted repo is not guaranteed.
FRESH_CHECKOUT_BASENAME = "claude-klabauter"

_STAMP_REL = ("coordinator_core", "_engine_stamp")

#: The env var that gates this hook to a cloud session. Absent or any value
#: other than the literal string below leaves the hook fully inert.
REMOTE_ENV_VAR = "CLAUDE_CODE_REMOTE"
REMOTE_ENV_TRUE = "true"


def _stamp_path(root: Path) -> Path:
    return root.joinpath(*_STAMP_REL)


def _readable_nonempty_stamp_mtime(root: Path) -> Optional[float]:
    """The `_engine_stamp` mtime under `root`, or `None` if it is missing,
    unreadable, or empty. Never raises."""
    stamp = _stamp_path(root)
    try:
        st = stamp.stat()
        if st.st_size <= 0:
            return None
        return st.st_mtime
    except OSError:
        return None


def resolve_checkout(search_parent: Path) -> Optional[Path]:
    """A case-insensitive `claude-klabauter` directory directly under
    `search_parent`, or `None`. Never raises."""
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


def _is_fresher_or_equal(checkout_mtime: float, frozen_mtime: Optional[float]) -> bool:
    """True iff the checkout's stamp is not older than the frozen root's own
    stamp. A frozen root with no readable stamp of its own can't be compared
    against, so this is False (fail-closed: leave the link as-is)."""
    if frozen_mtime is None:
        return False
    return checkout_mtime >= frozen_mtime


def _atomic_repoint(link_path: Path, target: Path) -> None:
    """Create a temp symlink beside `link_path` and `os.replace` it onto
    `link_path` — atomic on POSIX, never leaves `link_path` half-written."""
    link_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=link_path.name + ".", suffix=".tmp", dir=str(link_path.parent)
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    tmp_path.unlink()  # mkstemp creates a real file; symlink needs the name free
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
        checkout_mtime = _readable_nonempty_stamp_mtime(checkout)
        if checkout_mtime is None:
            verdict["reason"] = "checkout carries no readable engine stamp"
            return verdict
        frozen_mtime = _readable_nonempty_stamp_mtime(frozen_root)
        if not _is_fresher_or_equal(checkout_mtime, frozen_mtime):
            verdict["reason"] = "checkout stamp is not fresher than the frozen root's"
            return verdict
        _atomic_repoint(link_path, checkout)
        verdict["repinned"] = True
        verdict["target"] = str(checkout)
        return verdict
    except Exception as e:  # noqa: BLE001 - fail open, one-line note, never raise
        print(f"[repin_cloud_engine_root] skipped: {type(e).__name__}: {e}", file=sys.stderr)
        verdict["reason"] = f"error: {type(e).__name__}"
        return verdict


@register_op("hooks.repin_cloud_engine_root")
def _handler(params: dict, repo_root=None) -> dict:
    try:
        repin_cloud_engine_root()
    except Exception:  # noqa: BLE001 - belt-and-braces; repin_cloud_engine_root itself never raises
        pass
    return no_advisory()
