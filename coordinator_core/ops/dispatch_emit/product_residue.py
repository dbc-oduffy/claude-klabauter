"""product_residue: declared product writes still dirty after a run's commits, zero git."""

from __future__ import annotations

from collections.abc import Iterable

PRODUCT_DIRTY = "product_dirty"


def _norm(raw: object) -> str:
    return str(raw).replace("\\", "/")


def declared_product_residue(
    declared: Iterable[str],
    changed: Iterable[str],
    landed: Iterable[str],
) -> dict[str, str]:
    """Declared product paths still changed and not landed, as ``path -> reason``.

    Pure over repo-relative paths (backslashes normalised to POSIX); sorted by path."""
    done = {_norm(p) for p in landed}
    dirty = {_norm(p) for p in changed}
    out: dict[str, str] = {}
    for raw in declared:
        path = _norm(raw)
        if path in dirty and path not in done:
            out[path] = PRODUCT_DIRTY
    return dict(sorted(out.items()))
