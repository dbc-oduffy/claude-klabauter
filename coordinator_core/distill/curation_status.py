"""
coordinator_core.distill.curation_status — derived per-artifact curation-ledger
computation (C11; DEC-1).

Purpose: pure computation for the "curation ledger" DEC-1 defines as DERIVED, not
stored — every field below is recomputed from disk truth on each call (canonical
distillation-log parse via `_common.parse_distillation_log`, `docs/plans/` frontmatter
via `ripe_filter.scan_spec_dir`, the `archive/specs/` harvest-debt set via
`harvest_debt.compute_harvest_debt`, and open `state/improvement-queue/*.yaml`
entries). No stored ledger, no drift, no reconciliation ceremony (DEC-1).

Artifact universe (union of two disjoint trees, keyed by repo-relative posix path so
the two trees can never collide):

  - `docs/plans/*.md`        — active, not-yet-archived plans. `ripe` is meaningful
                                here (frontmatter `status:` in RIPE_STATUSES);
                                `prunable` is always False (too early — pruning a
                                still-active plan is C17's ripeness-safety-guarded
                                job, not this module's).
  - `archive/specs/**/*.md`  — already-archived specs. `ripe` is always False
                                (ripeness is a pre-archival concept); `harvested`
                                and `prunable` are meaningful here.

`harvested`: True if a HARVESTED_DISPOSITIONS log row exists for the artifact's path
(harvest_debt.HARVESTED_DISPOSITIONS — DISTILLED/PROMOTE/harvested/deleted).

`prunable`: True only for an archive/specs artifact that is (a) harvested, (b) not
`blocked_by` any open improvement-queue entry, and (c) not actively referenced
(`_common.active_reference_guard`) — i.e. safe to hand to the C12 disposal-manifest
tier as an eligible candidate. A sidecar-suffixed archive/specs file (C1/DEC-4) is
always prunable with reason "sidecar" regardless of the log (sidecars never earn a
harvested row of their own — see harvest_debt's C5 sidecar exclusion).

`blocked_by`: the sorted list of open (`status: open`) improvement-queue entry slugs
whose `surface` field names this artifact's path — an explicit signal that a live
queue item still concerns the artifact, so it must not be silently pruned out from
under that item.

`last_touched`: the artifact file's own mtime (UTC ISO-8601) — a plain filesystem
read, not a git-log spawn; keeps this module subprocess-free apart from the
pre-existing `active_reference_guard` ripgrep call it reuses unmodified.

Negative-spec: this module performs no writes of any kind (no manifest, no stamp, no
disposal decision) — those are the C12/C13/C14 disposal-tier ops. This module also
never makes the reality-check scout's NEW/ALREADY_CAPTURED/EPHEMERAL/SKIP judgment
(§ Negative spec, AC12) — `harvested`/`ripe`/`prunable`/`blocked_by` are all
mechanically derived signals, not authored classifications.

Spec backlink: docs/plans/2026-07-23-claude-klabauter-driven-ceremony-redesign.md § C11 (DEC-1)
Governing DR: docs/decisions/DR-228-distill-disposal-substrate-writer-category.md § D6
  (scratch-tier writer bounds; this module itself performs no I/O writes — the
  op wrapper, coordinator_core/ops/distill_curation_status.py, is the D6-bound writer)
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path

from coordinator_core.distill._common import (
    active_reference_guard,
    is_sidecar_filename,
    parse_distillation_log,
)
from coordinator_core.distill.harvest_debt import (
    HARVESTED_DISPOSITIONS,
    compute_harvest_debt,
)
from coordinator_core.distill.ripe_filter import scan_spec_dir
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter

__all__ = [
    "PLANS_TREE",
    "ARCHIVE_TREE",
    "ArtifactEntry",
    "CurationStatusResult",
    "compute_curation_status",
]

# Cohort tags — the two disjoint trees this module keys artifacts under, prefixed
# onto every artifact key so a same-named plan and archived spec never collide
# (e.g. "docs/plans/foo.md" vs "archive/specs/2026-07/foo.md" are already distinct
# repo-relative paths, but the explicit tag also lets a consumer group by tree
# without re-deriving it from the path shape).
PLANS_TREE = "plans"
ARCHIVE_TREE = "archive"


@dataclass(frozen=True)
class ArtifactEntry:
    """One per-artifact curation-ledger entry (the shape manifest_schema's
    curation-status `artifacts` dict values pin structurally)."""

    path: str  # repo-relative, posix
    tree: str  # PLANS_TREE or ARCHIVE_TREE
    harvested: bool
    ripe: bool
    prunable: bool
    blocked_by: list[str]
    last_touched: str  # UTC ISO-8601, from the file's own mtime
    reasons: list[str] = field(default_factory=list)  # non-empty only when prunable

    def to_dict(self) -> dict:
        return {
            "harvested": self.harvested,
            "ripe": self.ripe,
            "prunable": self.prunable,
            "blocked_by": list(self.blocked_by),
            "last_touched": self.last_touched,
        }


@dataclass(frozen=True)
class CurationStatusResult:
    """Full computed curation status — everything AC4 asks for: unharvested-RIPE
    count, prunable set with reasons, and the per-artifact ledger."""

    artifacts: dict[str, ArtifactEntry]
    unharvested_ripe_count: int
    prunable: list[dict]  # [{path, reasons: [...]}], sorted by path


def _mtime_iso(path: Path) -> str:
    """Return `path`'s mtime as a UTC ISO-8601 string (second precision)."""
    ts = path.stat().st_mtime
    return (
        _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )


