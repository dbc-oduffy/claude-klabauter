"""Renders the generated atlas tables (connectivity matrix, file-index block, fingerprints) from structure.json.

Pure formatting: no spawn, no rag call, no derivation. Output is a function of the
structure and `today` alone; keys are sorted and newlines are LF.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from coordinator_core.architecture_survey.contract import Decision

ATLAS_DIR = Path("docs") / "architecture"
MATRIX_NAME = "connectivity-matrix.md"
FILE_INDEX_NAME = "file-index.md"
FINGERPRINT_NAME = "structure-fingerprint.json"
BEGIN_MARKER = "<!-- generated:structure begin -->"
END_MARKER = "<!-- generated:structure end -->"
_BLOCK_RE = re.compile(re.escape(BEGIN_MARKER) + r".*?" + re.escape(END_MARKER), re.DOTALL)

RULE = "render the generated tables from structure.json; a need that is not covered leaves its artifact untouched"


def _data(payload: Any) -> Any:
    """rag's envelope wraps the door answer in `data`; accept either the envelope or the bare answer."""
    if isinstance(payload, dict) and isinstance(payload.get("data"), (dict, list)):
        return payload["data"]
    return payload


def _index_line(index: dict[str, Any]) -> str:
    if index.get("state") == "current" or index.get("commits_behind", 0) == 0:
        return f"Structure as of index commit {index['commit']}, current"
    return f"Structure as of index commit {index['commit']}, {index['commits_behind']} commits behind HEAD"


def _scalar(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return str(value)


def _limit_lines(limits: Any) -> list[str]:
    if isinstance(limits, dict):
        return [f"- `{key}`: {_scalar(limits[key])}" for key in sorted(limits)]
    if isinstance(limits, list):
        return [f"- {_scalar(item)}" for item in limits]
    if limits:
        return [f"- {limits}"]
    return []


def _matrix_text(structure: dict[str, Any], today: str) -> str:
    data = _data(structure["needs"]["edges"]["payload"]) or {}
    edges = data.get("edges", []) if isinstance(data, dict) else []
    weights: dict[tuple[str, str], int] = {}
    names = {s["name"] for s in structure["systems"]}
    for edge in edges:
        src, dst = edge["from_system"], edge["to_system"]
        names.update((src, dst))
        weights[(src, dst)] = weights.get((src, dst), 0) + edge["weight"]
    order = sorted(names)
    lines = [
        "---",
        f"last_mapped: {today}",
        "---",
        "",
        "# Connectivity Matrix",
        "",
        _index_line(structure["index"]) + ".",
        "",
    ]
    limits = data.get("limits") if isinstance(data, dict) else None
    limit_lines = _limit_lines(limits)
    if limit_lines:
        lines += ["Floor and counting caveats, as rag returned them:", "", *limit_lines, ""]
    lines += [
        "Cell = static import edge weight from ROW to COLUMN; `.` on the diagonal.",
        "",
        "| ROW \\ COL | " + " | ".join(order) + " |",
        "|---|" + "|".join("---" for _ in order) + "|",
    ]
    for row in order:
        cells = []
        for col in order:
            if row == col:
                cells.append(".")
            else:
                cells.append(str(weights.get((row, col), 0)))
        lines.append(f"| {row} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _file_rows(payload: Any) -> list[tuple[str, str]]:
    rows = {(r["path"], r["system"]) for r in _data(payload) or []}
    return sorted(rows)


def _block_text(structure: dict[str, Any]) -> str:
    lines = [BEGIN_MARKER, "", _index_line(structure["index"]) + ".", ""]
    lines += ["| System | Files | Lines |", "|---|---|---|"]
    for s in sorted(structure["systems"], key=lambda s: s["name"]):
        lines.append(f"| {s['name']} | {s['file_count']} | {s['line_count']} |")
    need = structure["needs"]["file-index"]
    if need["covered"]:
        lines += ["", "```"]
        lines += [f"{path} -> {system}" for path, system in _file_rows(need["payload"])]
        lines += ["```"]
    lines += ["", END_MARKER]
    return "\n".join(lines)


def _file_index_text(existing: str, block: str) -> tuple[str, bool]:
    if _BLOCK_RE.search(existing):
        return _BLOCK_RE.sub(lambda _m: block, existing, count=1), False
    base = existing.rstrip("\n")
    return (base + "\n\n" if base else "") + block + "\n", True


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def render_structure(repo_root: Path | str, structure: dict[str, Any], *, today: date | str) -> Decision:
    """Write the generated atlas artifacts from `structure` and return the `completeness` evidence Decision."""
    atlas = Path(repo_root) / ATLAS_DIR
    today_s = today.isoformat() if isinstance(today, date) else str(today)
    needs = structure["needs"]
    written: list[str] = []
    notes: list[str] = []

    if needs["edges"]["covered"]:
        _write(atlas / MATRIX_NAME, _matrix_text(structure, today_s))
        written.append(MATRIX_NAME)
    else:
        notes.append(f"edges uncovered: {MATRIX_NAME} untouched ({needs['edges']['reason']})")

    if needs["system-map"]["covered"]:
        fi_path = atlas / FILE_INDEX_NAME
        existing = fi_path.read_text(encoding="utf-8") if fi_path.exists() else ""
        text, markers_added = _file_index_text(existing, _block_text(structure))
        _write(fi_path, text)
        written.append(FILE_INDEX_NAME)
        if markers_added:
            notes.append("markers-added")
        if not needs["file-index"]["covered"]:
            notes.append(f"file-index uncovered: counts only, no per-file rows ({needs['file-index']['reason']})")
    else:
        notes.append(f"system-map uncovered: {FILE_INDEX_NAME} untouched ({needs['system-map']['reason']})")

    fingerprints = {
        "index_commit": structure["index"]["commit"],
        "systems": {s["name"]: s["fingerprint"] for s in sorted(structure["systems"], key=lambda s: s["name"])},
    }
    _write(atlas / FINGERPRINT_NAME, json.dumps(fingerprints, sort_keys=True, indent=2) + "\n")
    written.append(FINGERPRINT_NAME)

    evidence = "rendered: " + ", ".join(written)
    if notes:
        evidence += "; " + "; ".join(notes)
    return Decision(id="completeness", chosen="rendered", rule=RULE, evidence=evidence)
