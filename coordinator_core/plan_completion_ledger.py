"""Plan-completion ledger: one JSONL row per plan flip to a completed status.

Purpose: the committed record `ceremony.wiki_candidates` reads instead of walking git history.
Row: {schema: "plan-completion", v: 1, plan, status, completed_at (UTC ISO), completing_sha,
plan_blob, tshirt, writes[]}. `plan_blob` is the git blob id of the exact plan bytes committed,
computed in-process, so a preview is one `cat-file --batch` over blob ids with no history.
Paths are repo-relative with forward slashes; lines end in LF. Nothing here spawns a process.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

LEDGER = "state/plan-completions.jsonl"
SCHEMA = "plan-completion"
SCHEMA_VERSION = 1
DEFAULT_WINDOW_DAYS = 7
ROW_KEYS = ("schema", "v", "plan", "status", "completed_at", "completing_sha", "plan_blob", "tshirt", "writes")

# Plan `status` values meaning the plan's work is on the branch; each must be a member of
# plan.schema.json's status enum (pinned by test).
COMPLETED_STATUSES = frozenset({"landed", "implemented", "closed_partial", "closed"})
TSHIRT_RANK = {"XL": 5, "L": 4, "M": 3, "S": 2, "XS": 1}

_WRITES_RE = re.compile(r"^\s*(?:-\s+)?writes:[ \t]*(.*)$")
_ITEM_RE = re.compile(r"^\s*-\s+(\S.*?)\s*$")
_FM_SIZING_RE = re.compile(r"^sizing_object:[ \t]*(.*?)[ \t]*(?:#.*)?$", re.M)
_TSHIRT_LINE_RE = re.compile(r"^\s+tshirt:[ \t]*['\"]?(\w+)")
_FM_CLOSE_RE = re.compile(r"^---[ \t]*$", re.M)


def blob_id(data: bytes) -> str:
    """The git blob sha1 of `data` (`blob <len>\\0<bytes>`), equal to `git hash-object`."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def make_row(
    plan: str,
    status: str,
    completed_at: str,
    completing_sha: str,
    plan_bytes: bytes,
    tshirt: Optional[str],
    writes: List[str],
) -> Dict[str, Any]:
    """A schema-complete row; `plan_blob` is derived from `plan_bytes`, the committed plan content."""
    return {
        "schema": SCHEMA,
        "v": SCHEMA_VERSION,
        "plan": plan.replace("\\", "/"),
        "status": status,
        "completed_at": completed_at,
        "completing_sha": completing_sha,
        "plan_blob": blob_id(plan_bytes),
        "tshirt": tshirt,
        "writes": list(writes),
    }


def append_row(root: Path, row: Dict[str, Any]) -> Path:
    """Append `row` as one LF-terminated JSON line under `root`/LEDGER; returns the ledger path.

    One O_APPEND `write` of the whole line, so concurrent appenders interleave by line, never mid-line.
    Raises ValueError when `row` lacks a schema key or carries the wrong schema/version.
    """
    missing = [k for k in ROW_KEYS if k not in row]
    if missing:
        raise ValueError(f"plan-completion row missing keys: {missing}")
    if row["schema"] != SCHEMA or row["v"] != SCHEMA_VERSION:
        raise ValueError(f"plan-completion row has schema {row['schema']!r} v{row['v']!r}")
    path = Path(root) / LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(row, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")
    from coordinator_core.atomic_append import append_line
    from coordinator_core.session.declared_writes import declare_write

    append_line(path, line)
    declare_write(path)
    return path


def _parse_instant(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def read_window(
    root: Path,
    since_iso: Optional[str] = None,
    days: int = DEFAULT_WINDOW_DAYS,
    *,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Ledger rows with `completed_at` at or after the window start, in file order.

    Start is `since_iso` (an ISO date or datetime; naive means UTC), else `now - days`. Parse only:
    a missing ledger is an empty list; a blank, malformed or wrong-schema line is skipped.
    Raises ValueError when `since_iso` is given but is not an ISO instant.
    """
    if since_iso:
        start = _parse_instant(since_iso)
        if start is None:
            raise ValueError(f"since is not an ISO date or datetime: {since_iso!r}")
    else:
        start = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    try:
        text = (Path(root) / LEDGER).read_text(encoding="utf-8")
    except OSError:
        return []
    rows: List[Dict[str, Any]] = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("schema") != SCHEMA:
            continue
        at = _parse_instant(str(row.get("completed_at", "")))
        if at is not None and at >= start:
            rows.append(row)
    return rows


def _unquote(value: str) -> str:
    return value.strip().strip("\"'").strip()


def frontmatter(text: str) -> str:
    """The YAML between the leading `---` fences, "" when the text has none."""
    if not text.startswith("---"):
        return ""
    close = _FM_CLOSE_RE.search(text[3:])
    return text[3 : 3 + close.start()] if close else ""


def body(text: str) -> str:
    """The plan past its frontmatter: a preview spent on YAML shows the reader no design."""
    if not text.startswith("---"):
        return text
    close = _FM_CLOSE_RE.search(text[3:])
    return text[3 + close.end():].lstrip("\n") if close else text


def spine_writes(text: str) -> List[str]:
    """Repo-relative `writes:` paths of the `plan-tasks` fenced block, forward slashes, in order."""
    paths: List[str] = []
    in_block = False
    pending = False
    for line in text.splitlines():
        if not in_block:
            in_block = line.lstrip().startswith("```") and "plan-tasks" in line
            continue
        if line.lstrip().startswith("```"):
            break
        item = _ITEM_RE.match(line)
        if pending and item:
            raw = [_unquote(item.group(1))]
        else:
            pending = False
            m = _WRITES_RE.match(line)
            if not m:
                continue
            rest = m.group(1).strip()
            if rest.startswith("["):
                raw = [_unquote(p) for p in rest.strip("[]").split(",")]
            else:
                pending = not rest
                continue
        for p in raw:
            p = p.replace("\\", "/")
            p = p[2:] if p.startswith("./") else p
            if p and "\n" not in p and p not in paths:
                paths.append(p)
    return paths


def tshirt(root: Path, fm: str) -> Optional[str]:
    """`estimate.tshirt` of the plan's `sizing_object` yaml, None when absent or unreadable."""
    m = _FM_SIZING_RE.search(fm)
    rel = _unquote(m.group(1)) if m else ""
    if not rel or rel == "null":
        return None
    try:
        lines = (Path(root) / rel).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    in_estimate = False
    for line in lines:
        if line.startswith("estimate:"):
            in_estimate = True
        elif in_estimate:
            if line and not line[0].isspace():
                break
            t = _TSHIRT_LINE_RE.match(line)
            if t:
                value = t.group(1).upper()
                return value if value in TSHIRT_RANK else None
    return None