def _open_queue_surface_index(queue_dir: Path) -> dict[str, list[str]]:
    """Return {repo-relative posix path -> sorted [entry-slug, ...]} for every
    OPEN (status: open) improvement-queue YAML entry whose `surface:` field names
    that path.

    A best-effort YAML-frontmatter-shaped read: each entry file is itself a bare
    YAML document (not markdown+frontmatter), so this reads it with the same
    read_fm_field primitive against the whole file text (read_fm_field operates on
    any YAML-shaped text block, not specifically a frontmatter-delimited one).
    A malformed/unreadable entry is skipped, never raised — one bad queue file
    must not abort the whole scan (same posture as ripe_filter._classify_one).
    """
    index: dict[str, list[str]] = {}
    if not queue_dir.is_dir():
        return index
    for entry_path in sorted(queue_dir.glob("*.yaml")):
        try:
            text = entry_path.read_text(encoding="utf-8")
        except OSError:
            continue
        status = read_fm_field(text, "status")
        if status is None or status.strip().strip('"').strip("'") != "open":
            continue
        surface = read_fm_field(text, "surface")
        if not surface:
            continue
        surface = surface.strip().strip('"').strip("'")
        index.setdefault(surface, []).append(entry_path.stem)
    for slugs in index.values():
        slugs.sort()
    return index


def _harvested_paths_exact(log_text: str) -> set[str]:
    """Repo-relative posix paths carrying a HARVESTED_DISPOSITIONS row, matched
    EXACTLY against the log row's own path text (posix-normalized) — no
    specs_dir-relative reduction (unlike harvest_debt._row_path_relative_to_specs_dir),
    because this function is also used against docs/plans paths, which are not
    under any specs_dir at all."""
    harvested: set[str] = set()
    for row in parse_distillation_log(log_text):
        if row.disposition in HARVESTED_DISPOSITIONS:
            harvested.add(Path(row.path).as_posix())
    return harvested


