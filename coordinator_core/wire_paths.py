
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

__all__ = [
    "rel_id",
    "plans_dir",
    "archived_plan_path",
    "resolve_plan_pointer",
    "is_archived_plan_path",
]

_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2})-\d{2}-")
_PLAN_DIR_PREFIXES = ("docs/plans/", "tasks/plans/")


def rel_id(path: Path, root: Path) -> str:
    """Return the repo-relative wire ``id`` for ``path`` — ALWAYS forward-slash.

    THE single source of truth for any repo-relative path string that leaves the
    process: JSON-RPC result fields, op params handed to a sibling op, git/rg
    subprocess arguments, rendered artifact text, and any value another module keys
    a lookup map on.  ``str(path.relative_to(root))`` is WRONG for all of those: it
    renders with ``os.sep``, so the same artifact gets id ``state/handoffs/x.md`` on
    POSIX and ``state\\handoffs\\x.md`` on Windows.  A wire value whose shape depends
    on the producing host's OS is a contract defect — cockpit, rag, and DoE tooling
    all key on this string, and git and ripgrep only ever speak forward-slash paths
    (so any comparison against ``git ls-files`` / ``git status --porcelain`` output,
    or any ``rg --fixed-strings`` needle built from a path, silently mismatches on
    Windows too).

    Round-trip safety: consumers that rebuild a filesystem path do so as
    ``root / cid`` — ``pathlib`` accepts an embedded ``/`` on every platform
    including Windows, so posix ids remain round-trippable.

    Raises ValueError when ``path`` is not under ``root`` (same as relative_to);
    callers that must tolerate that classify the item themselves.
    """
    return path.relative_to(root).as_posix()


def plans_dir(root: Path) -> Path:
    return root / "docs" / "plans"


def archived_plan_path(root: Path, plan: Path | str) -> Optional[Path]:
    """Archive destination of ``plan``: ``root/archive/specs/<YYYY-MM>/<basename>``.

    The one derivation shared by the archive mover and every reader that follows
    an archived plan. None when the basename has no ``YYYY-MM-DD-`` prefix. Pure.
    """
    name = Path(plan).name
    m = _DATE_PREFIX_RE.match(name)
    if m is None:
        return None
    return root / "archive" / "specs" / m.group(1) / name


def resolve_plan_pointer(root: Path, pointer: str) -> Optional[Path]:
    """Resolve a plan pointer to an existing file, falling back to the archive.

    The archive fallback applies only to pointers under ``docs/plans/`` or
    ``tasks/plans/``. At most two stats; no spawn, no glob.
    """
    literal = root / pointer
    if literal.is_file():
        return literal
    try:
        rel = rel_id(literal, root)
    except ValueError:
        return None
    if not rel.startswith(_PLAN_DIR_PREFIXES):
        return None
    archived = archived_plan_path(root, rel)
    if archived is not None and archived.is_file():
        return archived
    return None


def is_archived_plan_path(root: Path, path: Path) -> bool:
    """True iff ``path`` lies under the repo's ``archive/`` tree."""
    try:
        return rel_id(path, root).startswith("archive/")
    except ValueError:
        return False
