"""Week-marker mechanics for /workweek-start: bootstrap, reset and update of
state/week-changelog/HEADER.md and its per-session priorities fragments.

No git. The commit-priorities and commit-archive-reset verbs commit what
these functions write.

Negative-spec: the daily-file pattern mirrors `_DAILY_FILE_RE` in
coordinator/bin/workweek-complete-close.py (a hyphenated script, not
importable); keep the two in step. Unlike that archiver, reset_week leaves
`*-pending-release.md` in place.
"""

from __future__ import annotations

import re
import shutil
from datetime import date
from pathlib import Path

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.cadence.period_mode import week_window
from coordinator_core.ops.check_weekly_staleness import (
    _PRIOR_WEEK_RELEASED_RE,
    _WEEK_STARTING_RE,
)

WEEK_DIR = Path("state") / "week-changelog"
ARCHIVE_DIR = Path("archive") / "week-changelogs"
UNSET_SENTINEL = "(not yet set"

_DAILY_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}.*\.md$")
_PENDING_RELEASE_SUFFIX = "-pending-release.md"
_LAST_START_RE = re.compile(r"^\*\*Last /workweek-start:\*\*")
_BARE_DATE_RE = re.compile(r"^\*\*Week starting:\*\*[ \t]*(\d{4}-\d{2}-\d{2})")
_UNCHECKED_RE = re.compile(r"^- \[ \] (.*) — (.*)$")
_SID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_HEADER_TEMPLATE = (
    "# Week Changelog\n\n"
    "<!-- Directory convention:\n"
    "     state/week-changelog/ holds the current week's changelog state.\n"
    "     HEADER.md (this file) is written by /workweek-complete on reset and by\n"
    "     /workweek-start on re-run. It is the only shared file in this directory\n"
    "     — all other files are per-machine daily blocks (YYYY-MM-DD-{hostname}.md)\n"
    "     written by /workday-complete, which avoids concurrent-write conflicts.\n\n"
    "     Priorities are NOT stored inline in HEADER.md — each /workweek-start\n"
    "     writer owns its own fragment file, HEADER.priorities.<SID_SHORT>.md,\n"
    "     to avoid a second collaborator's /workweek-start silently overwriting\n"
    "     the first's priorities in the same week. Readers merge all fragments\n"
    "     on read.\n\n"
    "     On /workweek-complete, the full directory (daily files + fragments +\n"
    "     old HEADER) is archived to archive/week-changelogs/<week-start>/ before\n"
    "     HEADER is rewritten and fragments are cleared.\n"
    "     coordinator/bin/check-weekly-staleness.py (.cmd on Windows) reads this\n"
    "     file to compute the staleness signal.\n"
    "-->\n\n"
)
_PRIORITIES_LINE = (
    "**Priorities (from /workweek-start):** see `HEADER.priorities.*.md` fragments "
    "— none yet; run /workweek-start to set priorities.\n"
)
_UNSET_PRIOR = "**Prior week released:** (none recorded)\n"


class WeekUnsetError(RuntimeError):
    """HEADER.md `Week starting:` is the unset sentinel; there is no week to archive."""


def _header_path(repo_root: Path) -> Path:
    return Path(repo_root) / WEEK_DIR / "HEADER.md"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, text.encode("utf-8"))


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _first_line(lines: list[str], pattern: re.Pattern) -> str | None:
    for line in lines:
        if pattern.match(line):
            return line
    return None


def bootstrap(repo_root: Path) -> bool:
    """Create HEADER.md from the template when absent; never overwrite.

    Returns whether it wrote.
    """
    header = _header_path(repo_root)
    if header.exists():
        return False
    _write(
        header,
        _HEADER_TEMPLATE
        + "**Week starting:** (not yet set — run /workweek-start to initialise)\n"
        + _UNSET_PRIOR
        + "**Last /workweek-start:** (none)\n"
        + _PRIORITIES_LINE,
    )
    return True


def write_priorities_fragment(
    repo_root: Path, sid_short: str, priorities: list[tuple[str, str]]
) -> Path:
    """Write this session's own HEADER.priorities.<sid_short>.md.

    One `- [ ] <title> — <goal_path>` line per `(title, goal_path)`. Only the
    named session's file is touched.
    """
    if not _SID_RE.match(sid_short):
        raise ValueError(f"sid_short is not a plain token: {sid_short!r}")
    path = Path(repo_root) / WEEK_DIR / f"HEADER.priorities.{sid_short}.md"
    lines = [
        f"- [ ] {' '.join(title.split())} — {goal_path}" for title, goal_path in priorities
    ]
    _write(path, "\n".join(lines) + ("\n" if lines else ""))
    return path


def unfinished_priorities(repo_root: Path) -> list[tuple[str, str, Path, int]]:
    """Unchecked lines of every priorities fragment, in fragment then line order.

    Each is `(title, goal_path, fragment_path, line_number)` (1-based). Read-only;
    whether a goal is still open is the caller's check.
    """
    week_dir = Path(repo_root) / WEEK_DIR
    out: list[tuple[str, str, Path, int]] = []
    for fragment in sorted(week_dir.glob("HEADER.priorities.*.md")):
        for number, line in enumerate(_read_lines(fragment), start=1):
            m = _UNCHECKED_RE.match(line)
            if m:
                out.append((m.group(1), m.group(2), fragment, number))
    return out


