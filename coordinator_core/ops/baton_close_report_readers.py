"""Baton header reader for `baton.close_report`: one pass over state/handoffs/.

Reads each baton's frontmatter bytes only (up to the closing fence), extracts the join keys by
line scan, and builds the successor index in memory. No YAML parse, no subprocess, no write.
"""

from __future__ import annotations

import os
import re

from coordinator_core.ops.baton_close_report_contract import BatonHeader
from coordinator_core.session import record_homes

LIVE_STATUSES = frozenset({"open", "claimed"})

_CHUNK = 16 * 1024
# A frontmatter block longer than this is treated as unreadable, not read to the end.
_MAX_HEADER_BYTES = 256 * 1024

_FENCE = re.compile(r"\n---[ \t]*(\n|$)")
_KEY_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(.*)$")
_GOAL_ID = re.compile(r"goal-[A-Za-z0-9_-]+")
_SCALAR_KEYS = frozenset(
    {
        "status",
        "predecessor",
        "governing_plan",
        "origin_plan_id",
        "sizing_object",
        "roadmap_id",
        "deliverable_id",
        "kind",
    }
)
_NULLS = frozenset({"", "none", "null", "~"})


def _scalar(raw: str) -> str | None:
    """Strip a trailing ` #comment` and surrounding quotes; None for empty/null forms."""
    value = raw.strip()
    if value[:1] in ('"', "'"):
        quote = value[0]
        end = value.find(quote, 1)
        value = value[1:end] if end > 0 else value[1:]
    else:
        cut = value.find(" #")
        if cut >= 0:
            value = value[:cut]
        if value.startswith("#"):
            value = ""
    value = value.strip()
    return None if value.lower() in _NULLS else value


def _read_frontmatter(path: str) -> list[str] | None:
    """Return the frontmatter lines between the fences, or None when unreadable."""
    try:
        with open(path, "rb") as fh:
            data = b""
            while True:
                chunk = fh.read(_CHUNK)
                data += chunk
                eof = not chunk
                text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
                if not text.startswith("---\n"):
                    return None
                for m in _FENCE.finditer(text, 3):
                    # A bare "---" at the buffer end may be a cut "----"; only EOF confirms it.
                    if m.group(1) == "\n" or eof:
                        return text[4 : m.start()].split("\n") if m.start() >= 4 else []
                if eof or len(data) >= _MAX_HEADER_BYTES:
                    return None
    except OSError:
        return None


def _parse(rel: str, lines: list[str]) -> BatonHeader | None:
    scalars: dict[str, str | None] = {}
    goals: list[str] = []
    in_goal_block = False
    for line in lines:
        if in_goal_block:
            stripped = line.strip()
            if stripped.startswith("-"):
                goals.extend(_GOAL_ID.findall(stripped))
                continue
            if stripped == "" or stripped.startswith("#") or line[:1] in (" ", "\t"):
                continue
            in_goal_block = False
        m = _KEY_LINE.match(line)
        if m is None:
            continue
        key, rest = m.group(1), m.group(2)
        if key in _SCALAR_KEYS:
            scalars.setdefault(key, _scalar(rest))
        elif key == "origin_goal_id":
            body = rest.split(" #", 1)[0]
            found = _GOAL_ID.findall(body)
            goals.extend(found)
            in_goal_block = not found and body.strip() == ""
    status = scalars.get("status")
    if status is None:
        return None
    return BatonHeader(
        path=rel,
        status=status,
        predecessor=scalars.get("predecessor"),
        governing_plan=scalars.get("governing_plan"),
        origin_plan_id=scalars.get("origin_plan_id"),
        sizing_object=scalars.get("sizing_object"),
        roadmap_id=scalars.get("roadmap_id"),
        deliverable_id=scalars.get("deliverable_id"),
        kind=scalars.get("kind"),
        origin_goal_id=list(dict.fromkeys(goals)),
    )


def read_baton_headers(worktree_root: str) -> tuple[dict[str, BatonHeader], list[str]]:
    """Return ({rel: header} for open|claimed batons, [rel of unreadable frontmatter]).

    `rel` is the repo-relative posix path. A missing handoffs directory yields two empties.
    Terminal-status batons are neither returned nor listed as unreadable.
    """
    home = record_homes.home_dir(worktree_root, "handoffs")
    rel_home = os.path.relpath(home, worktree_root).replace(os.sep, "/")
    headers: dict[str, BatonHeader] = {}
    unreadable: list[str] = []
    try:
        entries = sorted(os.scandir(home), key=lambda e: e.name)
    except OSError:
        return headers, unreadable
    for entry in entries:
        if not entry.name.endswith(".md"):
            continue
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        rel = f"{rel_home}/{entry.name}"
        lines = _read_frontmatter(entry.path)
        header = _parse(rel, lines) if lines is not None else None
        if header is None:
            unreadable.append(rel)
        elif header["status"] in LIVE_STATUSES:
            headers[rel] = header
    return headers, unreadable


def successor_index(headers: dict[str, BatonHeader]) -> set[str]:
    """Rels named as `predecessor` by some live baton in `headers`."""
    named: set[str] = set()
    for header in headers.values():
        pred = header["predecessor"]
        if pred:
            named.add(pred.replace("\\", "/"))
    return named
