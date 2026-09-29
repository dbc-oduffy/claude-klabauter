"""Compat aliases for :mod:`coordinator_core.content_root`.

Existing callers keep the old function names; the implementation is
`content_root`. New code imports `content_root` directly.
"""

from __future__ import annotations

from coordinator_core.content_root import read_content_root, read_pointer_files


def read_content_root_pointer_file(home: str | None = None) -> str:
    """Pointer-file rungs only (no registry): new name first, then the legacy name."""
    return read_pointer_files(home)


def read_content_root_pointer() -> str:
    """Alias of `read_content_root`."""
    return read_content_root()
