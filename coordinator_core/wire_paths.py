
from __future__ import annotations

from pathlib import Path

__all__ = ["rel_id", "plans_dir"]


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
