"""Shared, spawn-free predicates for UE `.uproject` detection.

Invariant: all three `.uproject` probes (SessionStart hook, orientation cache
generator, cache-sync verifier) route through here, so a skip rule never lives
in one probe alone. Stdlib only: no subprocess, no YAML library.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from coordinator_core.resolve_validation_cmd import _extract_frontmatter

SKIP_SEGMENTS: frozenset[str] = frozenset({"tests", "fixtures"})

_KEY_RE = re.compile(r"^(project_type|project_subtypes)\s*:\s*(.*)$")


def is_skipped_path(rel_path: str) -> bool:
    """True when a directory segment of the repo-relative path is in SKIP_SEGMENTS.

    The filename segment is excluded; match is whole-segment only.
    """
    parts = rel_path.replace("\\", "/").split("/")
    return any(p in SKIP_SEGMENTS for p in parts[:-1])


def _scalar(raw: str) -> str:
    val = raw.strip()
    if val[:1] in ("'", '"'):
        end = val.find(val[0], 1)
        return val[1:end] if end > 0 else val[1:]
    return val.split(" #", 1)[0].strip()


def _flow_items(raw: str) -> list[str]:
    inner = raw.split("#", 1)[0].strip()[1:]
    inner = inner.rsplit("]", 1)[0]
    return [s for s in (_scalar(p) for p in inner.split(",")) if s]


def declared_ue(root: str | os.PathLike) -> bool | None:
    """Whether `<root>/coordinator.local.md` frontmatter declares UE.

    None: file missing/unreadable or neither project_type nor project_subtypes
    set. True: project_type is `unreal` or `unreal` is in project_subtypes.
    False: any other declaration.
    """
    path = Path(root) / "coordinator.local.md"
    try:
        if not path.is_file():
            return None
        fm = _extract_frontmatter(path)
    except OSError:
        return None

    values: list[str] = []
    lines = fm.splitlines()
    i = 0
    while i < len(lines):
        m = _KEY_RE.match(lines[i])
        i += 1
        if not m:
            continue
        key, rest = m.group(1), m.group(2).strip()
        if rest.startswith("["):
            values.extend(_flow_items(rest))
        elif rest and not rest.startswith("#"):
            val = _scalar(rest)
            if val:
                values.append(val)
        elif key == "project_subtypes":
            while i < len(lines):
                item = lines[i].strip()
                if not item.startswith("-"):
                    break
                val = _scalar(item[1:])
                if val:
                    values.append(val)
                i += 1
    if not values:
        return None
    return "unreal" in values
