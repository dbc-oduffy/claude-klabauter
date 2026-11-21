"""Per-repo byproduct retention policy: which gitignored units of a category go.

Pure and filesystem-free. The runner lists units, builds `Unit` rows and
applies `select_victims`; this module only holds the category table and the
keep-newest-N / age / size rule.
"""
from __future__ import annotations

from dataclasses import dataclass

from coordinator_core.session.record_homes import home_rel

_DAY = 86400.0
_MB = 1024 * 1024


@dataclass(frozen=True)
class Category:
    """One prunable byproduct family.

    relpath is the repo-relative POSIX root; unit_glob selects its units
    (relative to relpath, `**` allowed); kind is "transient" or "record";
    gitignore_line is the exact `.gitignore` line that ignores the root.
    """

    name: str
    relpath: str
    unit_glob: str
    kind: str
    max_age_days: float
    keep_newest: int
    max_total_bytes: int
    gitignore_line: str


@dataclass(frozen=True)
class Unit:
    """One deletable unit: a path, its newest mtime (epoch seconds) and bytes."""

    path: str
    newest_mtime: float
    bytes: int


def _transient(name: str, relpath: str, unit_glob: str, gitignore_line: str,
               keep_newest: int = 3, max_total_bytes: int = 200 * _MB) -> Category:
    return Category(name, relpath, unit_glob, "transient", 7, keep_newest,
                    max_total_bytes, gitignore_line)


def _record(name: str, relpath: str, unit_glob: str, gitignore_line: str,
            max_age_days: float = 30, keep_newest: int = 20,
            max_total_bytes: int = 100 * _MB) -> Category:
    return Category(name, relpath, unit_glob, "record", max_age_days, keep_newest,
                    max_total_bytes, gitignore_line)


_LOCAL = ".coordinator-local"
_LOCAL_IGNORE = ".coordinator-local/*"

# Absent by design: .coordinator-local/coordinator-sessions (and its claim index),
# state/plan-sidecars, state/ledgers, state/kill-ledger.md, state/memo-outbox,
# .project-rag*. A test pins the absence.
CATEGORIES: tuple[Category, ...] = (
    _transient("scratch-top", "scratch", "*", "scratch/"),
    _transient("scratch-pytest-basetemp", "scratch", "**/pytest-of-*/pytest-*", "scratch/",
               keep_newest=1, max_total_bytes=100 * _MB),
    _transient("scratch-pytest-current", "scratch", "**/pytest-of-*/current", "scratch/"),
    _transient("state-scratch", "state/scratch", "*", "scratch/"),
    _transient("subagent-share", f"{_LOCAL}/subagent-share", "*", _LOCAL_IGNORE),
    _record("warp-runs", home_rel("warp"), "*", home_rel("warp") + "/"),
    _record("review-trail-state", "state/review-trail", "*", "state/review-trail/"),
    _record("review-trail-local", f"{_LOCAL}/review-trail", "*", _LOCAL_IGNORE),
    _record("local-plan-sidecars", f"{_LOCAL}/plan-sidecars", "*", _LOCAL_IGNORE),
    _record("local-workflow-sidecars", f"{_LOCAL}/workflow-sidecars", "*", _LOCAL_IGNORE),
    _record("local-close-jobs", f"{_LOCAL}/close-jobs", "*", _LOCAL_IGNORE),
    _record("local-dispatch-briefs", f"{_LOCAL}/dispatch-briefs", "*", _LOCAL_IGNORE),
    _record("local-ceremony", f"{_LOCAL}/ceremony", "*", _LOCAL_IGNORE),
    _record("housekeeping-cursor-logs", "state", "housekeeping-cursor-*.log", "*.log",
            max_age_days=14, keep_newest=5, max_total_bytes=5 * _MB),
)


def select_victims(units: list[Unit], category: Category, now: float) -> list[Unit]:
    """Return the units of `category` to delete, oldest-decision order.

    Newest first; the first `keep_newest` are always kept. Of the rest, a unit
    goes when older than `max_age_days`, or when keeping it would push the
    running kept-bytes total past `max_total_bytes`.
    """
    ordered = sorted(units, key=lambda u: u.newest_mtime, reverse=True)
    keep = max(category.keep_newest, 0)
    kept_bytes = sum(u.bytes for u in ordered[:keep])
    cutoff = now - category.max_age_days * _DAY
    victims: list[Unit] = []
    for unit in ordered[keep:]:
        if unit.newest_mtime < cutoff or kept_bytes + unit.bytes > category.max_total_bytes:
            victims.append(unit)
        else:
            kept_bytes += unit.bytes
    return victims
