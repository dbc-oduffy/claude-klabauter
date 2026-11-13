"""Mechanical reduction of one cited artifact to an ArtifactRecord: no model, no git.

Keeps ATX headings, the first line under each Decision/Consequences/Outcome section, and the
`yaml plan-tasks` ledger as id | title | disposition triples. Reduced frontmatter lines ride at the
head of `headings` prefixed `fm: ` (the record has no frontmatter field). The record is trimmed to
stay under MAX_RECORD_TOKENS by the token proxy.
"""

from __future__ import annotations

import re
from pathlib import Path

from coordinator_core.backlog.record_schema import ArtifactRecord, token_proxy
from coordinator_core.distill.scan_digest import _reduce_frontmatter

__all__ = ["MAX_RECORD_TOKENS", "extract_record", "record_tokens"]

MAX_RECORD_TOKENS = 550

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*(.*?)\s*$")
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_SECTION_RE = re.compile(r"^(decision|decisions|consequences|outcome|outcomes)\b", re.IGNORECASE)
_BOLD_LEAD_RE = re.compile(r"^\*\*(Decision|Consequences|Outcome)s?:?\*\*:?\s*(.+)$", re.IGNORECASE)
_ID_RE = re.compile(r"^- id:\s*(.*?)\s*$")
_FIELD_RE = re.compile(r"^ {2}(title|disposition):\s*(.*?)\s*$")

_MAX_HEADINGS = 24
_MAX_FM = 6
_MAX_SECTION_LINES = 4
_MAX_TASKS = 24
_LINE_CHARS = 110


def _clip(text: str, limit: int = _LINE_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _split_frontmatter(lines: list[str]) -> tuple[list[str], list[str]]:
    if lines and lines[0].strip() == "---":
        close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if close is not None:
            return lines[1:close], lines[close + 1 :]
    return [], lines


def _task_rows(block: list[str]) -> list[str]:
    rows: list[str] = []
    cur: dict[str, str] | None = None

    def flush() -> None:
        if cur is not None and cur.get("id"):
            rows.append(f"{cur['id']} | {_clip(cur.get('title', ''), 80)} | {cur.get('disposition', '')}")

    for raw in block:
        line = raw.rstrip()
        m = _ID_RE.match(line)
        if m:
            flush()
            cur = {"id": m.group(1)}
            continue
        f = _FIELD_RE.match(line)
        if f and cur is not None:
            cur[f.group(1)] = f.group(2).strip("\"'")
    flush()
    return rows


def record_tokens(record: ArtifactRecord) -> int:
    """token_proxy of the record's rendered fields."""
    parts = [record.path, *record.headings, *record.decisions, *record.outcomes, *record.tasks_ledger]
    return token_proxy("\n".join(parts))


def _trim(record: ArtifactRecord) -> ArtifactRecord:
    fields = {
        "headings": list(record.headings),
        "decisions": list(record.decisions),
        "outcomes": list(record.outcomes),
        "tasks_ledger": list(record.tasks_ledger),
    }
    while True:
        cur = ArtifactRecord(record.path, **{k: tuple(v) for k, v in fields.items()})
        if record_tokens(cur) < MAX_RECORD_TOKENS:
            return cur
        name = max(fields, key=lambda k: sum(len(x) for x in fields[k]))
        if not fields[name]:
            return cur
        fields[name].pop()


def extract_record(path: str, text: str | None = None, outline: str | None = None) -> ArtifactRecord:
    """Reduce one artifact to an ArtifactRecord. `outline` (a C2 outline, as text) stands in for the
    file read; with neither `outline` nor `text`, `path` is read from disk (unreadable -> empty record)."""
    if outline is not None:
        text = outline
    if text is None:
        try:
            text = Path(path).read_bytes().decode("utf-8", errors="replace")
        except OSError:
            return ArtifactRecord(path=path)
    fm_lines, body = _split_frontmatter(text.splitlines())
    fm = [f"fm: {_clip(x.strip(), 60)}" for x in _reduce_frontmatter(fm_lines) if x.strip()][:_MAX_FM]

    headings: list[str] = []
    decisions: list[str] = []
    outcomes: list[str] = []
    tasks: list[str] = []
    section: str | None = None
    taken = 0
    i = 0
    while i < len(body):
        line = body[i]
        # Fast path: only lines led by ` ~ # * can match a pattern; three regexes per prose line
        # was most of this function's cost.
        lead = line.lstrip()[:1]
        if lead not in ("`", "~", "#", "*"):
            if section and taken < 1 and lead:
                target = outcomes if section.startswith("outcome") else decisions
                target.append(_clip(f"{section}: {line.strip()}"))
                taken += 1
            i += 1
            continue
        fence = _FENCE_RE.match(line) if lead in ("`", "~") else None
        if fence:
            marker = fence.group(1)
            info = " ".join(fence.group(2).split())
            end = next(
                (j for j in range(i + 1, len(body))
                 if body[j].strip().startswith(marker) and set(body[j].strip()) == {marker[0]}),
                len(body),
            )
            if info in ("yaml plan-tasks", "plan-tasks"):
                tasks.extend(_task_rows(body[i + 1 : end]))
            i = end + 1
            continue
        h = _HEADING_RE.match(line) if lead == "#" else None
        if h:
            title = h.group(2)
            headings.append(f"{h.group(1)} {_clip(title, 70)}")
            sm = _SECTION_RE.match(title)
            section = sm.group(1).lower() if sm else None
            taken = 0
        else:
            b = _BOLD_LEAD_RE.match(line.strip())
            if b:
                target = outcomes if b.group(1).lower() == "outcome" else decisions
                target.append(_clip(f"{b.group(1)}: {b.group(2)}"))
            elif section and line.strip() and taken < 1:
                target = outcomes if section.startswith("outcome") else decisions
                target.append(_clip(f"{section}: {line.strip()}"))
                taken += 1
        i += 1

    record = ArtifactRecord(
        path=path,
        headings=tuple(fm + headings[:_MAX_HEADINGS]),
        decisions=tuple(decisions[:_MAX_SECTION_LINES]),
        outcomes=tuple(outcomes[:_MAX_SECTION_LINES]),
        tasks_ledger=tuple(tasks[:_MAX_TASKS]),
    )
    return _trim(record)
