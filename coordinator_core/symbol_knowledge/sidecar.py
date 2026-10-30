"""
coordinator_core.symbol_knowledge.sidecar -- read and write ``state/symbol-knowledge/definitions.json``.

Purpose: one JSON object per repo, ``{qualified_name: {"definition", "sha", "traps"}}``, keyed by
rag's qualified-name grammar. Only the detached batch job calls ``write_sidecar``; the close leg
reads at most.

Negative-spec:
  - Writes are atomic (same-directory temp + replace) with sorted keys, so a re-write of equal
    content is byte-identical.
  - A missing or unparseable file reads as empty; a malformed entry is dropped on read.
  - ``traps`` holds each line once, in first-seen order.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

SIDECAR_REL = "state/symbol-knowledge/definitions.json"
ENTRY_FIELDS = ("definition", "sha", "traps")


def sidecar_path(root: "str | Path") -> Path:
    return Path(root) / SIDECAR_REL


def _clean_traps(lines: Iterable[Any]) -> "list[str]":
    return list(dict.fromkeys(str(t).strip() for t in lines if str(t).strip()))


def make_entry(definition: str, sha: str, traps: Iterable[str] = ()) -> "dict[str, Any]":
    """A well-formed entry with keys in the stable ``ENTRY_FIELDS`` order."""
    return {"definition": str(definition), "sha": str(sha), "traps": _clean_traps(traps)}


def _valid_entry(value: Any) -> Optional["dict[str, Any]"]:
    if not isinstance(value, dict):
        return None
    definition, sha, traps = value.get("definition"), value.get("sha"), value.get("traps", [])
    if not isinstance(definition, str) or not isinstance(sha, str) or not isinstance(traps, list):
        return None
    return make_entry(definition, sha, traps)


def read_sidecar(root: "str | Path") -> "dict[str, dict[str, Any]]":
    """The sidecar's entries; ``{}`` when the file is absent or unreadable."""
    try:
        data = json.loads(sidecar_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: "dict[str, dict[str, Any]]" = {}
    for key, value in data.items():
        entry = _valid_entry(value)
        if isinstance(key, str) and key and entry is not None:
            out[key] = entry
    return out


def merge_entries(
    existing: Mapping[str, Mapping[str, Any]],
    definitions: Mapping[str, "tuple[str, str]"],
    traps: Optional[Mapping[str, Iterable[str]]] = None,
) -> "dict[str, dict[str, Any]]":
    """*existing* with ``definitions`` (``qualified_name -> (definition, sha)``) replaced and
    ``traps`` (``qualified_name -> lines``) appended unique to the entry's trap list. A trap for a
    symbol with no definition yet keeps an empty definition and the sha ``""``."""
    merged = {k: dict(v) for k, v in existing.items()}
    for name, (definition, sha) in definitions.items():
        prior = merged.get(name, {})
        merged[name] = make_entry(definition, sha, prior.get("traps", ()))
    for name, lines in (traps or {}).items():
        prior = merged.get(name, {"definition": "", "sha": "", "traps": []})
        merged[name] = make_entry(prior.get("definition", ""), prior.get("sha", ""), [*prior.get("traps", ()), *lines])
    return merged


def render(entries: Mapping[str, Mapping[str, Any]]) -> str:
    """The canonical file text: sorted symbol keys, ``ENTRY_FIELDS`` order inside each entry."""
    ordered = {
        name: {f: entries[name][f] for f in ENTRY_FIELDS} for name in sorted(entries)
    }
    return json.dumps(ordered, indent=2, ensure_ascii=False) + "\n"


def write_sidecar(root: "str | Path", entries: Mapping[str, Mapping[str, Any]]) -> Path:
    """Atomically write *entries* to the sidecar; returns its path."""
    from coordinator_core.atomic_replace import atomic_write_bytes

    path = sidecar_path(root)
    atomic_write_bytes(path, render(entries).encode("utf-8"))
    return path
