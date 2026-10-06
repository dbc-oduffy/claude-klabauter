"""Pure plan-frontmatter port for artifact.adopt: alias renames plus fill-if-absent.

Text-level only: no I/O, no minting, no session or git reads. The caller supplies every derived
value. Key order and comments are preserved; nothing round-trips through a YAML dumper.
"""
from __future__ import annotations

import re
from typing import Optional, Sequence, TypedDict

from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    insert_fm_field_raw,
    read_fm_field,
    replace_fm_field,
    unquote_yaml_scalar,
)

# alias key -> schema key; renamed only when the schema key is absent.
ALIAS_TABLE: dict[str, str] = {
    "date": "created",
    "authors": "author",
    "state": "status",
    "name": "title",
}

_H1_RE = re.compile(r"^# +(.+?)[ \t]*$", re.MULTILINE)


class PlanDerived(TypedDict, total=False):
    """Caller-supplied values; a missing or None entry is never filled."""

    created: Optional[str]
    author: Optional[str]
    branch: Optional[str]
    plan_id: Optional[str]
    deliverable_id: Optional[str]


def _dq(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _rename_key(fm: str, old: str, new: str) -> str:
    pattern = re.compile(r"^" + re.escape(old) + r":(?=[ \t]|\r?$)", re.MULTILINE)
    return pattern.sub(new + ":", fm, count=1)


def _first_h1(body_text: str) -> str | None:
    m = _H1_RE.search(body_text)
    return m.group(1) if m else None


def port_plan_frontmatter(
    fm_text: str | None,
    body_text: str,
    derived: PlanDerived,
    status_enum: Sequence[str],
) -> tuple[str, list[str]]:
    """Return ``(new_frontmatter_text, changes)``; ``changes`` holds ``key: before -> after`` lines.

    A present key is never overwritten, even one that looks wrong. ``status`` is never derived.
    ``fm_text=None`` builds a block, taking ``title`` from the first ``# `` line of ``body_text``
    (omitted when there is none). A present-but-null ``plan_id`` counts as present.
    """
    changes: list[str] = []
    if fm_text is None:
        fm = ""
        h1 = _first_h1(body_text)
        if h1 is not None:
            fm = insert_fm_field_raw(fm, "title", _dq(h1))
            changes.append(f"title: (absent) -> {_dq(h1)}")
    else:
        fm = fm_text

    for alias, target in ALIAS_TABLE.items():
        if read_fm_field(fm, alias) is not None and read_fm_field(fm, target) is None:
            fm = _rename_key(fm, alias, target)
            changes.append(f"{alias} -> {target}: renamed")

    raw_status = read_fm_field(fm, "status")
    if raw_status:
        status = unquote_yaml_scalar(raw_status)
        if status is not None and status not in status_enum:
            folded = status.strip().lower()
            if folded in status_enum:
                fm = replace_fm_field(fm, "status", folded)
                changes.append(f"status: {status} -> {folded}")

    def fill(key: str, value: str | None, raw: bool) -> None:
        nonlocal fm
        if not value or read_fm_field(fm, key) is not None:
            return
        if raw:
            fm = insert_fm_field_raw(fm, key, _dq(value))
            changes.append(f"{key}: (absent) -> {_dq(value)}")
        else:
            fm = insert_fm_field(fm, key, value)
            changes.append(f"{key}: (absent) -> {value}")

    fill("created", derived.get("created"), raw=False)
    fill("author", derived.get("author"), raw=False)
    fill("branch", derived.get("branch"), raw=True)
    fill("plan_id", derived.get("plan_id"), raw=True)
    fill("deliverable_id", derived.get("deliverable_id"), raw=True)
    if read_fm_field(fm, "initiative") is None:
        fm = insert_fm_field_raw(fm, "initiative", "null")
        changes.append("initiative: (absent) -> null")

    return (fm.lstrip("\n") if fm_text is None else fm), changes
