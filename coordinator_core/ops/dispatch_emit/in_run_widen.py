"""In-run footprint widening and re-sizing decisions for an engine run.

Pure, no git spawn. ``classify_block`` decides whether a row's ROW-LOCAL-BLOCK reply names one
in-ask file the run may add to its footprint (``Widen``) or must halt on (``Halt``);
``resize_verdict`` decides whether a t-shirt growth continues in-run or flags to the EM. The
emitted run script is JS, so ``classify_block_js`` renders the same decision from the same
constants. ``record_widen`` / ``widened_paths`` persist the widen list on the Python side only.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional, Sequence, Union

from coordinator_core.bash_guards.guard_doctrine_surface_bash_write import _ADVISORY_EXTRA_IDENTIFIERS
from coordinator_core.bash_guards.dispatch import _LOCAL_CONFIG_GOVERNED_SURFACE
from coordinator_core.write_guards.guard_doctrine_surface_edits import (
    _CONTENT_ONLY_SURFACES,
    _PER_REPO_SURFACES,
)

TSHIRT_ORDER = ("XS", "S", "M", "L", "XL", "XXL")
#: Growth of this many steps or more flags to the EM.
FLAG_STEPS = 4

WIDEN_RECORD_NAME = "widened-paths.jsonl"

_TOKEN = "ROW-LOCAL-BLOCK"
_NIL_VALUES = ("none", "n/a", "no")
_LINE_RE_SRC = r"^[*_]{0,2}" + _TOKEN + r"[*_]{0,2}:[ \t]*(.*)$"
_SHAPE_RE_SRC = r"^[A-Za-z0-9_.@+-][A-Za-z0-9_.@+/-]*$"
_STRIP_CHARS = "*_`'\" \t\r"

# Doctrine surfaces come from the doctrine-surface guards' own constants; state/ and .git/ are
# the engine's own stores.
_ENGINE_PREFIXES = ("state/", ".git/")
_SURFACE_PATHS = tuple(
    "/".join(parts) for parts in (*_CONTENT_ONLY_SURFACES, *_PER_REPO_SURFACES)
) + (_LOCAL_CONFIG_GOVERNED_SURFACE,)
_SURFACE_NAMES = tuple(sorted({p.rsplit("/", 1)[-1].lower() for p in _SURFACE_PATHS}))
_DOCTRINE_PREFIXES = tuple(i.lower() for i in _ADVISORY_EXTRA_IDENTIFIERS)
_DEFAULT_DENY_PREFIXES = tuple(p.lower() for p in _ENGINE_PREFIXES) + _DOCTRINE_PREFIXES


@dataclass(frozen=True)
class Widen:
    """The row may add ``path`` (repo-relative, posix) to its footprint."""

    path: str


@dataclass(frozen=True)
class Halt:
    """The block stays a halt to the EM; ``reason`` is the one fact that decided it."""

    reason: str


def _reply_value(reason_text: str) -> "tuple[Optional[str], str]":
    """The single ROW-LOCAL-BLOCK value in a reply, else (None, why-not)."""
    text = reason_text.replace("\\n", "\n")
    values = []
    for match in re.finditer(_LINE_RE_SRC, text, re.MULTILINE):
        value = match.group(1).strip(_STRIP_CHARS)
        if value and value.lower() not in _NIL_VALUES:
            values.append(value)
    if not values:
        return None, "no ROW-LOCAL-BLOCK value"
    if len(set(values)) > 1:
        return None, "several ROW-LOCAL-BLOCK values"
    return values[0], ""


def _shape_halt(value: str, repo_root: Union[str, Path]) -> str:
    """Why ``value`` is not one repo-relative path inside the repo root, '' when it is."""
    if not re.match(_SHAPE_RE_SRC, value) or value.endswith("/"):
        return f"not one repo-relative path: {value}"
    if any(seg in ("", ".", "..") for seg in value.split("/")):
        return f"path escapes or is not normalized: {value}"
    root = PurePosixPath(Path(repo_root).as_posix())
    if root.joinpath(value).parts[: len(root.parts)] != root.parts:
        return f"path outside the repo root: {value}"
    return ""


def _denied(value: str, deny_prefixes: Iterable[str]) -> str:
    low = value.lower()
    for prefix in deny_prefixes:
        if low.startswith(prefix.lower()) or ("/" + prefix.lower()) in "/" + low:
            return f"denied surface: {prefix}"
    if low in (p.lower() for p in _SURFACE_PATHS) or low.rsplit("/", 1)[-1] in _SURFACE_NAMES:
        return f"doctrine surface: {value}"
    return ""


def classify_block(
    reason_text: str, repo_root: Union[str, Path], held_paths: Iterable[str]
) -> Union[Widen, Halt]:
    """``Widen(path)`` only when the reply's ROW-LOCAL-BLOCK value names one repo-relative path
    inside ``repo_root``, outside state/ and the doctrine surfaces, and not in ``held_paths``.
    The contract is deny-list only: any other repo path widens, with no in-ask membership test."""
    value, why = _reply_value(reason_text or "")
    if value is None:
        return Halt(why)
    bad = _shape_halt(value, repo_root) or _denied(value, _DEFAULT_DENY_PREFIXES)
    if bad:
        return Halt(bad)
    if value.lower() in {str(h).replace("\\", "/").lower() for h in held_paths}:
        return Halt(f"peer-held path: {value}")
    return Widen(value)


def resize_verdict(old_tshirt: str, new_tshirt: str) -> str:
    """``continue`` for growth of 1-3 steps (or none), ``flag`` for 4+ steps, an unknown size, or
    landing on XXL from below XL."""
    try:
        old = TSHIRT_ORDER.index(str(old_tshirt).strip().upper())
        new = TSHIRT_ORDER.index(str(new_tshirt).strip().upper())
    except ValueError:
        return "flag"
    if new - old >= FLAG_STEPS:
        return "flag"
    if new == len(TSHIRT_ORDER) - 1 and old < len(TSHIRT_ORDER) - 2:
        return "flag"
    return "continue"


def record_widen(run_dir: Union[str, Path], row_id: str, path: str) -> None:
    """Append ``{row, path}`` to the run's widen record; idempotent per (row, path)."""
    run = Path(run_dir)
    if any(r == {"row": row_id, "path": path} for r in _records(run)):
        return
    run.mkdir(parents=True, exist_ok=True)
    with (run / WIDEN_RECORD_NAME).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"row": row_id, "path": path}, sort_keys=True) + "\n")


