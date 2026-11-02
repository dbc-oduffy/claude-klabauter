"""Split one multi-item backlog row into per-item child rows and close the parent. Implements `split_row`.

Children are minted through the queue appender (the appender assigns the id = file stem), each tagged
`split-from:<parent>` and `split-item:<i>` at mint time and then stamped `split_from: <parent>`. The
parent gains `split_into: [...]` and closes via an atomic tmp+os.replace rewrite. Idempotent: a parent
already carrying `split_into` returns those ids and writes nothing; after a crash between minting and
the parent stamp, the tagged children are reused and only the missing indices are minted. Backlog rows
are not the grind ledger (`grind_rows._ledger_path`). No git spawn, no subprocess.
"""

from __future__ import annotations

import datetime
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from coordinator_core.frontmatter import primitives as fm_primitives
from coordinator_core.frontmatter import schema_validate
from coordinator_core.ops.queue_append import append_queue_entry
from coordinator_core.session.declared_writes import declare_write

MAX_SPLIT_ITEMS = 8
MIN_SPLIT_ITEMS = 2

CLOSED_BY = "grind-split"

# Grind profile name -> backlog schema (and `state/<schema>/` directory).
_PROFILE_SCHEMA = {
    "bug": "bug-backlog",
    "debt": "debt-backlog",
    "improvement": "improvement-queue",
}

_SCHEMA_REQUIRED_CARRY = {
    "bug-backlog": ("severity",),
    "debt-backlog": ("source", "risk", "proposed_action"),
    "improvement-queue": ("proposed_action", "from_repo", "change_kind"),
}
# Schemas whose required `surface` is taken from the item's declared files, falling back to the parent's.
_SURFACE_SCHEMAS = frozenset({"bug-backlog", "improvement-queue"})
_SEVERITY_DEFAULT = "P1"

_FROM_TAG = "split-from:"
_ITEM_TAG = "split-item:"


def _schema_for(profile: str) -> str:
    try:
        return _PROFILE_SCHEMA[profile]
    except KeyError:
        raise ValueError(
            f"split_row: no backlog schema for profile {profile!r}; known: {sorted(_PROFILE_SCHEMA)}"
        ) from None


def _check_items(items: Sequence[Mapping[str, Any]]) -> None:
    n = len(items)
    if n < MIN_SPLIT_ITEMS or n > MAX_SPLIT_ITEMS:
        raise ValueError(
            f"split_row: {n} items; need {MIN_SPLIT_ITEMS}..{MAX_SPLIT_ITEMS}"
        )
    for i, item in enumerate(items):
        title = item.get("title") if isinstance(item, Mapping) else None
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"split_row: item {i} has no title")


