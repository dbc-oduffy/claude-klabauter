"""Closed row recognisers for fleet.migrate_prose_queue: parse_file(text) -> FileParse.

Three fixed lead-token grammars, no heading or paragraph splitting:
  GRAMMAR_PIPE  `- YYYY-MM-DD | from_repo | SURFACE | BODY | proposed target: TARGET`
                cells keyed "0".."4"; cells past the fifth fold into "4".
  GRAMMAR_TABLE a markdown table whose header names ID plus Description or Summary;
                cells keyed by canonical column name (see CANONICAL_COLUMNS), unknown
                headers keep their own text.
  GRAMMAR_DSR   `- DSR-...| ...`, `- **DSR-...** text`, `| DSR-... | ... |` (headerless).
                cells always carry "ID" and "date" (embedded in the ID). A pipe row whose
                cell count equals the file's `Schema:` line maps cell i to the declared
                name (first cell "ID"); every other DSR row carries "body" = the text
                after the ID.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from coordinator_core.ops.fleet._prose_queue_contract import (
    OUTCOME_FILE_UNPARSEABLE,
    OUTCOME_PARSED,
    FileParse,
    RawRow,
)

GRAMMAR_PIPE = "dated-pipe"
GRAMMAR_TABLE = "id-table"
GRAMMAR_DSR = "dsr-led"

# Lower-cased header -> canonical key. Description and Summary share one key, as do Found and Added.
CANONICAL_COLUMNS: Dict[str, str] = {
    "id": "ID",
    "system": "System",
    "severity": "Severity",
    "description": "Description",
    "summary": "Description",
    "source": "Source",
    "status": "Status",
    "found": "Found",
    "added": "Found",
    "why blocked": "Why Blocked",
}

_LINE_SPLIT_RE = re.compile(r"\r\n|\r|\n")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_PIPE_LEAD_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2})\s*\|")
_DSR_ID = r"DSR-(\d{4}-\d{2}-\d{2})-\d+"
_DSR_DASH_PIPE_RE = re.compile(r"^- (" + _DSR_ID + r")\s*\|")
_DSR_BOLD_RE = re.compile(r"^- \*\*(" + _DSR_ID + r")\*\*\s*(.*)$")
_DSR_TABLE_ROW_RE = re.compile(r"^\|\s*(" + _DSR_ID + r")\s*\|")
_SEPARATOR_RE = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_SCHEMA_RE = re.compile(r"^\s*(?:>\s*)?Schema:\s*`([^`]+)`")
_UNESCAPED_PIPE_RE = re.compile(r"(?<!\\)\|")


def _split_cells(line: str) -> List[str]:
    """Split a table row on unescaped pipes, dropping the outer empty edges."""
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [c.replace("\\|", "|").strip() for c in _UNESCAPED_PIPE_RE.split(body)]


def _schema_names(lines: List[str]) -> Optional[List[str]]:
    """Declared names of the first DSR-led `Schema:` line, or None when the file declares none."""
    for line in lines:
        m = _SCHEMA_RE.match(line)
        if m:
            names = [n.strip() for n in m.group(1).split("|")]
            if names and names[0].startswith("DSR-"):
                return names
    return None


def _canonical_header(header: str) -> str:
    return CANONICAL_COLUMNS.get(header.strip().lower(), header.strip())


def _is_entry_header(cells: List[str]) -> bool:
    keys = {CANONICAL_COLUMNS.get(c.strip().lower()) for c in cells}
    return "ID" in keys and "Description" in keys


def _dsr_cells(
    id_text: str, date: str, rest_cells: List[str], rest_text: str, schema: Optional[List[str]], pipe: bool
) -> Dict[str, str]:
    cells: Dict[str, str] = {"ID": id_text, "date": date}
    if pipe and schema is not None and len(rest_cells) + 1 == len(schema):
        for name, value in zip(schema[1:], rest_cells):
            cells[name] = value
    else:
        cells["body"] = rest_text
    return cells


def _table_rows(
    lines: List[str], start: int, header_cells: List[str]
) -> "tuple[List[RawRow], int]":
    """Rows of an entry table whose header is at index start; returns (rows, next index)."""
    keys: List[str] = []
    for h in header_cells:
        key = _canonical_header(h)
        if key in keys:
            key = h.strip()
        keys.append(key)
    rows: List[RawRow] = []
    i = start + 2
    while i < len(lines) and lines[i].lstrip().startswith("|"):
        cells = _split_cells(lines[i])
        if len(cells) > len(keys):
            cells = cells[: len(keys) - 1] + [" | ".join(cells[len(keys) - 1 :])]
        mapped = {k: v for k, v in zip(keys, cells)}
        if any(mapped.values()):
            rows.append(RawRow(GRAMMAR_TABLE, i + 1, i + 1, lines[i], mapped))
        i += 1
    return rows, i


def parse_file(text: str) -> FileParse:
    """Recognise rows in text; outcome file-unparseable (rows empty) when no grammar matches."""
    lines = _LINE_SPLIT_RE.split(text)
    schema = _schema_names(lines)
    schema_line = " | ".join(schema) if schema else None
    rows: List[RawRow] = []
    in_fence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            i += 1
            continue
        if in_fence:
            i += 1
            continue
        stripped = line.strip()
        lineno = i + 1

        if stripped.startswith("|"):
            if i + 1 < len(lines) and _SEPARATOR_RE.match(lines[i + 1].strip()):
                header = _split_cells(stripped)
                if _is_entry_header(header):
                    table_rows, i = _table_rows(lines, i, header)
                    rows.extend(table_rows)
                else:
                    i += 2
                    while i < len(lines) and lines[i].lstrip().startswith("|"):
                        i += 1
                continue
            m = _DSR_TABLE_ROW_RE.match(stripped)
            if m:
                cells = _split_cells(stripped)
                rest = cells[1:]
                rows.append(
                    RawRow(
                        GRAMMAR_DSR,
                        lineno,
                        lineno,
                        line,
                        _dsr_cells(m.group(1), m.group(2), rest, " | ".join(rest), schema, True),
                    )
                )
            i += 1
            continue

        m = _DSR_DASH_PIPE_RE.match(stripped)
        if m:
            cells = [c.strip() for c in stripped[2:].split("|")]
            rest = cells[1:]
            rows.append(
                RawRow(
                    GRAMMAR_DSR,
                    lineno,
                    lineno,
                    line,
                    _dsr_cells(m.group(1), m.group(2), rest, " | ".join(rest), schema, True),
                )
            )
            i += 1
            continue

        m = _DSR_BOLD_RE.match(stripped)
        if m:
            rows.append(
                RawRow(
                    GRAMMAR_DSR,
                    lineno,
                    lineno,
                    line,
                    _dsr_cells(m.group(1), m.group(2), [], m.group(3).strip(), schema, False),
                )
            )
            i += 1
            continue

        m = _PIPE_LEAD_RE.match(stripped)
        if m:
            cells = [c.strip() for c in stripped[2:].split("|")]
            if len(cells) > 5:
                cells = cells[:4] + ["|".join(cells[4:]).strip()]
            rows.append(
                RawRow(GRAMMAR_PIPE, lineno, lineno, line, {str(n): c for n, c in enumerate(cells)})
            )
        i += 1

    if not rows:
        return FileParse(OUTCOME_FILE_UNPARSEABLE, [], schema_line)
    return FileParse(OUTCOME_PARSED, rows, schema_line)
