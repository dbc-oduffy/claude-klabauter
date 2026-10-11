"""Canonical repo-relative tree-entry paths: the refusal every commit route applies before a
tree write. Pure, spawn-free.

TRAP: git accepts a non-canonical entry (a `.git` component included) at `update-index` and
fails only later; a remote's fsck then rejects every push carrying that tree.
"""

from __future__ import annotations

import ntpath
import posixpath
from typing import Iterable, Optional


def _has_windows_drive(normalized: str) -> bool:
    """True iff `normalized` (already `/`-normalized) carries a Windows
    drive letter (`"C:/foo"`, `"C:foo"`) -- platform-independent by
    construction. `ntpath.splitdrive` is used explicitly rather than
    `os.path.splitdrive`/`Path(...).is_absolute()`: `os.path` resolves to
    `posixpath` on a non-Windows host, whose `splitdrive` is a no-op, and a
    `PosixPath("C:/foo").is_absolute()` is False -- either would let a
    drive-letter path slip the parse-time containment guard on Linux while
    catching it on Windows, exactly the platform-dependent gap this check
    exists to close. `ntpath` itself is a pure-Python module present on
    every platform, so this reads the same drive-letter shape everywhere."""
    return ntpath.splitdrive(normalized)[0] != ""


def canonical_repo_relative_path_refusal(path: str) -> Optional[str]:
    """`None` iff `path` is a canonical repo-relative tree-entry path; else a refusal naming the path
    and, when safe, its canonical form. REFUSAL, never silent normalisation: git rejects a
    non-canonical entry only at `update-index`/`read-tree`, after the caller saw rc=0.

    Refuses: absolute, Windows drive, and (per `/` component) empty, `.`, `..`, or `.git` (casefolded).
    """
    normalized = path.replace("\\", "/")
    reasons = []
    if not normalized:
        reasons.append("empty path")
    if normalized.startswith("/"):
        reasons.append("absolute path")
    if _has_windows_drive(normalized):
        reasons.append("Windows drive letter")
    if normalized:
        parts = normalized.split("/")
        if any(p == "" for p in parts):
            reasons.append("empty path component (a doubled or trailing '/')")
        if any(p == "." for p in parts):
            reasons.append("'.' path component")
        if any(p == ".." for p in parts):
            reasons.append("'..' path component")
        if any(p.casefold() == ".git" for p in parts):
            reasons.append("'.git' path component")
    if not reasons:
        return None

    candidate = posixpath.normpath(normalized) if normalized else normalized
    canonical_ok = (
        normalized
        and candidate not in ("", ".", "..")
        and not candidate.startswith("../")
        and not candidate.startswith("/")
        and not _has_windows_drive(candidate)
        and not any(p.casefold() == ".git" for p in candidate.split("/"))
    )
    hint = f"; canonical form: {candidate!r}" if canonical_ok else ""
    return (
        f"refusing non-canonical path {path!r} ({', '.join(reasons)}){hint} -- "
        "pass a canonical repo-relative path"
    )


def first_non_canonical_path_refusal(paths) -> Optional[str]:
    """First `canonical_repo_relative_path_refusal` hit across `paths`, else `None`."""
    for candidate_path in paths:
        refusal = canonical_repo_relative_path_refusal(candidate_path)
        if refusal is not None:
            return refusal
    return None
