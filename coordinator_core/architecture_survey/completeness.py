"""Finish-tail completeness check: every scoped system has a page and an index row, written pages carry today's stamp.

Read-mostly and spawn-free. The caller fails the run on an `incomplete` decision; orphan and
stale pages are findings, never failures.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

from coordinator_core.architecture_survey.contract import Decision
from coordinator_core.cartography.atlas_record import _parse_last_mapped

ATLAS_DIR = Path("docs") / "architecture"
SYSTEMS_DIR = ATLAS_DIR / "systems"
INDEX_FILE = "systems-index.md"
ATLAS_WIDE_FILES: tuple[str, ...] = (
    "systems-index.md",
    "connectivity-matrix.md",
    "cross-system-map.md",
    "file-index.md",
)
STALE_AFTER_DAYS = 90

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---", re.DOTALL)
_ROW_NAME_RE = re.compile(r"^\|\s*\[?`?([A-Za-z0-9_.\-]+)`?\]?(?:\([^)]*\))?\s*\|")


@dataclass(frozen=True)
class Completeness:
    """`decision` is the gate; `orphan_pages` and `stale_pages` are receipt findings only."""

    decision: Decision
    orphan_pages: tuple[str, ...] = field(default_factory=tuple)
    stale_pages: tuple[str, ...] = field(default_factory=tuple)


def _frontmatter_last_mapped(text: str) -> str | None:
    match = _FRONTMATTER_RE.match(text)
    return _parse_last_mapped(match.group(1)) if match else None


def _index_names(index_path: Path) -> set[str]:
    if not index_path.is_file():
        return set()
    names: set[str] = set()
    for line in index_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _ROW_NAME_RE.match(line)
        if match:
            names.add(match.group(1))
    return names


def verify(
    repo_root: str | Path,
    systems: Iterable[str],
    pages_written: Iterable[str],
    *,
    mode: str,
    today: date,
) -> Completeness:
    """Check the atlas under `repo_root` against the scoped `systems` and the `pages_written` list.

    `pages_written` entries are repo-relative or absolute paths. Atlas-wide pages are required
    non-empty on `bootstrap` and on any run whose scope is the whole atlas (`refresh`).
    """
    root = Path(repo_root)
    scoped = sorted(set(systems))
    misses: list[str] = []

    systems_dir = root / SYSTEMS_DIR
    indexed = _index_names(root / ATLAS_DIR / INDEX_FILE)
    for name in scoped:
        if not (systems_dir / f"{name}.md").is_file():
            misses.append(f"missing-page: {name}")
        if name not in indexed:
            misses.append(f"missing-index-row: {name}")

    if mode in ("bootstrap", "refresh"):
        for fname in ATLAS_WIDE_FILES:
            path = root / ATLAS_DIR / fname
            if not path.is_file() or path.stat().st_size == 0:
                misses.append(f"missing-or-empty: {(ATLAS_DIR / fname).as_posix()}")

    today_s = today.isoformat()
    for page in sorted(set(pages_written)):
        path = Path(page)
        if not path.is_absolute():
            path = root / path
        if not path.is_file():
            misses.append(f"written-page-absent: {page}")
            continue
        stamp = _frontmatter_last_mapped(path.read_text(encoding="utf-8", errors="replace"))
        if stamp != today_s:
            misses.append(f"last-mapped-not-today: {page} ({stamp})")

    scoped_set = set(scoped)
    orphans: list[str] = []
    stale: list[str] = []
    cutoff = today - timedelta(days=STALE_AFTER_DAYS)
    if systems_dir.is_dir():
        for page in sorted(systems_dir.glob("*.md")):
            if mode != "targeted" and page.stem not in scoped_set:
                orphans.append(page.relative_to(root).as_posix())
            stamp = _frontmatter_last_mapped(page.read_text(encoding="utf-8", errors="replace"))
            try:
                mapped = date.fromisoformat(stamp) if stamp else None
            except ValueError:
                mapped = None
            if mapped is not None and mapped < cutoff:
                stale.append(page.relative_to(root).as_posix())

    complete = not misses
    decision = Decision(
        id="completeness",
        chosen="complete" if complete else "incomplete",
        rule=(
            "every scoped system has a page and an index row; atlas-wide pages non-empty on "
            "bootstrap/refresh; every written page has last_mapped == today"
        ),
        evidence=(
            f"{len(scoped)} systems checked, {len(misses)} misses"
            + ("" if complete else ": " + "; ".join(misses))
        ),
    )
    return Completeness(decision=decision, orphan_pages=tuple(orphans), stale_pages=tuple(stale))
