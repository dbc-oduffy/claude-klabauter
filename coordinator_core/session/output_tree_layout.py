"""Shard layout for dated record trees: where a new entry belongs and where an existing one lives.

A sharded tree is `<tree>/<YYYY-MM>/<file>`; the month comes from a per-tree date extractor over the
filename. Vendored from DoE `coordinator/lib/output_tree_layout.py` (shard_path, resolve rule, LAYOUT);
`tests/test_output_tree_layout.py` compares shard_path against it when `$CONTENT_ROOT_ROOT` is set.
Unlike DoE's `resolve`, every function here takes `repo_root` explicitly (no cwd fallback).

Negative-spec: stdlib only, no import-time work (on the Stop-hook path, like `record_homes`); no
subprocess; never creates a directory; at most one stat plus one REDIRECTS read per `locate`.
Paths in and out are repo-relative POSIX; `\\` input is accepted.
"""

from __future__ import annotations

import os
import re
from typing import Callable, Iterator

REDIRECTS_NAME = "REDIRECTS.md"
ROOT_EXEMPT_NAMES = (REDIRECTS_NAME, "INDEX.md", ".gitkeep")
ROOT_EXEMPT_PREFIXES = ("README",)

_LEADING_DATE = re.compile(r"^(\d{4})-(\d{2})-\d{2}(?!\d)")
_ASK_STAMP = re.compile(r"^ask-(\d{4})(\d{2})\d{2}T\d{6}")
_WARP_DATE = re.compile(r"^warp-(\d{4})-(\d{2})-\d{2}-")
_MONTH_DIR = re.compile(r"^\d{4}-\d{2}$")


def is_month_dir(name: str) -> bool:
    """True when `name` is a `YYYY-MM` shard directory name."""
    return _MONTH_DIR.match(name) is not None


def _month(rx: re.Pattern[str], name: str) -> str | None:
    m = rx.match(name)
    return f"{m.group(1)}-{m.group(2)}" if m else None


def _leading_date_month(name: str) -> str | None:
    return _month(_LEADING_DATE, name)


def _delivery_month(name: str) -> str | None:
    return _month(_ASK_STAMP, name) or _month(_WARP_DATE, name)


LAYOUT: tuple[tuple[str, str, str], ...] = (
    ("docs/plans/**", "month", "claude-klabauter"),
    ("state/sizings/**", "month", "claude-klabauter"),
    ("state/handoffs/**", "month", "claude-klabauter"),
    ("docs/delivery/**", "month", "claude-klabauter"),
)

_EXTRACTORS: dict[str, Callable[[str], str | None]] = {
    "docs/plans": _leading_date_month,
    "state/sizings": _leading_date_month,
    "state/handoffs": _leading_date_month,
    "docs/delivery": _delivery_month,
}


def _norm(path: str) -> str:
    p = str(path).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.strip("/")


def tree_of(glob: str) -> str:
    """`docs/plans/**` -> `docs/plans`."""
    return glob[:-3] if glob.endswith("/**") else glob


def trees() -> tuple[str, ...]:
    return tuple(tree_of(g) for g, _, _ in LAYOUT)


def is_exempt(name: str) -> bool:
    """True for a name that legitimately sits at a tree root beside the shards."""
    return name in ROOT_EXEMPT_NAMES or name.startswith(ROOT_EXEMPT_PREFIXES)


def month_for(tree: str, filename: str) -> str | None:
    """`YYYY-MM` for a filename under `tree`, or None when no extractor accepts it.

    Raises KeyError for a tree with no LAYOUT row.
    """
    extractor = _EXTRACTORS.get(tree_of(_norm(tree)))
    if extractor is None:
        raise KeyError(f"no LAYOUT row for tree {tree!r}")
    return extractor(filename)


def shard_path(tree: str, filename: str) -> str:
    """Repo-relative POSIX `<tree>/<YYYY-MM>/<filename>` a new entry belongs at.

    Raises ValueError when no extractor of the tree accepts the name.
    """
    t = tree_of(_norm(tree))
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    month = month_for(t, name)
    if month is None:
        raise ValueError(f"{name!r} carries no date this tree's extractor accepts")
    return f"{t}/{month}/{name}"


def write_rel(tree: str, name: str) -> str:
    """Where a writer puts `name`: its shard, or the flat path when no extractor accepts it.

    Never raises for an undated name; raises KeyError only for a tree with no LAYOUT row.
    """
    t = tree_of(_norm(tree))
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    month = month_for(t, base)
    return f"{t}/{month}/{base}" if month else f"{t}/{base}"


def _redirect_target(repo_root: str, tree: str, old: str) -> str | None:
    try:
        with open(os.path.join(repo_root, tree, REDIRECTS_NAME), encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip().strip("`").strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and _norm(cells[0]) == old:
            return _norm(cells[1])
    return None


def locate(repo_root: str, rel: str) -> str:
    """Where the cited tree path lives now, repo-relative POSIX.

    The flat path if it is a file; else the tree's REDIRECTS.md old -> new row; else the shard rule.
    A path under no LAYOUT tree, already nested, or whose name no extractor accepts returns normalized
    and unchanged. A sharded cite that does not exist also returns unchanged.
    """
    p = _norm(rel)
    for tree in trees():
        if not p.startswith(tree + "/"):
            continue
        if os.path.isfile(os.path.join(repo_root, p)):
            return p
        hit = _redirect_target(repo_root, tree, p)
        if hit:
            return hit
        rest = p[len(tree) + 1:]
        if "/" in rest:
            return p
        try:
            return shard_path(tree, rest)
        except ValueError:
            return p
    return p


def iter_entries(repo_root: str, tree: str, suffix: str) -> Iterator[str]:
    """Repo-relative POSIX paths of `suffix` files at the tree root, then in each `YYYY-MM` child dir.

    Root-exempt names (README*, INDEX.md, REDIRECTS.md, .gitkeep) are skipped. Order is unspecified.
    A missing tree yields nothing.
    """
    t = tree_of(_norm(tree))
    base = os.path.join(repo_root, t)
    shards: list[str] = []
    try:
        with os.scandir(base) as it:
            for e in it:
                if e.is_file():
                    if e.name.endswith(suffix) and not is_exempt(e.name):
                        yield f"{t}/{e.name}"
                elif _MONTH_DIR.match(e.name) and e.is_dir():
                    shards.append(e.name)
    except OSError:
        return
    for month in shards:
        try:
            with os.scandir(os.path.join(base, month)) as it:
                for e in it:
                    if e.is_file() and e.name.endswith(suffix) and not is_exempt(e.name):
                        yield f"{t}/{month}/{e.name}"
        except OSError:
            continue
