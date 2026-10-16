"""Design-registry reader and lookup: a stdlib mirror of DoE's `design-registry/1` reader.

Contract: coordinator-content-repo `coordinator/docs/wiki/schema-and-validation-contracts/design-registry.md`.
Reference reader (read at 5eba5f50de, never imported): `coordinator/hooks/scripts/_design_registry_gate.py`.
`load` and `matches` keep DoE's names, keys and accept/reject rules; `lookup` adds the hit layer,
renaming `board`/`harvest` to `board_url`/`harvest_path`. File reads only, no process spawn.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter

SCHEMA_ID = "design-registry/1"
DEFAULT_PATH = "docs/design/registry.yaml"
LOCAL_KEY = "design_registry_path"
UNPARSEABLE = "registry-unparseable"

_STATUSES = ("designed", "partial", "implemented", "deviated")
_KEY_RE = re.compile(r"^([A-Za-z0-9_\-]+):(?:[ \t]+(.*))?$")
_BLOCK_SCALAR_RE = re.compile(r"^[|>][+\-0-9]*$")
_URL_RE = re.compile(r"claude\.ai/artifact/([A-Za-z0-9_-]+)")
_BOM = "﻿"


class _SubsetError(ValueError):
    pass


def _strip_comment(value: str) -> str:
    quote = ""
    for i, ch in enumerate(value):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#" and (i == 0 or value[i - 1] in " \t"):
            return value[:i]
    return value


def _split_flow(body: str) -> list:
    items: list = []
    cur: list = []
    quote = ""
    for ch in body:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
            cur.append(ch)
        elif ch == ",":
            items.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    items.append("".join(cur))
    return [i.strip() for i in items if i.strip()]


def _scalar(raw: str):
    value = _strip_comment(raw).strip()
    if not value:
        return ""
    if value[0] in ("'", '"'):
        close = value.find(value[0], 1)
        if close == -1 or value[close + 1 :].strip():
            raise _SubsetError("unterminated or trailing-text quoted scalar")
        return value[1:close]
    if value[0] in "&*!%@`" or _BLOCK_SCALAR_RE.match(value) or value[0] == "{":
        raise _SubsetError("scalar outside the subset")
    if value in ("null", "~"):
        return None
    return value


def _value(raw: str):
    value = _strip_comment(raw).strip()
    if value.startswith("["):
        if not value.endswith("]"):
            raise _SubsetError("unterminated flow list")
        return [_scalar(i) for i in _split_flow(value[1:-1])]
    return _scalar(raw)


def _indent_of(line: str) -> int:
    if line[: len(line) - len(line.lstrip())].count("\t"):
        raise _SubsetError("tab indentation")
    return len(line) - len(line.lstrip(" "))


def _parse_registry(text: str) -> dict:
    lines = text.lstrip(_BOM).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    top: dict = {}
    entries: Optional[list] = None
    entry: Optional[dict] = None
    dash_indent = key_indent = -1
    pending: Optional[str] = None
    skipping_top = False
    for raw in lines:
        stripped = _strip_comment(raw).rstrip()
        if not stripped.strip():
            continue
        indent = _indent_of(stripped)
        body = stripped.strip()
        is_item = body == "-" or body.startswith("- ")
        if indent == 0 and not (is_item and entries is not None):
            if body.startswith("-"):
                raise _SubsetError("list item at column zero")
            m = _KEY_RE.match(body)
            if not m:
                raise _SubsetError("top-level line is not `key: value`")
            key, rest = m.group(1), m.group(2)
            entry, pending, skipping_top = None, None, False
            if key == "entries":
                if rest:
                    raise _SubsetError("`entries` must be a block list")
                entries = []
                dash_indent = -1
            elif rest:
                top[key] = _value(rest)
            else:
                skipping_top = True
            continue
        if skipping_top:
            continue
        if entries is None:
            raise _SubsetError("indented line outside `entries`")
        if is_item:
            if dash_indent == -1:
                dash_indent = indent
            if indent == dash_indent:
                inner = body[1:].strip()
                m = _KEY_RE.match(inner)
                if not m:
                    raise _SubsetError("entry item is not a mapping")
                entry = {}
                entries.append(entry)
                key_indent = indent + (len(body) - len(body[1:].lstrip()))
                pending = None
                key, rest = m.group(1), m.group(2)
                if rest:
                    entry[key] = _value(rest)
                else:
                    entry[key] = []
                    pending = key
                continue
            if entry is not None and pending is not None and indent > dash_indent:
                item = body[1:].strip()
                if _KEY_RE.match(item) and not item.startswith(("'", '"')) and ": " in item:
                    raise _SubsetError("nesting deeper than the subset")
                entry[pending].append(_scalar(item))
                continue
            raise _SubsetError("unexpected list item")
        if entry is None:
            raise _SubsetError("mapping line before the first entry")
        if indent != key_indent:
            raise _SubsetError("entry key is not aligned")
        m = _KEY_RE.match(body)
        if not m:
            raise _SubsetError("entry line is not `key: value`")
        key, rest = m.group(1), m.group(2)
        pending = None
        if rest:
            entry[key] = _value(rest)
        else:
            entry[key] = []
            pending = key
    top["entries"] = entries if entries is not None else None
    return top


def _norm(path: str) -> str:
    path = path.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _validate_entries(top: dict) -> list:
    if top.get("schema") != SCHEMA_ID:
        raise _SubsetError("schema is not design-registry/1")
    raw_entries = top.get("entries")
    if not isinstance(raw_entries, list):
        raise _SubsetError("entries is missing")
    out: list = []
    seen: set = set()
    for raw in raw_entries:
        ident = raw.get("id")
        if not isinstance(ident, str) or not ident or ident in seen:
            raise _SubsetError("entry id missing or repeated")
        seen.add(ident)
        board = raw.get("board")
        match = _URL_RE.search(board) if isinstance(board, str) else None
        if match is None or board[match.end() :].strip():
            raise _SubsetError("board is not an artifact URL ending at its id")
        artboards, sources = raw.get("artboards"), raw.get("sources")
        if not isinstance(artboards, list) or not artboards:
            raise _SubsetError("artboards need at least one item")
        if not isinstance(sources, list):
            raise _SubsetError("sources must be a list")
        for items in (artboards, sources):
            if not all(isinstance(i, str) and i for i in items):
                raise _SubsetError("artboards and sources hold non-empty strings")
        harvest = raw.get("harvest")
        if not isinstance(harvest, str) or not _norm(harvest).strip("/"):
            raise _SubsetError("harvest missing")
        status = raw.get("status")
        if status not in _STATUSES:
            raise _SubsetError("status outside the enum")
        ratification = raw.get("ratification")
        if ratification is not None and not isinstance(ratification, (str, list)):
            raise _SubsetError("ratification malformed")
        if isinstance(ratification, list) or ratification == "":
            ratification = None
        if status == "deviated" and not ratification:
            raise _SubsetError("deviated entry without ratification")
        out.append(
            {
                "id": ident,
                "board": match.group(0),
                "artboards": list(artboards),
                "harvest": _norm(harvest).rstrip("/"),
                "sources": [_norm(s) for s in sources],
                "status": status,
                "ratification": ratification,
            }
        )
    return out


def _read_text(path: Path) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except (OSError, ValueError, TypeError):
        return None
    return text[1:] if text.startswith(_BOM) else text


def registry_path(repo_root) -> Path:
    """The registry file for this repo: the `coordinator.local.md` key, else DEFAULT_PATH.

    A key that escapes the repo root resolves outside it, which `load` refuses."""
    root = Path(repo_root)
    relative = DEFAULT_PATH
    text = _read_text(root / "coordinator.local.md")
    split = split_frontmatter(text) if text else None
    value = read_fm_field_unquoted(split.fm_text, LOCAL_KEY) if split else None
    if isinstance(value, str) and value.strip():
        relative = value.strip()
    return (root / _norm(relative)).resolve()


def load(repo_root) -> tuple:
    """(entries, None) when present and valid; (None, None) when absent; (None, code) otherwise."""
    root = Path(repo_root).resolve()
    path = registry_path(root)
    try:
        path.relative_to(root)
    except ValueError:
        return None, UNPARSEABLE
    if not path.is_file():
        return None, None
    try:
        text = path.read_text(encoding="utf-8")
        return _validate_entries(_parse_registry(text)), None
    except (OSError, UnicodeError, _SubsetError):
        return None, UNPARSEABLE


def _segment_regex(segment: str):
    parts = []
    for ch in segment:
        if ch == "*":
            if not parts or parts[-1] != "[^/]*":
                parts.append("[^/]*")
        elif ch == "?":
            parts.append("[^/]")
        else:
            parts.append(re.escape(ch))
    return re.compile("".join(parts))


@lru_cache(maxsize=512)
def _compiled(glob: str) -> tuple:
    return tuple(
        "**" if seg == "**" else _segment_regex(seg) for seg in _norm(glob).split("/") if seg
    )


def matches(glob: str, path: str) -> bool:
    """Whether `glob` covers `path`: POSIX segments, case-sensitive, `**` spans whole segments."""
    pattern = _compiled(glob)
    parts = tuple(seg for seg in _norm(path).split("/") if seg)

    @lru_cache(maxsize=None)
    def walk(pi: int, si: int) -> bool:
        if pi == len(pattern):
            return si == len(parts)
        seg = pattern[pi]
        if seg == "**":
            return any(walk(pi + 1, k) for k in range(si, len(parts) + 1))
        return si < len(parts) and seg.fullmatch(parts[si]) is not None and walk(pi + 1, si + 1)

    return walk(0, 0)


def _literal_head(glob: str) -> str:
    glob = _norm(glob)
    cut = min((glob.find(c) for c in "*?" if c in glob), default=len(glob))
    return glob[:cut]


def _intersects(glob: str, touched: str) -> bool:
    if touched.endswith("/"):
        head = _literal_head(glob)
        return head.startswith(touched) or touched.startswith(head)
    return matches(glob, touched)


def _harvest_record(root: Path, harvest: str) -> Optional[str]:
    candidate = (root / harvest / "harvest.json").resolve()
    try:
        rel = candidate.relative_to(root)
    except ValueError:
        return None
    return rel.as_posix() if candidate.is_file() else None


def lookup(repo_root, paths) -> tuple:
    """(hits, None); (None, code) when the registry is malformed; ([], None) when absent.

    One hit per entry whose sources intersect a scope path, in registry order. A hit carries
    `id`, `board_url`, `artboards`, `status`, `paths` (the scope paths matched) and, only when
    `<harvest>/harvest.json` is a file under the repo root, `harvest_path`."""
    entries, error = load(repo_root)
    if error:
        return None, error
    if not entries:
        return [], None
    root = Path(repo_root).resolve()
    scope = [p for p in paths if isinstance(p, str) and p.strip()]
    hits: list = []
    for entry in entries:
        matched = [t for t in scope if any(_intersects(g, t) for g in entry["sources"])]
        if not matched:
            continue
        hit = {
            "id": entry["id"],
            "board_url": entry["board"],
            "artboards": list(entry["artboards"]),
            "status": entry["status"],
            "paths": matched,
        }
        record = _harvest_record(root, entry["harvest"])
        if record is not None:
            hit["harvest_path"] = record
        hits.append(hit)
    return hits, None
