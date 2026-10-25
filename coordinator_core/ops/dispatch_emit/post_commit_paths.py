"""Paths a ledger verdict's post_commit `commit` rows oblige the terminal commit to carry."""
from __future__ import annotations

from pathlib import Path
from typing import Mapping


def _texts(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, str)]
    return []


def _contained(root: Path, token: str) -> str | None:
    """Repo-relative posix path of `token` when it names an existing file or directory under
    `root` (never `root` itself), else None."""
    if not token:
        return None
    try:
        found = (root / token.replace("\\", "/")).resolve()
        rel = found.relative_to(root)
    except (OSError, ValueError):
        return None
    if not rel.parts or not found.exists():
        return None
    return rel.as_posix()


def commit_act_paths(root: Path, verdict: Mapping) -> tuple[str, ...]:
    """Sorted, deduped repo-relative posix files and directories named in the evidence or
    remaining of every item whose status is post_commit with a `commit` act. A row naming no
    existing in-repo path yields none. Pure: no git spawn."""
    from coordinator_core.ops.dispatch_emit.requirements_ledger import normalize_verdict, path_tokens

    verdict = normalize_verdict(verdict)
    items = verdict.get("items") if isinstance(verdict, Mapping) else None
    if not isinstance(items, list):
        return ()
    base = Path(root).resolve()
    found: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping) or item.get("status") != "post_commit":
            continue
        if "commit" not in _texts(item.get("post_commit_acts")):
            continue
        for text in _texts(item.get("evidence")) + _texts(item.get("remaining")):
            for token in path_tokens(text):
                rel = _contained(base, token)
                if rel is not None:
                    found.add(rel)
    return tuple(sorted(found))
