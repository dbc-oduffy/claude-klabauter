"""Pure reconcile of an asks-entry's declared writes against the delivered paths."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from posixpath import dirname


def _norm(path: str) -> str:
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def reconcile(declared: Sequence[str], delivered: Iterable[str]) -> dict:
    """Return undelivered, undeclared and renamed paths (POSIX, no leading "./").

    A declared/delivered pair is a rename only when, within one parent dir,
    exactly one declared path went undelivered and exactly one delivered path
    was undeclared; a paired path leaves undelivered/undeclared. Otherwise both
    stay unpaired.
    """
    decl = list(dict.fromkeys(_norm(p) for p in declared))
    deliv = list(dict.fromkeys(_norm(p) for p in delivered))
    deliv_set, decl_set = set(deliv), set(decl)
    undelivered = [p for p in decl if p not in deliv_set]
    undeclared = [p for p in deliv if p not in decl_set]

    by_dir_u: dict[str, list[str]] = {}
    by_dir_d: dict[str, list[str]] = {}
    for p in undelivered:
        by_dir_u.setdefault(dirname(p), []).append(p)
    for p in undeclared:
        by_dir_d.setdefault(dirname(p), []).append(p)

    renamed: list[dict[str, str]] = []
    paired_u: set[str] = set()
    paired_d: set[str] = set()
    for d, us in by_dir_u.items():
        ds = by_dir_d.get(d, [])
        if len(us) == 1 and len(ds) == 1:
            renamed.append({"declared": us[0], "delivered": ds[0]})
            paired_u.add(us[0])
            paired_d.add(ds[0])

    return {
        "undelivered": [p for p in undelivered if p not in paired_u],
        "undeclared": [p for p in undeclared if p not in paired_d],
        "renamed": renamed,
    }
