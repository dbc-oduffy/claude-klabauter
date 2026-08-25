"""
coordinator_core.cartography._guard — shared target_root containment helper.

Purpose: single implementation of the path-containment pattern every
cartography primitive (``tree``, ``file_index``, ``churn``, ``symbols``,
``edges``) needs before it reads a caller-supplied path relative to a
``target_root`` — reject traversal outside the root rather than resolving
and silently walking/reading outside the intended tree.

Mirrors ``coordinator_core.ops._path_guard.contained_path`` (same
post-``.resolve()``, symlink-safe, fails-closed shape) collapsed into a
single ``target_root``-relative call so the five cartography primitives
share one call site rather than each reimplementing containment.

Threat model: bounded defense-in-depth (blast-radius containment against a
malformed/fat-fingered path silently walking outside the intended tree),
not a privilege boundary — same same-user, in-process CLI model as
``coordinator_core.ops._path_guard`` (see that module's docstring).

Negative-spec:
  - Does NOT decide which roots are valid for a given op — callers supply
    their own ``target_root``; this module has no notion of a default root.
  - Does NOT normalize/sanitize a path for reuse — returns the resolved
    ``Path`` or raises; it never mutates or repairs the input.
"""

from __future__ import annotations

from pathlib import Path


class PathEscapeError(ValueError):
    """Raised when a candidate path resolves outside its ``target_root``."""


def path_guard(target_root: str | Path, path: str | Path) -> Path:
    """Return the resolved ``path`` iff it is contained under ``target_root``.

    ``path`` may be absolute or relative; relative paths are joined onto
    ``target_root`` before resolution. Post-``.resolve()`` containment check
    (symlink-safe — ``Path.resolve()`` follows symlinks, so a symlink that
    resolves outside ``target_root`` is caught). Fails closed: any
    ``OSError`` while resolving either side, or a resolved candidate that
    isn't under the resolved root, raises ``PathEscapeError`` rather than
    returning a permissive fallback.

    Args:
        target_root: The containing root directory.
        path: The caller-supplied candidate, absolute or relative to
            ``target_root``.

    Returns:
        The resolved ``Path``, guaranteed to be under ``target_root``.

    Raises:
        PathEscapeError: if resolution fails or the candidate escapes
            ``target_root`` (e.g. via ``..`` traversal or a symlink).
    """
    try:
        root = Path(target_root).resolve()
    except OSError as exc:
        raise PathEscapeError(f"could not resolve target_root: {target_root!r}") from exc

    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate

    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise PathEscapeError(f"could not resolve path: {path!r}") from exc

    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PathEscapeError(f"{path!r} escapes target_root {target_root!r}") from exc

    return resolved
