"""
coordinator_core.symbol_knowledge.batch_context -- the shared context a batch prompt carries.

Purpose: ``resolve`` reads each target file once per batch and returns, per file, its import map, the
signature and first docstring line of its neighbour symbols, and each target symbol's source span
and body hash; ``render_block`` turns one file's context into the prompt block.

Negative-spec:
  - Reads only the named files, once each; spawns no process and makes no model call.
  - A target whose span exceeds ``PER_SYMBOL_TOKEN_CAP`` is deferred, never truncated; ``fit`` hands
    back the deferred names next to the kept ones.
  - Neighbour overflow past ``NEIGHBOUR_LIMIT`` is stated in the block ("+N more"), never dropped
    silently.
  - A file that is missing, unparseable or escapes the root yields an empty context; its targets
    carry no span and stay in the batch.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

PER_SYMBOL_TOKEN_CAP = 1500
BATCH_TOKEN_BUDGET = 24000
NEIGHBOUR_LIMIT = 40
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Rough token count (4 chars per token), rounded up; used only to budget a batch."""
    return -(-len(text) // CHARS_PER_TOKEN)


@dataclass(frozen=True)
class TargetSpan:
    """One target symbol's source: 1-based inclusive lines, the text, and sha256 of its UTF-8 bytes."""

    lineno: int
    end_lineno: int
    source: str
    body_hash: str


@dataclass
class FileContext:
    """Everything one file contributes to a batch prompt."""

    path: str
    imports: "list[str]" = field(default_factory=list)
    neighbours: "list[str]" = field(default_factory=list)
    spans: "dict[str, TargetSpan]" = field(default_factory=dict)


def _descriptor_parts(qualified_name: str) -> "list[str]":
    """Trailing name components of a rag qualified name or a SCIP symbol string, outermost first."""
    text = qualified_name
    if "/" in text:
        text = text.rsplit("/", 1)[1]
        text = text.rstrip(".").removesuffix("()").rstrip("#.")
        return [p for p in text.replace("#", ".").split(".") if p]
    return [p for p in text.split(".") if p]


def _find(tree: ast.Module, parts: Sequence[str]) -> "ast.AST | None":
    """The top-level function/class (or one method of a class) the trailing *parts* name."""
    if not parts:
        return None
    top = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    if len(parts) >= 2 and isinstance(top.get(parts[-2]), ast.ClassDef):
        for item in top[parts[-2]].body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == parts[-1]:
                return item
    return top.get(parts[-1])


def _imports(tree: ast.Module) -> "list[str]":
    out = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            out.extend(a.name + (f" as {a.asname}" if a.asname else "") for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mod = "." * node.level + (node.module or "")
            out.append(mod + ": " + ", ".join(a.name for a in node.names))
    return list(dict.fromkeys(out))


def _neighbours(table: Mapping[str, Any], skip: "set[str]") -> "list[str]":
    lines = []

    def line(name: str, signature: str, doc: Any) -> None:
        first = (doc or "").strip().splitlines()[0] if doc else ""
        lines.append(f"{signature}" + (f" -- {first}" if first else ""))

    for fn in table.get("functions", []):
        if fn["name"] not in skip:
            line(fn["name"], fn.get("signature") or f"def {fn['name']}", fn.get("docstring"))
    for cls in table.get("classes", []):
        if cls["name"] not in skip:
            line(cls["name"], f"class {cls['name']}", cls.get("docstring"))
        for m in cls.get("methods", []):
            if f"{cls['name']}.{m['name']}" not in skip:
                line(m["name"], f"{m.get('signature') or 'def ' + m['name']}  [in {cls['name']}]", m.get("docstring"))
    return lines


def resolve(root: "str | Path", targets: Sequence[Mapping[str, Any]]) -> "dict[str, FileContext]":
    """Per-file context for every distinct ``path`` in *targets* (each with ``qualified_name``),
    computed once; the key is the repo-relative path."""
    from coordinator_core.cartography.symbols import build_symbols

    by_path: "dict[str, list[str]]" = {}
    for t in targets:
        by_path.setdefault(str(t["path"]), []).append(str(t["qualified_name"]))
    out: "dict[str, FileContext]" = {}
    for path, names in by_path.items():
        ctx = FileContext(path)
        out[path] = ctx
        try:
            source = (Path(root) / path).read_text(encoding="utf-8")
            tree = ast.parse(source)
            table = build_symbols(root, [path])["files"][0]
        except Exception:  # noqa: BLE001 -- empty context, targets stay
            continue
        lines = source.splitlines()
        skip: "set[str]" = set()
        for name in names:
            parts = _descriptor_parts(name)
            node = _find(tree, parts)
            if node is None or getattr(node, "end_lineno", None) is None:
                continue
            start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
            text = "\n".join(lines[start - 1 : node.end_lineno])
            ctx.spans[name] = TargetSpan(start, node.end_lineno, text, hashlib.sha256(text.encode("utf-8")).hexdigest())
            is_method = len(parts) >= 2 and not any(node is n for n in tree.body)
            skip.add(f"{parts[-2]}.{parts[-1]}" if is_method else parts[-1])
        ctx.imports = _imports(tree)
        ctx.neighbours = _neighbours(table, skip)
    return out


def fit(
    symbols: Sequence[Mapping[str, Any]],
    contexts: Mapping[str, FileContext],
    budget: int = BATCH_TOKEN_BUDGET,
) -> "tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]":
    """Split *symbols* into ``(kept, deferred)``: a symbol whose span is over the per-symbol cap, or
    that would push the running span total past *budget*, is deferred whole."""
    kept: "list[Mapping[str, Any]]" = []
    deferred: "list[Mapping[str, Any]]" = []
    used = 0
    for s in symbols:
        ctx = contexts.get(str(s["path"]))
        span = ctx.spans.get(str(s["qualified_name"])) if ctx else None
        cost = estimate_tokens(span.source) if span else 0
        if cost > PER_SYMBOL_TOKEN_CAP or used + cost > budget:
            deferred.append(s)
            continue
        used += cost
        kept.append(s)
    return kept, deferred


def render_block(ctx: FileContext, names: Sequence[str]) -> str:
    """One shared-context block for *ctx*: imports, neighbour signatures, and the spans of *names*."""
    parts = [f"### {ctx.path}"]
    if ctx.imports:
        parts.append("imports: " + "; ".join(ctx.imports))
    if ctx.neighbours:
        shown = ctx.neighbours[:NEIGHBOUR_LIMIT]
        parts.append("neighbours:\n" + "\n".join(f"  {n}" for n in shown))
        if len(ctx.neighbours) > len(shown):
            parts.append(f"  (+{len(ctx.neighbours) - len(shown)} more neighbours not shown)")
    for name in names:
        span = ctx.spans.get(name)
        if span:
            parts.append(f"source of {name} (lines {span.lineno}-{span.end_lineno}):\n{span.source}")
        else:
            parts.append(f"source of {name}: unavailable")
    return "\n".join(parts)
