"""plan.merge delta application: splice the EM's delta into source spine rows and render the delta tables.

Pure functions, no I/O. A row is edited by whole top-level key block only; an untouched row returns its
original text, and a `body` that gains a note keeps its source bytes as a prefix.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

import yaml

from coordinator_core.ops._plan_merge_contract import (
    EDIT_CLASSES,
    Refusal,
    RowAccount,
    SourceRow,
)

NOTE_MARKER = "CONSOLIDATION:"
DELTA_SOURCE = "(delta)"

_PLAIN = re.compile(r"^[A-Za-z0-9_./][A-Za-z0-9 _./-]*$")
_AMBIGUOUS = {"true", "false", "null", "yes", "no", "on", "off", "y", "n", "~"}
_NUMERIC = re.compile(r"^[-+]?(\d[\d_]*)?\.?\d*([eE][-+]?\d+)?$")
_BLOCK_HEADER = re.compile(r"^[|>][+-]?\d*\s*$")


def _reparses_as_str(value: str) -> bool:
    """True when a plain YAML scalar of `value` loads back as the same string."""
    try:
        return yaml.safe_load(value) == value
    except yaml.YAMLError:
        return False


def _scalar(value: Any) -> str:
    if isinstance(value, str):
        if (
            _PLAIN.match(value) and not value.endswith(" ") and value.lower() not in _AMBIGUOUS
            and not _NUMERIC.match(value) and _reparses_as_str(value)
        ):
            return value
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(value)


def _block_scalar(text: str, indent: int) -> Tuple[str, List[str]]:
    pad = " " * indent
    chomp = "" if text.endswith("\n") and not text.endswith("\n\n") else ("+" if text.endswith("\n\n") else "-")
    first = next((ln for ln in text.split("\n") if ln.strip()), "")
    hint = "2" if first.startswith(" ") else ""
    body = text[:-1] if text.endswith("\n") else text
    lines = [(pad + ln) if ln else "" for ln in body.split("\n")]
    return "|" + hint + chomp, lines


def _is_inline(value: Any) -> bool:
    if isinstance(value, str):
        return "\n" not in value
    return not isinstance(value, (dict, list)) or not value


def _inline(value: Any) -> str:
    if isinstance(value, dict):
        return "{}"
    if isinstance(value, list):
        return "[]"
    return _scalar(value)


def _emit(key: str, value: Any, ind: int) -> List[str]:
    pad = " " * ind
    if _is_inline(value):
        return [f"{pad}{key}: {_inline(value)}"]
    if isinstance(value, str):
        header, body = _block_scalar(value, ind + 2)
        return [f"{pad}{key}: {header}"] + body
    return [f"{pad}{key}:"] + _emit_node(value, ind + 2)


def _emit_node(value: Any, ind: int) -> List[str]:
    pad = " " * ind
    if isinstance(value, dict):
        out: List[str] = []
        for k, v in value.items():
            out.extend(_emit(str(k), v, ind))
        return out
    out = []
    for item in value:
        if _is_inline(item):
            out.append(f"{pad}- {_inline(item)}")
        elif isinstance(item, str):
            header, body = _block_scalar(item, ind + 2)
            out.append(f"{pad}- {header}")
            out.extend(body)
        else:
            sub = _emit_node(item, ind + 2)
            sub[0] = f"{pad}- {sub[0][ind + 2:]}"
            out.extend(sub)
    return out


class _Row:
    """A row split into top-level key blocks, each with its trailing blank-line gap."""

    def __init__(self, text: str) -> None:
        lines = text.split("\n")
        head = re.match(r"^( *)- ([\w.-]+):(?:\s|$)", lines[0]) if lines else None
        if head is None:
            raise ValueError("row text does not start with a '- key:' list item")
        self.key_indent = len(head.group(1)) + 2
        key_line = re.compile(r"^ {%d}(?! )([\w.-]+):(?:\s|$)" % self.key_indent)
        starts: List[Tuple[str, int]] = [(head.group(2), 0)]
        for i in range(1, len(lines)):
            m = key_line.match(lines[i])
            if m:
                starts.append((m.group(1), i))
        self.blocks: List[List[Any]] = []
        for n, (key, s) in enumerate(starts):
            e = starts[n + 1][1] if n + 1 < len(starts) else len(lines)
            body, gap = lines[s:e], []
            while len(body) > 1 and body[-1].strip() == "":
                gap.insert(0, body.pop())
            self.blocks.append([key, body, gap])

    def text(self) -> str:
        return "\n".join(ln for _, body, gap in self.blocks for ln in body + gap)

    def index(self, key: str) -> int:
        return next((i for i, b in enumerate(self.blocks) if b[0] == key), -1)

    def set_block(self, key: str, value: Any) -> None:
        lines = _emit(key, value, self.key_indent)
        i = self.index(key)
        if i >= 0:
            self.blocks[i][1] = lines
        else:
            self.blocks.append([key, lines, []])
            last = self.blocks[-2]
            self.blocks[-1][2], last[2] = last[2], []

    def drop_block(self, key: str) -> None:
        i = self.index(key)
        if i <= 0:
            return
        if i == len(self.blocks) - 1:
            self.blocks[i - 1][2] = self.blocks[i][2]
        del self.blocks[i]

    def add_note(self, note: str) -> None:
        i = self.index("body")
        note_lines = (NOTE_MARKER + " " + note.strip("\n")).split("\n")
        if i >= 0 and _BLOCK_HEADER.match(self.blocks[i][1][0].split(":", 1)[1].strip()):
            body = self.blocks[i][1]
            content = next((ln for ln in body[1:] if ln.strip()), "")
            pad = " " * (len(content) - len(content.lstrip()) if content else self.key_indent + 2)
            body.append("")
            body.extend((pad + ln) if ln else "" for ln in note_lines)
            return
        header, lines = _block_scalar("\n".join(note_lines), self.key_indent + 2)
        self.blocks.append(["consolidation", [f"{' ' * self.key_indent}consolidation: {header}"] + lines, []])
        self.blocks[-1][2], self.blocks[-2][2] = self.blocks[-2][2], []


def _new_row_text(entry: Mapping[str, Any]) -> str:
    lines = [f"- id: {_scalar(entry['id'])}"]
    for key, value in entry["fields"].items():
        lines.extend(_emit(str(key), value, 2))
    lines.extend(_emit("body", str(entry["body"]), 2))
    return "\n".join(lines) + "\n"


def _apply_entry(row: _Row, entry: Mapping[str, Any]) -> None:
    for key, value in (entry.get("fields") or {}).items():
        row.set_block(str(key), value)
    for key in entry.get("drop_fields") or ():
        row.drop_block(str(key))
    if entry.get("deferred") is True:
        row.set_block("disposition", "deferred")
    if entry.get("note"):
        row.add_note(str(entry["note"]))


def _classes(entry: Mapping[str, Any]) -> set:
    out = set()
    if entry.get("fields") or entry.get("drop_fields"):
        out.add("fields")
    if entry.get("note"):
        out.add("note")
    if entry.get("deferred") is True:
        out.add("drop")
    return out


def apply_delta(
    rows: Sequence[SourceRow], delta: Mapping[str, Any]
) -> Union[Tuple[List[str], List[RowAccount]], Refusal]:
    """Apply `delta["rows"]` to `rows` (already re-keyed, in output order).

    Returns the output row texts and one RowAccount per output row, or a Refusal when an entry names no
    row (DELTA_UNMATCHED_ROW) or a new row's `after` anchor names no row (DELTA_UNMATCHED_ANCHOR).
    A row whose text is unchanged by its entries is reported untouched.
    """
    ids = [r.row_id for r in rows]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        return Refusal("ID_COLLISION", f"duplicate row ids reach delta application: {dupes}", row_ids=tuple(dupes))
    entries = list(delta.get("rows") or ())
    existing = set(ids)
    edits = [e for e in entries if not e.get("new")]
    adds = [e for e in entries if e.get("new")]

    unmatched = [e["id"] for e in edits if e["id"] not in existing]
    if unmatched:
        return Refusal(
            "DELTA_UNMATCHED_ROW",
            f"delta rows entries match no row id: {unmatched}",
            row_ids=tuple(unmatched),
        )
    clash = [e["id"] for e in adds if e["id"] in existing]
    if clash:
        return Refusal("ID_COLLISION", f"new rows reuse existing row ids: {clash}", row_ids=tuple(clash))

    by_id = {}
    for r in rows:
        by_id[r.row_id] = [r.source, r.text, set(), r.text]
    for e in edits:
        slot = by_id[e["id"]]
        try:
            row = _Row(slot[1])
        except ValueError as exc:
            return Refusal("SPINE_MALFORMED", f"row {e['id']}: {exc}", sources=(slot[0],), row_ids=(e["id"],))
        _apply_entry(row, e)
        slot[1] = row.text()
        slot[2] |= _classes(e)
    order = [r.row_id for r in rows]
    new_ids = set()
    for e in adds:
        anchor = e["after"]
        if anchor not in by_id:
            return Refusal(
                "DELTA_UNMATCHED_ANCHOR",
                f"new row {e['id']} follows {anchor!r}, which matches no row id",
                row_ids=(e["id"], anchor),
            )
        by_id[e["id"]] = [DELTA_SOURCE, _new_row_text(e), set(), ""]
        new_ids.add(e["id"])
        order.insert(order.index(anchor) + 1, e["id"])

    texts: List[str] = []
    accounts: List[RowAccount] = []
    for rid in order:
        source, text, classes, original = by_id[rid]
        texts.append(text)
        if rid in new_ids:
            accounts.append(RowAccount(rid, source, "new", ("fields",)))
        elif text == original:
            accounts.append(RowAccount(rid, source, "untouched"))
        else:
            ordered = tuple(c for c in EDIT_CLASSES if c in classes)
            accounts.append(RowAccount(rid, source, "amended", ordered))
    return texts, accounts


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _dropped_fields(item: Any) -> Tuple[str, str, str]:
    if isinstance(item, Mapping):
        return item.get("heading", ""), item.get("source", ""), item.get("reason", "")
    heading, source, reason = item
    return heading, source, reason


def render_delta_tables(
    accounts: Iterable[RowAccount],
    dropped: Iterable[Any],
    reasons: Optional[Mapping[str, str]] = None,
) -> str:
    """Render exactly two Markdown tables: `Row | Source | Edit classes | Reason` for every edited or
    new row, then `Section | Source | Reason` for every dropped section.

    `dropped` items are mappings with `heading`, `source`, `reason` (or such triples). `reasons` maps a row
    id to the delta entry's `reason`; a row with none renders an empty cell.
    """
    reasons = reasons or {}
    lines = ["| Row | Source | Edit classes | Reason |", "| --- | --- | --- | --- |"]
    for a in accounts:
        if a.status == "untouched":
            continue
        classes = ", ".join(a.edit_classes) if a.edit_classes else a.status
        lines.append(f"| {_cell(a.row_id)} | {_cell(a.source)} | {_cell(classes)} | {_cell(reasons.get(a.row_id, ''))} |")
    lines += ["", "| Section | Source | Reason |", "| --- | --- | --- |"]
    for item in dropped:
        heading, source, reason = _dropped_fields(item)
        lines.append(f"| {_cell(heading)} | {_cell(source)} | {_cell(reason)} |")
    return "\n".join(lines) + "\n"
