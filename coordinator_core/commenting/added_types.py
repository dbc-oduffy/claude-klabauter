"""Added-comment record and the detector entry signature."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CommentKind = Literal["comment", "docstring"]

SUFFIXES: frozenset[str] = frozenset(
    {
        ".py",
        ".ts", ".tsx", ".js", ".mjs", ".cpp", ".h", ".hpp", ".cs", ".jsonc",
        ".sh", ".bash", ".yaml", ".yml", ".toml",
    }
)


@dataclass(frozen=True)
class AddedComment:
    """A comment or docstring present in `after` and absent from `before`; `line` is 1-based in `after`."""

    path: str
    line: int
    text: str
    kind: CommentKind


def added_comments(rel_path: str, before: str, after: str) -> list[AddedComment]:
    """Comments added between `before` and `after`; suffixes outside SUFFIXES return []."""
    raise NotImplementedError("added_comments is implemented by coordinator_core.commenting.added")