def record_widens(run_dir: Union[str, Path], items: object) -> int:
    """Record each well-formed ``{row, path}`` of ``items`` (the run script's widen list) through
    ``record_widen``; a malformed item or an unsafe path is skipped. Returns the count recorded."""
    count = 0
    for item in items if isinstance(items, list) else ():
        row = item.get("row") if isinstance(item, dict) else None
        path = item.get("path") if isinstance(item, dict) else None
        if not (isinstance(row, str) and isinstance(path, str) and row):
            continue
        if (
            re.match(_SHAPE_RE_SRC, path)
            and not path.endswith("/")
            and not any(seg in ("", ".", "..") for seg in path.split("/"))
            and not _denied(path, _DEFAULT_DENY_PREFIXES)
        ):
            record_widen(run_dir, row, path)
            count += 1
    return count


def _records(run_dir: Path) -> "list[dict]":
    try:
        lines = (run_dir / WIDEN_RECORD_NAME).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and isinstance(rec.get("row"), str) and isinstance(rec.get("path"), str):
            out.append(rec)
    return out


def widened_paths(run_dir: Union[str, Path]) -> "list[str]":
    """Every recorded widened path, first-seen order, deduplicated."""
    seen: "list[str]" = []
    for rec in _records(Path(run_dir)):
        if rec["path"] not in seen:
            seen.append(rec["path"])
    return seen


def widened_by_row(run_dir: Union[str, Path]) -> "dict[str, list[str]]":
    """``{row id: [paths]}`` from the widen record, first-seen order, deduplicated per row."""
    out: "dict[str, list[str]]" = {}
    for rec in _records(Path(run_dir)):
        paths = out.setdefault(rec["row"], [])
        if rec["path"] not in paths:
            paths.append(rec["path"])
    return out


def classify_block_js(repo_root_rel_denylist: Optional[Sequence[str]] = None) -> str:
    """JS source of ``classifyBlock(replyText, heldPaths) -> {widen: path} | {halt: reason}``,
    generated from this module's constants. ``repo_root_rel_denylist`` adds repo-relative deny
    prefixes to the defaults (state/, .git/, the doctrine rules trees). Repo-root containment is
    lexical here: the JS accepts only a normalized relative path, so it needs no root."""
    deny = list(_DEFAULT_DENY_PREFIXES) + [p.lower() for p in (repo_root_rel_denylist or ())]
    paths = [p.lower() for p in _SURFACE_PATHS]
    return (
        "function classifyBlock(replyText, heldPaths) {\n"
        f"  const lineRe = new RegExp({json.dumps(_LINE_RE_SRC)}, 'gm');\n"
        f"  const shapeRe = new RegExp({json.dumps(_SHAPE_RE_SRC)});\n"
        f"  const nil = {json.dumps(list(_NIL_VALUES))};\n"
        f"  const strip = {json.dumps(_STRIP_CHARS)};\n"
        f"  const deny = {json.dumps(deny)};\n"
        f"  const surfacePaths = {json.dumps(paths)};\n"
        f"  const surfaceNames = {json.dumps(list(_SURFACE_NAMES))};\n"
        "  const text = String(replyText || '').split('\\\\n').join('\\n');\n"
        "  const values = [];\n"
        "  for (const m of text.matchAll(lineRe)) {\n"
        "    let v = m[1];\n"
        "    while (v.length && strip.includes(v[0])) v = v.slice(1);\n"
        "    while (v.length && strip.includes(v[v.length - 1])) v = v.slice(0, -1);\n"
        "    if (v && !nil.includes(v.toLowerCase())) values.push(v);\n"
        "  }\n"
        "  if (!values.length) return { halt: 'no ROW-LOCAL-BLOCK value' };\n"
        "  if (new Set(values).size > 1) return { halt: 'several ROW-LOCAL-BLOCK values' };\n"
        "  const value = values[0];\n"
        "  if (!shapeRe.test(value) || value.endsWith('/')) return { halt: 'not one repo-relative path: ' + value };\n"
        "  if (value.split('/').some((s) => s === '' || s === '.' || s === '..')) return { halt: 'path escapes or is not normalized: ' + value };\n"
        "  const low = value.toLowerCase();\n"
        "  for (const p of deny) {\n"
        "    if (low.startsWith(p) || ('/' + low).includes('/' + p)) return { halt: 'denied surface: ' + p };\n"
        "  }\n"
        "  if (surfacePaths.includes(low) || surfaceNames.includes(low.split('/').pop())) return { halt: 'doctrine surface: ' + value };\n"
        "  const held = (heldPaths || []).map((h) => String(h).split('\\\\').join('/').toLowerCase());\n"
        "  if (held.includes(low)) return { halt: 'peer-held path: ' + value };\n"
        "  return { widen: value };\n"
        "}\n"
    )
