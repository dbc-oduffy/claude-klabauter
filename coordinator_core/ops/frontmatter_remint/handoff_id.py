"""handoff_id resolver for `frontmatter.remint`: the wire-derived id, never a fabricated one.

The value is `_derive_handoff_id(repo, path)[0]` with `repo` the emitting repo's own
slug, because the hash input is `f"{repo}:{basename}"` and any other label mints an id
the cockpit wire never publishes. The mint site is guarded here, not left to the schema.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping

import yaml

from coordinator_core.dag import (
    build_handoff_id_index,
    read_handoff_meta,
    scan_repo_handoff_corpus,
)
from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    read_fm_field,
    rebuild,
    replace_fm_field,
    serialize_yaml_scalar,
    split_frontmatter,
    write_fm_nested_field,
)
from coordinator_core.locked_write import MutateAbort, locked_rmw
from coordinator_core.ops.emit.context import resolve_repo_name
from coordinator_core.ops.emit.sections.handoffs import _derive_handoff_id
from coordinator_core.ops.frontmatter_remint._contract import Refused, Resolved

_MINTED_ID_RE = re.compile(r"^hnd-(?!placeholder-replace-with)[a-z0-9-]+-[0-9a-f]{6}$")

# Gravestone (DR-344): the `scan_citers` git-grep over state/ + archive/handoffs measured 484-719ms
# process time, over the 500ms kill bar, and was deleted. No caller needs citers: `resolve` keeps the
# old id as an alias that every handoff_id index resolves, so no citer dangles.


def resolve(path: Path, fm: Mapping[str, Any], repo_root: Path) -> Resolved | Refused:
    """Derive the wire handoff_id for `path`; a changed id keeps the old one as an alias, so no citer dangles.

    Refuses if underivable or colliding (the new id, or the old id claimed by another record).
    """
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
        for other, meta in metas.items():
            if other != self_abs and old_id in ({str(meta.get("handoff_id") or "").strip()} | set(_aliases(meta))):
                return Refused("handoff-id-collision", f"{old_id} is already claimed by {other}")
        return Resolved({"handoff_id": new_id, "handoff_id_aliases": sorted(set(_aliases(fm)) | {old_id})})
    return Resolved({"handoff_id": new_id})


def _aliases(fm: Mapping[str, Any]) -> list[str]:
    """`handoff_id_aliases` as stripped non-empty strings; absent, None or a scalar tolerated."""
    raw = fm.get("handoff_id_aliases")
    items = raw if isinstance(raw, list) else [raw]
    return [s for s in (str(i).strip() for i in items if i is not None) if s]


def write(path: Path, repo_root: Path) -> Resolved | Refused:
    """Resolve, then write `handoff_id` and the alias list.

    Owns its writer because the handler's scalar path would quote the alias list into a string.
    """
    split = split_frontmatter(path.read_text(encoding="utf-8"))
    fm = yaml.safe_load(split.fm_text) if split is not None else None
    res = resolve(path, fm if isinstance(fm, dict) else {}, repo_root)
    if isinstance(res, Refused):
        return res

    def mutate(old_text: str) -> str:
        cur = split_frontmatter(old_text)
        if cur is None:
            raise MutateAbort(f"no parseable YAML frontmatter in {path.name}")
        text = cur.fm_text
        for key, val in res.value.items():
            if isinstance(val, list):
                text = write_fm_nested_field(text, key, "".join(f"  - {serialize_yaml_scalar(v)}\n" for v in val))
            elif read_fm_field(text, key) is None:
                text = insert_fm_field(text, key, val)
            else:
                text = replace_fm_field(text, key, val)
        return rebuild(cur, text)

    locked_rmw(path, mutate, repo_root=repo_root)
    return res
