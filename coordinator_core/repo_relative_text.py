"""Shared repo-relativizer for one path and for free text.

`to_repo_relative` answers one path; `relativize_text` rewrites every absolute
root prefix inside an emitted text. Both emit posix and never take the root
from anywhere but the caller.

Negative-spec: a path outside every root is returned untouched (None from
`to_repo_relative`, byte-identical from `relativize_text`), never relativized
against a root it is not under.
"""

from __future__ import annotations

import re
from typing import Optional, Sequence

from coordinator_core.write_guards._case_fold_path import repo_relative

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
# A root is a prefix only at a path-token start: `/x/repo` must not match inside `/y/x/repo`.
_NOT_AFTER_PATH_CHAR = r"(?<![\w.\\/-])"


def to_repo_relative(path: str, root: str) -> Optional[str]:
    """Posix repo-relative form of `path`, or None when it is outside `root`.

    Delegates the symlink/realpath and case-fold traps to
    `write_guards._case_fold_path.repo_relative`."""
    return repo_relative(path, root)


def _root_pattern(root: str) -> str:
    parts = root.replace("\\", "/").rstrip("/").split("/")
    return r"[\\/]".join(re.escape(p) for p in parts)


def relativize_text(text: str, roots: Sequence[tuple[str, str]]) -> str:
    """Rewrite each `<root><separator>` in `text` to its replacement prefix.

    `roots` is `(absolute_root, replacement)`; `""` replaces with nothing, any
    other replacement gains one trailing `/`. Both separators match; a
    drive-lettered root also matches case-insensitively. Longest root wins so a
    nested root beats its parent. Pure: no I/O, no spawn."""
    live = [(r, p) for r, p in roots if r.replace("\\", "/").rstrip("/")]
    if not text or not live:
        return text
    live.sort(key=lambda rp: len(rp[0].replace("\\", "/").rstrip("/")), reverse=True)
    alternatives = []
    replacements: dict[str, str] = {}
    for i, (root, prefix) in enumerate(live):
        flag = "(?i:" if _WINDOWS_DRIVE.match(root) else "(?:"
        name = f"r{i}"
        alternatives.append(f"(?P<{name}>{flag}{_root_pattern(root)}))")
        replacements[name] = prefix.replace("\\", "/").rstrip("/")
    pattern = re.compile(_NOT_AFTER_PATH_CHAR + "(?:" + "|".join(alternatives) + r")[\\/]")

    def _sub(match: re.Match) -> str:
        prefix = replacements[match.lastgroup]
        return prefix + "/" if prefix else ""

    return pattern.sub(_sub, text)
