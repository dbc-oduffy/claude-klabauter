"""Importability judge for the tree a halt leaves behind.

In process, zero spawns. Judges the run's own changed `.py` files and their importers inside the
run's write set; the caller evicts what this reports (`halt_tree_evict`).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Optional, Sequence


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _importers(root: Path, own: Sequence[str], write_set: Sequence[str]) -> list[str]:
    """Write-set `.py` files outside `own` whose text names an own module's stem."""
    stems = {Path(p).stem for p in own if Path(p).stem != "__init__"}
    stems |= {Path(p).parent.name for p in own if Path(p).stem == "__init__" and Path(p).parent.name}
    out = []
    for rel in sorted(set(write_set) - set(own)):
        if not rel.endswith(".py"):
            continue
        text = _read(root / rel)
        if text is not None and any(s in text for s in stems):
            out.append(rel)
    return out


def broken_paths(
    root: Path, own_changed: Sequence[str], write_set: Sequence[str]
) -> dict[str, str]:
    """Return `{repo-relative posix path: rendered finding}` for every path left unimportable.

    `own_changed` are the paths the run owns (never a peer's); `write_set` bounds the importers
    examined. The result may name a write-set path the run does not own: the caller reports it and
    never evicts it. An empty dict means the tree is importable.

    Fixed point: an own path found broken reads as its HEAD bytes in the next pass, so an importer
    that only breaks once that path is evicted is reported too.
    """
    from coordinator_core.authoring_leaks import HeadView, import_closure

    root = Path(root)
    own = sorted({p for p in own_changed if p.endswith(".py")})
    if not own:
        return {}
    gate = own + _importers(root, own, write_set)
    view = HeadView(root, import_closure.candidates(gate))
    broken: dict[str, str] = {}
    evicted: set[str] = set()

    def landed(rel: str) -> Optional[str]:
        return view.head_text(rel) if rel in evicted else _read(root / rel)

    for _ in range(len(own) + 1):
        for rel in own:
            if rel in evicted:
                continue
            text = landed(rel)
            if text is None:
                continue
            try:
                ast.parse(text)
            except (SyntaxError, ValueError) as exc:
                broken[rel] = f"{getattr(exc, 'lineno', 0) or 0}: syntax error: {exc}"
                evicted.add(rel)
        live = [p for p in gate if p not in evicted]
        try:
            found = import_closure.detect(root, live, view, landed_text=landed)
        except Exception as exc:
            return {**broken, "": f"import-closure detector failed: {type(exc).__name__}: {exc}"}
        grew = False
        for f in found:
            broken.setdefault(f.path, f"{f.line}: {f.detail}")
            if f.path in own and f.path not in evicted:
                evicted.add(f.path)
                grew = True
        if not grew:
            break
    return broken
