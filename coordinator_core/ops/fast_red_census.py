"""Classify pytest short-summary FAILED/ERROR lines by failure shape and first-pass cluster them.

Node ids come from test_red_record.parse_failing_nodeids so census rows and the
standing-red registry key identically; a second id parser would mint phantom new red.
"""
from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from coordinator_core.ops.test_red_record import parse_failing_nodeids

_TAIL_RE = re.compile(r"^([A-Za-z_][\w.]*)(?::\s*(.*))?$")
_IMPORT_EXC = frozenset({"ImportError", "ModuleNotFoundError", "SyntaxError", "IndentationError"})
_OSERROR_EXC = frozenset({
    "OSError", "IOError", "EnvironmentError", "FileNotFoundError", "FileExistsError",
    "PermissionError", "NotADirectoryError", "IsADirectoryError", "TimeoutError",
    "ConnectionError", "ConnectionRefusedError", "ConnectionResetError",
    "BrokenPipeError", "BlockingIOError", "InterruptedError", "ChildProcessError",
    "ProcessLookupError", "WinError",
})
_HEX_RE = re.compile(r"\b(?:0x[0-9a-fA-F]+|[0-9a-fA-F]{7,})\b")
_PATH_RE = re.compile(r"(?:[A-Za-z]:)?[\\/]?(?:[\w.~-]+[\\/])+[\w.~-]*|[\w.~-]+\.(?:py|yaml|yml|json|md|txt|log|tmp)\b")
_DIGITS_RE = re.compile(r"\d+")


@dataclass(frozen=True)
class SummaryRow:
    nodeid: str
    outcome: str
    exc_type: str
    message: str
    shape: str


def _shape(nodeid: str, outcome: str, exc_type: str, message: str) -> str:
    if outcome == "ERROR" and "::" not in nodeid:
        return "collection-import"
    if exc_type in _IMPORT_EXC:
        return "collection-import"
    if outcome == "ERROR":
        return "fixture-env"
    if exc_type == "Failed" and "timeout" in message.lower():
        return "fixture-env"
    if exc_type in _OSERROR_EXC:
        return "fixture-env"
    if exc_type == "AssertionError":
        return "assertion"
    return "unclassified"


def classify_summary(output: str) -> list[SummaryRow]:
    rows: list[SummaryRow] = []
    seen: set[str] = set()
    for line in output.splitlines():
        line = line.strip()
        outcome = "FAILED" if line.startswith("FAILED") else "ERROR" if line.startswith("ERROR") else ""
        if not outcome:
            continue
        _, ids = parse_failing_nodeids(line)
        if not ids:
            continue
        nodeid = ids[0]
        if nodeid in seen:
            continue
        seen.add(nodeid)
        tail = ""
        idx = line.find(nodeid)
        rest = line[idx + len(nodeid):] if idx >= 0 else ""
        if " - " in rest:
            tail = rest.split(" - ", 1)[1].strip()
        exc_type, message = "", ""
        m = _TAIL_RE.match(tail) if tail else None
        if m:
            exc_type, message = m.group(1), (m.group(2) or "").strip()
        elif tail:
            message = tail
        rows.append(SummaryRow(nodeid, outcome, exc_type, message,
                               _shape(nodeid, outcome, exc_type, message)))
    return rows


def _normalise(message: str) -> str:
    message = _PATH_RE.sub("<path>", message)
    message = _HEX_RE.sub("<hex>", message)
    return _DIGITS_RE.sub("<n>", message)


def _slug(shape: str, exc_type: str, norm: str) -> str:
    digest = hashlib.sha1(f"{shape}|{exc_type}|{norm}".encode()).hexdigest()[:8]
    return f"{shape}-{(exc_type or 'none').lower()}-{digest}"


def cluster_rows(rows: Iterable[SummaryRow]) -> dict[str, list[SummaryRow]]:
    clusters: dict[str, list[SummaryRow]] = {}
    for row in rows:
        slug = _slug(row.shape, row.exc_type, _normalise(row.message))
        clusters.setdefault(slug, []).append(row)
    return dict(sorted(clusters.items()))


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_table(clusters: dict[str, list[SummaryRow]]) -> str:
    lines = ["| cluster | shape | files | node ids | first message |", "|---|---|---|---|---|"]
    for slug, rows in clusters.items():
        files = sorted({r.nodeid.split("::", 1)[0] for r in rows})
        ids = "<br>".join(r.nodeid for r in rows)
        lines.append(f"| {slug} | {rows[0].shape} | {_cell(', '.join(files))} | "
                     f"{_cell(ids)} | {_cell(rows[0].message)} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] != "cluster":
        print("usage: fast_red_census cluster <capture-file>", file=sys.stderr)
        return 2
    text = Path(argv[1]).read_text(encoding="utf-8", errors="replace")
    print(render_table(cluster_rows(classify_summary(text))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
