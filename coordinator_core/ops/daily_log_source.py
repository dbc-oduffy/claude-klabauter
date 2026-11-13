"""Plans completed in a half-open window: one adapter, example-retrieval-repo view first, filesystem scan fallback.

``plans_completed(repo, since, until)`` returns ``[{plan, summary_line, sha, sizing}]`` for plans
marked complete in ``[since, until)``. The preferred source is example-retrieval-repo's read-only
``plans_completed`` sqlite view (doc_files read contract), deleted plans and completing sha
included. When the store or view is absent the fallback is one ``os.scandir`` pass over docs/plans
month dirs in the window, reading frontmatter only (terminal ``status:`` plus a
completed/updated/asserted_at day, else file mtime): no git spawn, ``sha`` None, deleted plans
unlisted, reply marked degraded. No model or agent module is imported.
"""
from __future__ import annotations

import datetime
import os
import re
import sqlite3
from pathlib import Path
from typing import Iterator, Optional


TERMINAL_STATUSES = ("complete", "completed", "implemented", "landed")
SIZING_RANK = {"XS": 0, "S": 1, "M": 2, "L": 3, "XL": 4}
SUMMARY_MAX = 240
PLANS_PATHSPEC = "docs/plans"
FRONT_BYTES = 8192
DEGRADED_VIEW_ABSENT = {
    "mode": "view-absent",
    "reason": "plans_completed view absent: filesystem scan of docs/plans; completing sha unknown, deleted plans not listed",
}

_DATE_RE = re.compile(r"^\s*(?:completed|completed_at|updated|asserted_at):\s*[\"']?(\d{4}-\d{2}-\d{2})", re.M)
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_STATUS_RE = re.compile(r"^status:\s*[\"']?([A-Za-z_-]+)", re.M)
_SIZING_OBJECT_RE = re.compile(r"^sizing_object:\s*[\"']?([^\"'#\s]+)", re.M)
_TSHIRT_RE = re.compile(r"^\s+tshirt:\s*[\"']?([A-Za-z]+)", re.M)
_TITLE_RE = re.compile(r"^title:\s*[\"']?(.+?)[\"']?\s*$", re.M)
_PROBLEM_RE = re.compile(r"^## Problem\s*$", re.M)
_VIEW_COLUMNS = ("plan", "summary_line", "sha", "sizing")
VIEW_DB_RELPATH = ("project-rag", "indexes", "doc_files.sqlite")


def _epoch(moment: "datetime.datetime | int | float") -> int:
    if isinstance(moment, datetime.datetime):
        if moment.tzinfo is None:
            moment = moment.astimezone()
        return int(moment.timestamp())
    return int(moment)


def view_db_path(repo: "Path | str", settings_home: "Path | str | None" = None) -> Optional[Path]:
    """The example-retrieval-repo doc_files store for ``repo`` under the settings home, or None when absent."""
    if settings_home is None:
        from coordinator_core._settings_home import settings_home as _home

        settings_home = _home()
    slug = Path(repo).resolve().name.replace("-", "_")
    base = Path(settings_home).joinpath(*VIEW_DB_RELPATH[:2])
    for cand in (base / f"project_{slug}" / VIEW_DB_RELPATH[2], base / VIEW_DB_RELPATH[2]):
        if cand.is_file():
            return cand
    return None


def _from_view(db: Path, since: int, until: int) -> Optional["list[dict]"]:
    """Rows from the ``plans_completed`` view, or None when the view cannot answer."""
    try:
        con = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        cur = con.execute(
            "SELECT plan, summary_line, sha, sizing FROM plans_completed "
            "WHERE completed_at >= ? AND completed_at < ?",
            (since, until),
        )
        return [dict(zip(_VIEW_COLUMNS, row)) for row in cur.fetchall()]
    except sqlite3.Error:
        return None
    finally:
        con.close()


def _front(plan_text: str) -> str:
    return plan_text.split("\n---", 1)[0] if plan_text.startswith("---") else ""


def _is_terminal(plan_text: "str | None") -> bool:
    """True when the plan's frontmatter ``status:`` is a terminal value."""
    m = _STATUS_RE.search(_front(plan_text or ""))
    return bool(m and m.group(1).lower() in TERMINAL_STATUSES)


def _completed_span(front: str, mtime: float) -> "tuple[float, float]":
    """``[start, end)`` epoch span of the plan's completion: its frontmatter day, else the file mtime.

    Trap: after a clone or pull mtime is checkout time, so undated terminal plans read as completed then.
    """
    m = _DATE_RE.search(front)
    if m:
        try:
            day = datetime.datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
            return day.timestamp(), day.timestamp() + 86400
        except ValueError:
            pass
    return mtime, mtime + 1