def update_in_place(repo_root: Path, today: date | str) -> None:
    """Set `Last /workweek-start:`; every other line is left byte-identical."""
    header = _header_path(repo_root)
    text = header.read_text(encoding="utf-8")
    new_line = f"**Last /workweek-start:** {today}"
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if _LAST_START_RE.match(line):
            ending = line[len(line.rstrip("\r\n")):]
            lines[i] = new_line + ending
            break
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(new_line + "\n")
    _write(header, "".join(lines))


def _as_date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _header_anchor(lines: list[str]) -> date | None:
    week_line = _first_line(lines, _WEEK_STARTING_RE)
    m = _BARE_DATE_RE.match(week_line) if week_line else None
    return date.fromisoformat(m.group(1)) if m else None


def _daily_week_starts(week_dir: Path, anchor: date) -> dict[Path, date]:
    """Each archivable daily file mapped to the start of its week on `anchor`'s chain."""
    out: dict[Path, date] = {}
    for f in sorted(week_dir.glob("*.md")):
        if not _DAILY_FILE_RE.match(f.name) or f.name.endswith(_PENDING_RELEASE_SUFFIX):
            continue
        try:
            day = date.fromisoformat(f.name[:10])
        except ValueError:
            continue
        out[f] = week_window(day, anchor)[0]
    return out


def missed_weeks(repo_root: Path, today: date | str) -> tuple[str, ...]:
    """Week starts, oldest first, that `reset_week` would archive; () when HEADER is unset or unreadable."""
    root = Path(repo_root)
    try:
        anchor = _header_anchor(_read_lines(_header_path(root)))
    except OSError:
        return ()
    if anchor is None:
        return ()
    current = week_window(_as_date(today), anchor)[0]
    starts = {s for s in _daily_week_starts(root / WEEK_DIR, anchor).values() if s < current}
    if week_window(anchor, anchor)[0] < current:
        starts.add(week_window(anchor, anchor)[0])
    return tuple(s.isoformat() for s in sorted(starts))


def current_week_start(repo_root: Path, today: date | str) -> str:
    """The week start containing `today`, on the chain of HEADER's `Week starting:`.

    An unset HEADER falls back to the newest archived week's chain, then to `today`.
    """
    root = Path(repo_root)
    day = _as_date(today)
    try:
        anchor = _header_anchor(_read_lines(_header_path(root)))
    except OSError:
        anchor = None
    if anchor is None:
        archived = sorted(
            p.name for p in (root / ARCHIVE_DIR).glob("*") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)
        ) if (root / ARCHIVE_DIR).is_dir() else []
        anchor = date.fromisoformat(archived[-1]) if archived else day
    return week_window(day, anchor)[0].isoformat()


def reset_week(repo_root: Path, today: date | str) -> tuple[str, ...]:
    """Archive each missed week and write a fresh HEADER; return the archived week starts, oldest first.

    Daily files bucket by week on HEADER's `Week starting:` weekday chain; each
    missed week goes to archive/week-changelogs/<its-start>/. Files of the
    current week, HEADER.md and `*-pending-release.md` stay. Priorities
    fragments move with HEADER's own week. `Week starting:` becomes the current
    week's start, not `today`. `Prior week released:` carries over. Nothing is
    archived while `today` is still in HEADER's week, and then HEADER is not rewritten.
    Raises WeekUnsetError when `Week starting:` is the unset sentinel.
    """
    root = Path(repo_root)
    header = _header_path(root)
    lines = _read_lines(header)
    anchor = _header_anchor(lines)
    if anchor is None:
        raise WeekUnsetError(
            f"{header}: `Week starting:` is unset; nothing to archive"
        )
    header_week = week_window(anchor, anchor)[0]
    current = week_window(_as_date(today), anchor)[0]

    week_dir = root / WEEK_DIR
    archived: set[date] = set()
    if header_week < current:
        archived.add(header_week)
        (root / ARCHIVE_DIR / header_week.isoformat()).mkdir(parents=True, exist_ok=True)
        for f in sorted(week_dir.glob("HEADER.priorities.*.md")):
            shutil.move(str(f), str(root / ARCHIVE_DIR / header_week.isoformat() / f.name))
    for f, start in _daily_week_starts(week_dir, anchor).items():
        if start >= current:
            continue
        dest = root / ARCHIVE_DIR / start.isoformat()
        dest.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), str(dest / f.name))
        archived.add(start)

    if not archived:
        return ()

    released = _first_line(lines, _PRIOR_WEEK_RELEASED_RE)
    _write(
        header,
        _HEADER_TEMPLATE
        + f"**Week starting:** {current.isoformat()}\n"
        + (released + "\n" if released else _UNSET_PRIOR)
        + "**Last /workweek-start:** (none)\n"
        + _PRIORITIES_LINE,
    )
    return tuple(s.isoformat() for s in sorted(archived))
