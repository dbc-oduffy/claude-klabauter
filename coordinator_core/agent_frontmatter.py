from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

FRONTMATTER_DELIM = "---"

TOOLS_LINE = re.compile(r"^tools:\s*(.+)$", re.MULTILINE)

SELECT_RE = re.compile(r"select:([A-Za-z0-9_,\-]+)")


def agent_files(dirs: Iterable[Path]) -> list[Path]:
    files: dict[Path, None] = {}
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            files.setdefault(f)
    return list(files)


def split_frontmatter(text: str) -> tuple[str, str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != FRONTMATTER_DELIM:
        return "", text
    for i in range(1, len(lines)):
        if lines[i].strip() == FRONTMATTER_DELIM:
            frontmatter = "".join(lines[: i + 1])
            body = "".join(lines[i + 1 :])
            return frontmatter, body
    return "", text


def parse_tools_line(raw: str) -> list[str]:
    value = raw.strip()
    if value.startswith("["):
        inner = value.strip("[]")
        entries = [e.strip().strip("'\"") for e in inner.split(",")]
    else:
        entries = [e.strip().strip("'\"") for e in value.split(",")]
    return [e for e in entries if e]


def declared_tools(frontmatter: str) -> list[str]:
    match = TOOLS_LINE.search(frontmatter)
    if match is None:
        return []
    return parse_tools_line(match.group(1))


def declared_mcp_tools(frontmatter: str) -> list[str]:
    return [t for t in declared_tools(frontmatter) if t.startswith("mcp__")]


def bootstrap_tool_names(body: str) -> set[str]:
    names: set[str] = set()
    for match in SELECT_RE.finditer(body):
        names.update(name for name in match.group(1).split(",") if name)
    return names
