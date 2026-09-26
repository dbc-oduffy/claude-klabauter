
from __future__ import annotations

from pathlib import Path


class PathEscapeError(ValueError):
    pass


def path_guard(target_root: str | Path, path: str | Path) -> Path:
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
