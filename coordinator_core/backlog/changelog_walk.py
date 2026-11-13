"""Changelog walker: every ISO week of the history, its flagged changes, and the paths they cite.

A change is flagged when it is a `## Work Completed` bullet of a daily summary (the condensed log
is the filter). Week-changelog day files and WEEK-SUMMARY.md bodies are never flagged sources; a
WEEK-SUMMARY.md only marks a week as having a summary. `weeks_total` is the count of ISO weeks
spanned by the earliest and latest period found, not the number of summaries present. Pure parse:
no git, and no cited artifact is ever opened (ids resolve by one directory listing per target dir).
"""

from __future__ import annotations

import os
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from coordinator_core.backlog.record_schema import FlaggedChange, WeekEntry
from coordinator_core.session import record_homes

__all__ = ["walk_changelogs", "iso_week_key"]

_DAILY_DIR = "archive/daily-summaries"
_WEEK_DIR = "archive/week-changelogs"
_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_SHA_RE = re.compile(r"`([0-9a-f]{7,40})`")
_DR_RE = re.compile(r"\bDR-(\d+)\b")
_ID_RE = re.compile(r"\b(handoff|entry)\s+([0-9a-f]{6})\b", re.IGNORECASE)
_PATH_RE = re.compile(r"(?<![\w/:.@-])((?:[A-Za-z0-9_.\-]+/)+[A-Za-z0-9_.\-]*[A-Za-z0-9_\-]\.[A-Za-z0-9]{1,8})(?![\w/])")
_BOLD_RE = re.compile(r"^\*\*(.+?)\*\*")
_TARGETS = {"handoff": record_homes.home_rel("handoffs"), "entry": "archive/completed"}
_DR_DIR = "docs/decisions"


def iso_week_key(day: date) -> str:
    """The Monday (ISO date string) of the ISO week containing `day`; the key of a WeekEntry."""
    return (day - timedelta(days=day.weekday())).isoformat()


def _frontmatter(text: str) -> dict[str, str]:
    """Flat `key: value` pairs of a leading `---` block; no nesting, no YAML dependency."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep and key and not key.startswith((" ", "\t")):
            out[key.strip()] = value.strip().strip("'\"")
    return out


def _parse_day(value: str | None) -> date | None:
    m = _DATE_RE.match(value or "")
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def _work_bullets(text: str) -> list[str]:
    """Bullets of the `## Work Completed` section, continuation lines folded into their bullet."""
    bullets: list[str] = []
    in_section = False
    for line in text.split("\n"):
        if line.startswith("## "):
            in_section = line[3:].strip().lower() == "work completed"
            continue
        if not in_section:
            continue
        if line.startswith("- "):
            bullets.append(line[2:].strip())
        elif bullets and line.startswith((" ", "\t")) and line.strip():
            bullets[-1] += " " + line.strip()
    return bullets


def _title(bullet: str) -> str:
    m = _BOLD_RE.match(bullet)
    if m:
        return m.group(1).rstrip(":").strip()
    return re.split(r"[:.;]", bullet, maxsplit=1)[0].strip()[:120]


_DR_NAME_RE = re.compile(r"dr-(\d+)-")


class _Index:
    """Lazy one-listing-per-directory resolver of ids to repo-relative paths.

    Trap: every bullet resolves against these listings, so per-lookup work must stay O(1) or a
    plain substring scan over precomputed basenames; a Path or regex per entry per citation took
    the pass over the 500ms bar (~600k Path constructions on the DoE corpus).
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._listings: dict[str, list[tuple[str, str]]] = {}
        self._dr_by_number: Optional[dict[int, list[str]]] = None

    def _listing(self, rel_dir: str) -> list[tuple[str, str]]:
        """(repo-relative posix path, lowercase basename) for every .md under `rel_dir`, sorted."""
        if rel_dir not in self._listings:
            base = os.path.join(str(self._root), rel_dir)
            prefix = rel_dir.strip("/").replace(os.sep, "/")
            found: list[tuple[str, str]] = []
            for cur, _dirs, files in os.walk(base):
                sub = os.path.relpath(cur, base).replace(os.sep, "/")
                head = prefix if sub == "." else f"{prefix}/{sub}"
                for name in files:
                    if name.endswith(".md"):
                        found.append((f"{head}/{name}", name.lower()))
            self._listings[rel_dir] = sorted(found)
        return self._listings[rel_dir]

    def dr(self, number: str) -> list[str]:
        if self._dr_by_number is None:
            table: dict[int, list[str]] = {}
            for rel, name in self._listing(_DR_DIR):
                m = _DR_NAME_RE.match(name)
                if m:
                    table.setdefault(int(m.group(1)), []).append(rel)
            self._dr_by_number = table
        return list(self._dr_by_number.get(int(number), ()))

    def hex_id(self, kind: str, token: str) -> list[str]:
        return [rel for rel, name in self._listing(_TARGETS[kind]) if token in name]