def _read(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def _write_atomic(path: Path, text: str, repo_root: Path) -> None:
    """Replace `path` with `text`; the tmp is unlinked on any failure, so no partial file survives."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    declare_write(path.relative_to(repo_root).as_posix())


def _flat(text: str) -> str:
    # The appender expands a literal backslash-n into a newline.
    return text.replace("\\n", " ")


def _child_body(row_id: str, parent_title: str, item: Mapping[str, Any]) -> str:
    parts = []
    evidence = item.get("evidence")
    if isinstance(evidence, str) and evidence.strip():
        parts.append(evidence.strip())
    files = [str(f) for f in (item.get("declared_files") or []) if str(f).strip()]
    if files:
        parts.append("Declared files:\n" + "\n".join(f"- {f}" for f in files))
    parts.append(f"Split from {row_id}: {parent_title}")
    return _flat("\n\n".join(parts))


def _item_tags(path: Path, row_id: str) -> dict[int, str] | None:
    """`{item index: row id}` entry source for a child of `row_id`; None when `path` is not one."""
    text = _read(path)
    if _FROM_TAG + row_id not in text:
        return None
    fm = schema_validate.parse_yaml(text)
    tags = fm.get("tags") if isinstance(fm, dict) else None
    if not isinstance(tags, list) or _FROM_TAG + row_id not in tags:
        return None
    for tag in tags:
        if isinstance(tag, str) and tag.startswith(_ITEM_TAG) and tag[len(_ITEM_TAG):].isdigit():
            return {int(tag[len(_ITEM_TAG):]): path.stem}
    return None


def _existing_children(queue_dir: Path, row_id: str) -> dict[int, str]:
    found: dict[int, str] = {}
    for path in sorted(queue_dir.glob("*.yaml")):
        if path.stem == row_id:
            continue
        hit = _item_tags(path, row_id)
        if hit:
            for index, child_id in hit.items():
                found.setdefault(index, child_id)
    return found


def _mint_child(
    repo_root: Path, schema: str, row_id: str, parent: dict, index: int, item: Mapping[str, Any]
) -> str:
    fields: dict[str, Any] = {
        "title": _flat(str(item["title"]).strip()),
        "body": _child_body(row_id, str(parent.get("title") or row_id), item),
        "status": "open",
        "tags": [f"{_FROM_TAG}{row_id}", f"{_ITEM_TAG}{index}"],
    }
    for key in _SCHEMA_REQUIRED_CARRY[schema]:
        value = parent.get(key)
        if value is None and key == "severity":
            value = _SEVERITY_DEFAULT
        if value is not None:
            fields[key] = value
    if schema in _SURFACE_SCHEMAS:
        files = [str(f) for f in (item.get("declared_files") or []) if str(f).strip()]
        fields["surface"] = ", ".join(files) if files else str(parent.get("surface") or row_id)
    result = append_queue_entry(schema, caller_worktree=repo_root, **fields)
    out_path = Path(result["out_path"])
    stamped = fm_primitives.insert_fm_field(_read(out_path), "split_from", row_id)
    _write_atomic(out_path, stamped, repo_root)
    return out_path.stem


def _close_parent(repo_root: Path, path: Path, child_ids: list[str]) -> None:
    text = _read(path)
    today = datetime.date.today().isoformat()
    text = fm_primitives.insert_fm_field_raw(text, "split_into", "[" + ", ".join(child_ids) + "]")
    for key, value in (("status", "closed"), ("closed_at", today), ("closed_by", CLOSED_BY)):
        if fm_primitives.read_fm_field(text, key) is not None:
            text = fm_primitives.replace_fm_field(text, key, value)
        else:
            text = fm_primitives.insert_fm_field(text, key, value)
    _write_atomic(path, text, repo_root)


def split_row(
    repo_root: Path,
    profile: str,
    row_id: str,
    items: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Mint one open child row per item and close `row_id`; returns the child ids in item order.

    Raises ValueError for fewer than `MIN_SPLIT_ITEMS` or more than `MAX_SPLIT_ITEMS` items, an item
    without a title, an unknown profile, or a missing parent row. A second call for the same row
    returns the same ids and writes nothing.
    """
    repo_root = Path(repo_root)
    schema = _schema_for(profile)
    queue_dir = repo_root / "state" / schema
    parent_path = queue_dir / f"{row_id}.yaml"
    if not parent_path.is_file():
        raise ValueError(f"split_row: no {schema} row {row_id!r} under {queue_dir}")
    parent = schema_validate.parse_yaml(_read(parent_path))
    if not isinstance(parent, dict):
        raise ValueError(f"split_row: {parent_path} is not a YAML mapping")
    prior = parent.get("split_into")
    if prior:
        return [str(x) for x in prior]
    _check_items(items)
    existing = _existing_children(queue_dir, row_id)
    ids: list[str] = []
    for index, item in enumerate(items):
        child = existing.get(index)
        if child is None:
            child = _mint_child(repo_root, schema, row_id, parent, index, item)
        ids.append(child)
    _close_parent(repo_root, parent_path, ids)
    return ids