def _plan_files(repo: str, since: int, until: int) -> "Iterator[str]":
    """Plan ``.md`` paths directly under docs/plans and under month dirs overlapping the window."""
    root = os.path.join(repo, PLANS_PATHSPEC)
    lo = datetime.datetime.fromtimestamp(since, datetime.timezone.utc)
    hi = datetime.datetime.fromtimestamp(until, datetime.timezone.utc)
    months = {f"{lo.year:04d}-{lo.month:02d}", f"{hi.year:04d}-{hi.month:02d}"}
    try:
        top = list(os.scandir(root))
    except OSError:
        return
    for entry in top:
        if entry.is_dir():
            if entry.name in months or (_MONTH_RE.match(entry.name) and f"{lo:%Y-%m}" < entry.name < f"{hi:%Y-%m}"):
                try:
                    inner = list(os.scandir(entry.path))
                except OSError:
                    continue
                for e in inner:
                    if e.name.endswith(".md") and e.name.count(".") == 1:
                        yield e.path
        elif entry.name.endswith(".md") and entry.name.count(".") == 1:
            yield entry.path


def _read_head(path: str, size: int = FRONT_BYTES) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read(size).decode("utf-8", "replace")
    except OSError:
        return ""


def _read_sizing(repo: str, plan_text: str) -> Optional[str]:
    m = _SIZING_OBJECT_RE.search(_front(plan_text))
    if not m:
        return None
    t = _TSHIRT_RE.search(_read_head(os.path.join(repo, m.group(1)), 4096))
    return t.group(1).upper() if t else None


def summary_line(plan_text: str) -> str:
    """First prose line of ``## Problem``, else the frontmatter ``title:``, capped at SUMMARY_MAX."""
    m = _PROBLEM_RE.search(plan_text)
    if m:
        for line in plan_text[m.end():].splitlines():
            stripped = line.strip()
            if stripped.startswith("## "):
                break
            if stripped and not stripped.startswith(("#", "```", ">", "|", "<!--")):
                return stripped[:SUMMARY_MAX]
    t = _TITLE_RE.search(plan_text)
    return (t.group(1) if t else "")[:SUMMARY_MAX]


def rank_by_sizing(rows: "list[dict]") -> "list[dict]":
    """Rows ordered by sizing tshirt descending; unknown sizing last; ties by plan path."""
    return sorted(rows, key=lambda r: (-SIZING_RANK.get(str(r.get("sizing") or "").upper(), -1), r["plan"]))


def _from_scan(repo: str, since: int, until: int) -> "list[dict]":
    """Terminal-status plans on disk whose completion day or mtime falls in ``[since, until)``; sha is None."""
    rows = []
    for path in _plan_files(repo, since, until):
        head = _read_head(path)
        if not _is_terminal(head):
            continue
        start, end = _completed_span(_front(head), os.stat(path).st_mtime)
        if not (start < until and end > since):
            continue
        text = head if len(head.encode()) < FRONT_BYTES else _read_head(path, 1 << 20)
        rows.append(
            {"plan": os.path.relpath(path, repo).replace(os.sep, "/"), "summary_line": summary_line(text),
             "sha": None, "sizing": _read_sizing(repo, head)}
        )
    return rows


def plans_completed_result(
    repo: "Path | str",
    since: "datetime.datetime | int | float",
    until: "datetime.datetime | int | float",
    *,
    view_db: "Path | str | None" = None,
    settings_home: "Path | str | None" = None,
) -> dict:
    """``{rows, degraded}`` for plans marked complete in ``[since, until)``.

    ``rows`` are unranked ``{plan, summary_line, sha, sizing}``. The ``plans_completed`` view in
    ``view_db`` (or the settings-home store) is primary. An absent store, absent view or sqlite
    error yields a filesystem scan of docs/plans (no git spawn): ``sha`` is None, deleted plans
    are not listed, and ``degraded`` is ``{"mode": "view-absent", "reason": ...}``; else None.
    """
    lo, hi = _epoch(since), _epoch(until)
    db = Path(view_db) if view_db else view_db_path(repo, settings_home)
    if db is not None:
        rows = _from_view(db, lo, hi)
        if rows is not None:
            return {"rows": rows, "degraded": None}
    return {"rows": _from_scan(str(repo), lo, hi), "degraded": dict(DEGRADED_VIEW_ABSENT)}


def plans_completed(
    repo: "Path | str",
    since: "datetime.datetime | int | float",
    until: "datetime.datetime | int | float",
    *,
    view_db: "Path | str | None" = None,
    settings_home: "Path | str | None" = None,
) -> "list[dict]":
    """The ``rows`` of ``plans_completed_result``."""
    return plans_completed_result(repo, since, until, view_db=view_db, settings_home=settings_home)["rows"]
