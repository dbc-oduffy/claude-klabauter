"""handoff_id resolver for `frontmatter.remint`: the wire-derived id, never a fabricated one.

The value is `_derive_handoff_id(repo, path)[0]` with `repo` the emitting repo's own
slug, because the hash input is `f"{repo}:{basename}"` and any other label mints an id
the cockpit wire never publishes. The mint site is guarded here, not left to the schema.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from coordinator_core.dag import (
    build_handoff_id_index,
    read_handoff_meta,
    scan_repo_handoff_corpus,
)
from coordinator_core.ops.emit.context import resolve_repo_name
from coordinator_core.ops.emit.sections.handoffs import _derive_handoff_id
from coordinator_core.ops.frontmatter_remint._contract import Refused, Resolved

_MINTED_ID_RE = re.compile(r"^hnd-(?!placeholder-replace-with)[a-z0-9-]+-[0-9a-f]{6}$")

#: Frontmatter fields whose values may name another handoff by id.
_REFERENCE_FIELDS = (
    "predecessor",
    "predecessor_id",
    "additional_predecessors",
    "forked_from",
    "origin_handoff",
    "origin_handoff_id",
)


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value.strip()
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _references_to(old_id: str, metas: Mapping[str, dict], self_abs: str) -> list[str]:
    hits: list[str] = []
    for p, meta in metas.items():
        if p == self_abs or not meta:
            continue
        for key in _REFERENCE_FIELDS:
            if old_id in _strings(meta.get(key)):
                hits.append(f"{os.path.basename(p)}:{key}")
                break
    return hits


def resolve(path: Path, fm: Mapping[str, Any], repo_root: Path) -> Resolved | Refused:
    """Derive the wire handoff_id for `path`; refuse if underivable, referenced or colliding."""
    repo = resolve_repo_name(repo_root)
    new_id = _derive_handoff_id(repo, str(path))[0]
    if not _MINTED_ID_RE.match(new_id):
        return Refused("handoff-id-underivable", f"derived {new_id!r} fails the minted-id shape")

    self_abs = os.path.abspath(path)
    paths = scan_repo_handoff_corpus(str(repo_root))
    metas = {os.path.abspath(p): read_handoff_meta(p) for p in paths}
    index = build_handoff_id_index(paths, metas)

    holder = index.get(new_id)
    if holder is not None and holder != self_abs:
        return Refused("handoff-id-collision", f"{new_id} is already held by {holder}")

    old = fm.get("handoff_id")
    old_id = str(old).strip() if old is not None else ""
    if old_id and old_id != new_id:
        refs = _references_to(old_id, metas, self_abs)
        if refs:
            return Refused(
                "handoff-id-referenced",
                f"{old_id} is cited by {', '.join(sorted(refs))}",
            )
    return Resolved({"handoff_id": new_id})
