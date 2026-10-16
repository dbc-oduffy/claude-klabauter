"""Zero-spawn LFS-attribute predicates: does a clone declare `filter=lfs` in its root, info, or index-tracked nested attributes files?"""

from __future__ import annotations

from pathlib import Path
from typing import Union

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_index import IndexParseError, parse_index_stat

_LFS_FILTER_MARKER = "filter=lfs"


def attributes_text_declares_lfs(text: str) -> bool:
    """True when a non-comment portion of any line of *text* contains `filter=lfs`."""
    for line in text.splitlines():
        if _LFS_FILTER_MARKER in line.split("#", 1)[0]:
            return True
    return False


def _file_declares_lfs(path: Path) -> bool:
    try:
        return attributes_text_declares_lfs(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return False


def clone_declares_lfs(root: Union[Path, str]) -> bool:
    """Root `.gitattributes`, common-dir `info/attributes`, or any index-tracked nested
    `.gitattributes` declares `filter=lfs`. Reads files and the index in process; an
    unreadable index or non-repo yields False for that source."""
    root_path = Path(root)
    if _file_declares_lfs(root_path / ".gitattributes"):
        return True
    try:
        if _file_declares_lfs(resolve_git_common_dir(root_path) / "info" / "attributes"):
            return True
    except (OSError, RuntimeError, ValueError):
        pass
    try:
        tracked = parse_index_stat(root_path)
    except (OSError, RuntimeError, IndexParseError):
        return False
    for rel in tracked:
        if rel == ".gitattributes" or not rel.endswith("/.gitattributes"):
            continue
        if _file_declares_lfs(root_path / rel):
            return True
    return False