def _cited(bullet: str, index: _Index) -> tuple[list[str], list[str]]:
    """(cited paths in first-seen order, unresolved id labels) of one bullet."""
    paths: list[str] = []
    unresolved: list[str] = []

    def add(found: list[str]) -> None:
        for p in found:
            if p not in paths:
                paths.append(p)

    for m in _PATH_RE.finditer(bullet):
        add([m.group(1)])
    for m in _DR_RE.finditer(bullet):
        hits = index.dr(m.group(1))
        add(hits)
        if not hits:
            unresolved.append(f"DR-{m.group(1)}")
    for m in _ID_RE.finditer(bullet):
        kind, token = m.group(1).lower(), m.group(2).lower()
        hits = index.hex_id(kind, token)
        add(hits)
        if not hits:
            unresolved.append(f"{kind} {token}")
    return paths, unresolved


def _day_changes(day: str, files: list[Path], index: _Index, unresolved_out: list[dict]) -> list[FlaggedChange]:
    """One day's flagged changes across all its daily summaries; bullets sharing a commit sha (or,
    with no sha, the same title) are one record."""
    records: list[dict] = []
    by_sha: dict[str, dict] = {}
    by_title: dict[str, dict] = {}
    for path in sorted(files):
        for bullet in _work_bullets(path.read_text(encoding="utf-8", errors="replace")):
            shas = list(dict.fromkeys(_SHA_RE.findall(bullet)))
            title = _title(bullet)
            rec = next((by_sha[s] for s in shas if s in by_sha), None)
            if rec is None and not shas:
                rec = by_title.get(title.lower())
            paths, unresolved = _cited(bullet, index)
            if rec is None:
                rec = {"title": title, "commits": [], "paths": []}
                records.append(rec)
                by_title.setdefault(title.lower(), rec)
            for s in shas:
                by_sha.setdefault(s, rec)
                if s not in rec["commits"]:
                    rec["commits"].append(s)
            for p in paths:
                if p not in rec["paths"]:
                    rec["paths"].append(p)
            for label in unresolved:
                entry = {"id": label, "day": day, "title": rec["title"]}
                if entry not in unresolved_out:
                    unresolved_out.append(entry)
    return [FlaggedChange(r["title"], day, tuple(r["commits"]), tuple(r["paths"])) for r in records]


def walk_changelogs(root: Path) -> tuple[list[WeekEntry], dict]:
    """Walk every ISO week from the earliest to the latest period under `root` (a DoE checkout).

    Returns (week entries oldest first, ledger). The ledger holds weeks_total, weeks_walked,
    weeks_without_summary, weeks_without_source (spanned weeks with neither a summary nor a daily
    summary; walked, empty), and unresolved ids as {id, day, title}.
    """
    root = Path(root)
    dailies: dict[str, list[Path]] = {}
    for path in sorted((root / _DAILY_DIR).glob("*.md")):
        fm = _frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        day = _parse_day(fm.get("period")) or _parse_day(path.name)
        if day is not None:
            dailies.setdefault(day.isoformat(), []).append(path)

    summaries: list[tuple[date, date, Path]] = []
    for path in sorted((root / _WEEK_DIR).glob("*/WEEK-SUMMARY.md")):
        fm = _frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        start = _parse_day(fm.get("week_start")) or _parse_day(path.parent.name)
        end = _parse_day(fm.get("week_end")) or start
        if start is not None and end is not None:
            summaries.append((start, end, path))

    points = [date.fromisoformat(d) for d in dailies] + [d for s, e, _ in summaries for d in (s, e)]
    ledger: dict = {"weeks_total": 0, "weeks_walked": 0, "weeks_without_summary": 0,
                    "weeks_without_source": 0, "unresolved": []}
    if not points:
        return [], ledger

    first = date.fromisoformat(iso_week_key(min(points)))
    last = date.fromisoformat(iso_week_key(max(points)))
    index = _Index(root)
    entries: list[WeekEntry] = []
    cursor = first
    while cursor <= last:
        week_end = cursor + timedelta(days=6)
        summary = next((p for s, e, p in summaries if s <= week_end and e >= cursor), None)
        days = sorted(d for d in dailies if cursor <= date.fromisoformat(d) <= week_end)
        flagged: list[FlaggedChange] = []
        for d in days:
            flagged.extend(_day_changes(d, dailies[d], index, ledger["unresolved"]))
        if summary is None:
            ledger["weeks_without_summary"] += 1
        if summary is None and not days:
            ledger["weeks_without_source"] += 1
        entries.append(WeekEntry(
            week=cursor.isoformat(),
            summary_path=summary.relative_to(root).as_posix() if summary else None,
            flagged=tuple(flagged),
        ))
        cursor += timedelta(days=7)
    ledger["weeks_total"] = len(entries)
    ledger["weeks_walked"] = len(entries)
    return entries, ledger
