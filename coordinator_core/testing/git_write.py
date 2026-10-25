"""Test-fixture writes that `git status` always sees, even under core.checkStat=minimal."""

from __future__ import annotations

import os
from pathlib import Path

_BUMP_NS = 2_000_000_000


def _stat_size_mtime(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return st.st_size, st.st_mtime_ns


def _bump_if_same_size(path: Path, old: tuple[int, int] | None) -> None:
    # A same-size rewrite inside the index's mtime tick reads clean under
    # core.checkStat=minimal; a changed size already defeats the stat cache.
    if old is None:
        return
    st = path.stat()
    if st.st_size != old[0]:
        return
    new_mtime = max(old[1], st.st_mtime_ns) + _BUMP_NS
    os.utime(path, ns=(st.st_atime_ns, new_mtime))


def write_seen_by_git(
    path: Path | str, text: str, *, encoding: str = "utf-8", newline: str | None = None
) -> None:
    """Write `text` to `path` so a following `git status` reports the change."""
    p = Path(path)
    old = _stat_size_mtime(p)
    p.write_text(text, encoding=encoding, newline=newline)
    _bump_if_same_size(p, old)


def write_bytes_seen_by_git(path: Path | str, data: bytes) -> None:
    """Bytes twin of `write_seen_by_git`."""
    p = Path(path)
    old = _stat_size_mtime(p)
    p.write_bytes(data)
    _bump_if_same_size(p, old)
