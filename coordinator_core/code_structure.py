"""Optional code-structure provider: example-retrieval-repo when it answers for this repo, pure Python otherwise.

`facts_for` is the one interface the update-docs and distill ops consume. It never raises on an
unavailable rag: the answer then carries `covered: False` and a reason, with symbols still filled
by the no-subprocess fallback (`ast` top-level names for .py, headings for .md).
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

from coordinator_core.architecture_survey import structure
from coordinator_core.architecture_survey.structure import Post, StructureUnavailable

PENDING_REASON = "batched-structure-door-pending"
NEED = "code-structure"
RAG_TIMEOUT_MS = 150
_COSTS_PATH = Path(structure.__file__).parent / "rag_read_costs.json"
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")


def _py_symbols(text: str) -> list[str]:
    names: list[str] = []
    for node in ast.parse(text).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            names.extend(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
    return names


def _md_symbols(text: str) -> list[str]:
    names: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and (m := _HEADING.match(line)):
            names.append(m.group(1))
    return names


def _fallback_symbols(repo_root: Path, path: str) -> list[str]:
    """Top-level names for one path; unreadable, unparsable or unsupported files yield []."""
    suffix = Path(path).suffix.lower()
    if suffix not in (".py", ".md"):
        return []
    try:
        text = (repo_root / path).read_text(encoding="utf-8")
        return _py_symbols(text) if suffix == ".py" else _md_symbols(text)
    except (OSError, UnicodeError, SyntaxError, ValueError):
        return []


def _rag_rows(envelope: dict[str, Any]) -> dict[str, dict[str, Any]]:
    data = envelope.get("data") or {}
    blocks = data.get("results") if isinstance(data.get("results"), list) else [data]
    rows: dict[str, dict[str, Any]] = {}
    for block in blocks:
        if isinstance(block, dict) and isinstance(block.get("file"), str):
            rows[block["file"]] = block
    return rows


def _names(items: Any) -> list[str]:
    return [i if isinstance(i, str) else str(i.get("name")) for i in items or [] if isinstance(i, (str, dict)) and (isinstance(i, str) or i.get("name"))]


def _load_costs() -> dict[str, Any]:
    return json.loads(_COSTS_PATH.read_text(encoding="utf-8"))


def _batched_door(costs: dict[str, Any]) -> str | None:
    """The batched symbols/referencers door, only when the cost record names it and marks it within the bar."""
    name = costs.get("needs", {}).get(NEED, {}).get("batched_door")
    if name and costs.get("doors", {}).get(name, {}).get("cost_class") == "within":
        return str(name)
    return None


def _from_rag(repo_root: str, paths: list[str], post: Post | None) -> dict[str, dict[str, Any]]:
    """No call unless the cost record names a within-bar batched door; then one identity probe plus one batched call."""
    costs = _load_costs()
    door = _batched_door(costs)
    if door is None:
        raise StructureUnavailable(PENDING_REASON)
    binding_door = costs["transport"]["project_binding"]["identity_door"]
    costs["doors"][binding_door]["timeout_ms"] = RAG_TIMEOUT_MS
    post_fn = post or structure._default_post
    url = structure._address(costs)
    try:
        structure._identity(post_fn, url, repo_root, costs)
        envelope = structure._call(post_fn, url, door, {"paths": paths}, RAG_TIMEOUT_MS)
    except (TimeoutError, OSError, ValueError, KeyError) as exc:
        raise StructureUnavailable(f"rag call failed: {type(exc).__name__}") from exc
    if structure._degraded(envelope):
        raise StructureUnavailable("rag door degraded")
    return _rag_rows(envelope)


def facts_for(repo_root: str | Path, paths: list[str], *, post: Post | None = None) -> dict[str, Any]:
    """Symbols and referencers per path: `{covered, reason, symbols, referencers}`.

    covered is True only when rag answered; a path rag returned no row for falls back per path.
    Never raises for an unavailable rag; covered is False with the reason and the fallback fills
    symbols (referencers stay empty: the fallback spawns nothing).
    """
    root = str(repo_root)
    paths = list(dict.fromkeys(paths))
    rows: dict[str, dict[str, Any]] = {}
    reason: str | None = None
    try:
        rows = _from_rag(root, paths, post)
    except StructureUnavailable as exc:
        reason = str(exc)
    symbols: dict[str, list[str]] = {}
    referencers: dict[str, list[str]] = {}
    for p in paths:
        row = rows.get(p)
        if row is not None:
            symbols[p] = _names(row.get("symbols"))
            referencers[p] = _names(row.get("referencers"))
        else:
            symbols[p] = _fallback_symbols(Path(root), p)
            referencers[p] = []
    return {"covered": reason is None, "reason": reason, "symbols": symbols, "referencers": referencers}