def compute_curation_status(
    repo_root: Path,
    *,
    plans_dir: Path | None = None,
    specs_dir: Path | None = None,
    log_path: Path | None = None,
    queue_dir: Path | None = None,
) -> CurationStatusResult:
    """Compute the full derived curation-status ledger for `repo_root`.

    All directory/file params default to the repo's own conventional locations
    (docs/plans, archive/specs, state/distillation-log.md,
    state/improvement-queue) — override only in tests against a fixture repo.

    An absent `plans_dir` or `specs_dir` degrades to an empty cohort for that
    tree (a fresh checkout or a repo lacking one of the two trees is a legitimate
    state, not an error). An absent `log_path` is NOT silently treated as
    "nothing harvested" for the archive/specs cohort — it fails loud via
    harvest_debt.compute_harvest_debt's own DistillationLogMissingError contract
    (same fail-loud posture harvest_debt itself establishes; an absent log must
    never manufacture a false "everything is unharvested debt" answer). Only the
    docs/plans cohort's `harvested` field falls back to an empty harvested-set
    when the log path is absent AND specs_dir is also absent (both-absent means
    there is genuinely nothing this ledger can derive from archive/specs at all).
    """
    repo_root = Path(repo_root)
    plans_dir = plans_dir if plans_dir is not None else repo_root / "docs" / "plans"
    specs_dir = specs_dir if specs_dir is not None else repo_root / "archive" / "specs"
    log_path = log_path if log_path is not None else repo_root / "state" / "distillation-log.md"
    queue_dir = queue_dir if queue_dir is not None else repo_root / "state" / "improvement-queue"

    blocked_index = _open_queue_surface_index(queue_dir)
    artifacts: dict[str, ArtifactEntry] = {}

    # --- archive/specs cohort: harvested + prunable are meaningful here ---
    if specs_dir.is_dir():
        debt = compute_harvest_debt(specs_dir, log_path)
        debt_set = set(debt.harvest_debt)
        all_specs_relpaths: set[str] = {
            p.relative_to(specs_dir).as_posix()
            for p in specs_dir.rglob("*")
            if p.is_file()
        }
        for rel in sorted(all_specs_relpaths):
            abs_path = specs_dir / rel
            full_path = f"archive/specs/{rel}"
            sidecar = is_sidecar_filename(abs_path.name)
            harvested = (not sidecar) and (rel not in debt_set)
            blocked = blocked_index.get(full_path, [])
            if sidecar:
                prunable = True
                reasons = ["sidecar"]
            elif harvested and not blocked:
                referenced = active_reference_guard(abs_path.stem, repo_root)
                if referenced:
                    prunable = False
                    reasons: list[str] = []
                else:
                    prunable = True
                    reasons = ["harvested, unreferenced"]
            else:
                prunable = False
                reasons = []
            if blocked and prunable:
                # blocked_by always wins over an otherwise-prunable verdict.
                prunable = False
                reasons = []
            artifacts[full_path] = ArtifactEntry(
                path=full_path,
                tree=ARCHIVE_TREE,
                harvested=harvested,
                ripe=False,
                prunable=prunable,
                blocked_by=blocked,
                last_touched=_mtime_iso(abs_path),
                reasons=reasons,
            )

    # --- docs/plans cohort: ripe is meaningful here; never prunable ---
    harvested_exact: set[str] = set()
    if log_path.exists():
        harvested_exact = _harvested_paths_exact(log_path.read_text(encoding="utf-8"))
    if plans_dir.is_dir():
        ripe_result = scan_spec_dir(plans_dir)
        ripe_set = set(ripe_result.harvest)
        all_plan_relpaths = ripe_set | {r.path for r in ripe_result.skip} | set(ripe_result.sidecars)
        for rel in sorted(all_plan_relpaths):
            full_path = f"docs/plans/{rel}"
            abs_path = plans_dir / rel
            artifacts[full_path] = ArtifactEntry(
                path=full_path,
                tree=PLANS_TREE,
                harvested=full_path in harvested_exact,
                ripe=rel in ripe_set,
                prunable=False,
                blocked_by=blocked_index.get(full_path, []),
                last_touched=_mtime_iso(abs_path),
            )

    unharvested_ripe_count = sum(
        1 for e in artifacts.values() if e.ripe and not e.harvested
    )
    prunable_list = [
        {"path": e.path, "reasons": list(e.reasons)}
        for e in sorted(artifacts.values(), key=lambda e: e.path)
        if e.prunable
    ]

    return CurationStatusResult(
        artifacts=artifacts,
        unharvested_ripe_count=unharvested_ripe_count,
        prunable=prunable_list,
    )
