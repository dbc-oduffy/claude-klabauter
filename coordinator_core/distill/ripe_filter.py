"""
coordinator_core.distill.ripe_filter — pure frontmatter scan partitioning a spec dir into
harvest-ripe vs skip-worthy specs.

Purpose: the first of the 3 independent (no-DoE-coupling) distill-ceremony scripts. Scans
every Markdown file under a given spec directory — recursing into subdirectories, so a
month-foldered archive layout (`archive/specs/YYYY-MM/*.md`) is handled in one invocation —
reads its `status:` frontmatter field, and partitions the set into three buckets:

  - SIDECARS: process-scaffolding files matching `_common.is_sidecar_filename` — checked
    BEFORE any frontmatter read, so a sidecar never reaches RIPE/SKIP classification even
    if it happens to carry a `status:` field of its own (queue F5 — sidecars used to fall
    through into the RIPE/SKIP frontmatter classification and could land in the harvest
    cohort).
  - RIPE (harvest-eligible): `status: implemented` or `status: shipped`.
  - SKIP: `status: superseded`, `abandoned`, or `partial` — carries a {path, status, reason}
    record explaining why it was skipped.

Any other status value (e.g. `draft`, `reviewed`, `approved`, `executing`) or a missing
`status:` field is also treated as SKIP (not-yet-ripe), with a reason naming the actual
status found (or "no status field").

No LLM, no coupling to DoE's canonical-log/distill_fate work — this is a pure, deterministic
frontmatter read. Consumes coordinator_core.distill._common's read-only frontmatter
re-exports (split_frontmatter, read_fm_field), not a divergent YAML parser.

Negative-spec: does not read the log, does not touch archive/specs/**, does not call
active_reference_guard, does not write anything — output is a pure function of a spec dir's
current frontmatter contents.

Spec backlink: pln-distill-ceremony-mechanical-su-1bcb38 § C1
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from coordinator_core.distill._common import (
    is_sidecar_filename,
    read_fm_field,
    split_frontmatter,
)
from coordinator_core.lifecycle_constants import SPEC_RIPE_STATUSES, SPEC_SKIP_STATUSES

_LOG = logging.getLogger(__name__)

__all__ = [
    "RIPE_STATUSES",
    "SKIP_STATUSES",
    "SkipRecord",
    "RipeFilterResult",
    "scan_spec_dir",
]

RIPE_STATUSES = SPEC_RIPE_STATUSES
"""status: values that mark a spec as harvest-ripe."""

SKIP_STATUSES = SPEC_SKIP_STATUSES
"""status: values that explicitly mark a spec as not-yet-harvestable."""


@dataclass(frozen=True)
class SkipRecord:

    path: str
    status: str | None
    reason: str


@dataclass(frozen=True)
class RipeFilterResult:

    harvest: list[str]
    skip: list[SkipRecord]
    sidecars: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "harvest": list(self.harvest),
            "skip": [
                {"path": r.path, "status": r.status, "reason": r.reason}
                for r in self.skip
            ],
            "sidecars": list(self.sidecars),
        }


def _classify_one(path: Path) -> tuple[bool, str | None, str | None]:
    """Return (is_ripe, status, skip_reason) for a single Markdown file.

    is_ripe is True only for status in RIPE_STATUSES. skip_reason is None when
    is_ripe is True (no reason needed for a harvest hit)."""
    text = path.read_text(encoding="utf-8")
    split = split_frontmatter(text)
    if split is None:
        return False, None, "no frontmatter block"

    status = read_fm_field(split.fm_text, "status")
    if status is None:
        return False, None, "no status field"

    status = status.strip()
    if status in RIPE_STATUSES:
        return True, status, None
    if status in SKIP_STATUSES:
        return False, status, f"status: {status}"
    return False, status, f"status: {status} (not yet ripe)"


def scan_spec_dir(spec_dir: Path) -> RipeFilterResult:
    spec_dir = Path(spec_dir)
    harvest: list[str] = []
    skip: list[SkipRecord] = []
    sidecars: list[str] = []

    md_paths: list[Path] = []
    pending_dirs: list[Path] = [spec_dir]
    while pending_dirs:
        current_dir = pending_dirs.pop()
        try:
            entries = list(current_dir.iterdir())
        except OSError as exc:
            _LOG.warning("scan_spec_dir: cannot scan %s — %s", current_dir, exc)
            raise
        for entry in entries:
            if entry.is_dir():
                pending_dirs.append(entry)
            elif entry.suffix == ".md" and entry.is_file():
                md_paths.append(entry)

    md_paths.sort(key=lambda p: p.relative_to(spec_dir).as_posix())

    for md_path in md_paths:
        rel_path = md_path.relative_to(spec_dir).as_posix()

        if is_sidecar_filename(md_path.name):
            sidecars.append(rel_path)
            continue

        try:
            is_ripe, status, reason = _classify_one(md_path)
        except (OSError, UnicodeDecodeError) as exc:
            skip.append(
                SkipRecord(path=rel_path, status=None, reason=f"unreadable: {exc.__class__.__name__}")
            )
            continue

        if is_ripe:
            harvest.append(rel_path)
        else:
            skip.append(SkipRecord(path=rel_path, status=status, reason=reason))

    sidecars.sort()
    return RipeFilterResult(harvest=harvest, skip=skip, sidecars=sidecars)
