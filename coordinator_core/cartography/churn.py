"""
coordinator_core.cartography.churn — emergent/churn-set primitive (pure functions).

Purpose: promotes the already-correct inline-bash "chunk K" emergence-detection
pass from example-doctrine-repo's architecture-survey skill
(architecture-survey.md:104-120, the `emergent.txt` computation) into tested
Python. The bash already fixes three footguns inline (verified 2026-07-12,
The Staff Engineer plan-review Finding 0) — this module is a PROMOTION of already-correct
math to a tested, reusable engine primitive, not a bug fix. Regression tests
(coordinator_core/cartography/tests/test_churn.py) lock in each mitigation
against future drift.

The three locked-in mitigations (mirrors architecture-survey.md:104-120):
  (a) Collation-safe set-difference — the bash uses `grep -vxF -f catalogued.txt
      churned-all.txt`, a literal full-line diff with NO sort/collation
      precondition (deliberately NOT `comm`, which requires both inputs
      `LC_ALL=C sort`'d on identical collation or it silently emits garbage).
      Ported here as an explicit Python hash-set difference
      (`set(churned_all) - set(catalogued)`), which is collation-agnostic by
      construction — same semantics, no shell/locale dependency.
  (b) Deleted-file-at-HEAD filter — `git log --name-only` lists both additions
      AND deletions; a path present in the emergent set but absent at HEAD is a
      deletion record, not uncatalogued architecture. The bash cross-checks via
      `git ls-files -- $(cat emergent.txt)`; ported here as an intersection with
      the caller's `git ls-files` output at HEAD.
  (c) Source-dir prefilter — excludes `docs/`, `tasks/`, `archive/` (and other
      caller-supplied meta-directories) from the emergent set BEFORE the
      chunk-K threshold test, so non-architectural churn (doc edits, archival
      sweeps) does not inflate the emergent set or trigger false chunk-K
      passes.

Spec backlink: docs/plans/2026-07-12-claude-klabauter-cartography-substrate-strand-a.md
§ chunk C3 (cartography.churn — promote chunk-K inline bash to tested Python).

Negative-spec:
  - Does NOT shell out to git itself — callers supply the already-computed
    `churned_all` / `catalogued` / `head_present` path lists (typically derived
    from `git log --name-only` / `git ls-files` by the op wrapper or caller).
    This keeps the primitive pure and independently testable without a git
    fixture for every call site.
  - Does NOT re-derive the chunk-K threshold decision (churned-files > 50% of
    catalogued-files) — this module returns the emergent set; threshold
    policy is a caller/consumer concern (example-doctrine-repo's Phase-0.5 gate, Part B, not
    executed here).
  - Does NOT treat a path outside every prefiltered source dir as "not
    emergent" silently — such paths are excluded from the emergent set
    (that is the point of the prefilter), not miscounted as catalogued.

Additive extension (chunk C3, docs/plans/2026-08-06-churn-emergent-detection-
file-granularity.md): `compare_against_recorded_atlas` adds a SEPARATE,
purely-additive comparison between a `RecordedExpansion` (live tracked-file
membership under the RECORDED mapping rule, from
coordinator_core.cartography.atlas_record) and a `RecordedAtlas` (the
RECORDED per-system file counts from the same module). This is a NEW
capability, not a redefinition of `compute_emergent_set`/`ChurnResult`.

Negative-spec (additive extension):
  - Does NOT change `compute_emergent_set`'s or `ChurnResult`'s semantics —
    `emergent`, `excluded_by_prefilter`, `deleted_at_head` are untouched.
  - Does NOT re-derive or touch `churn_ratio` — that stays bound to
    [0.0, 1.0] by the op wrapper's existing catalogued-at-HEAD population;
    this comparison is a wholly separate denominator (see `AtlasComparison
    .denominator`, sourced from `RecordedExpansion.considered_count`).
  - Does NOT touch disk — `compare_against_recorded_atlas` is pure, taking
    an already-loaded `RecordedAtlas`/`RecordedExpansion` pair; disk access
    is `atlas_record.load_recorded_atlas`'s sole concern.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coordinator_core.cartography.atlas_record import (
        RecordedAtlas,
        RecordedExpansion,
    )


DEFAULT_EXCLUDED_DIRS: tuple[str, ...] = ("docs/", "tasks/", "archive/")


@dataclass(frozen=True)
class ChurnResult:
    """Result of an emergent/churn-set computation.

    Attributes:
        emergent: sorted list of file paths present in ``churned_all`` but
            absent from ``catalogued``, filtered to paths that (1) still
            exist at HEAD and (2) fall under a retained source directory
            (i.e. NOT excluded by ``excluded_dirs``).
        excluded_by_prefilter: sorted list of paths that would otherwise be
            emergent but were dropped by the source-dir prefilter (mitigation
            (c)) — surfaced for diagnostics/testing, not part of ``emergent``.
        deleted_at_head: sorted list of paths that would otherwise be
            emergent but do not exist at HEAD (mitigation (b)) — surfaced for
            diagnostics/testing, not part of ``emergent``.
    """

    emergent: list[str] = field(default_factory=list)
    excluded_by_prefilter: list[str] = field(default_factory=list)
    deleted_at_head: list[str] = field(default_factory=list)


def compute_emergent_set(
    churned_all: list[str],
    catalogued: list[str],
    head_present: list[str],
    excluded_dirs: tuple[str, ...] = DEFAULT_EXCLUDED_DIRS,
) -> ChurnResult:
    """Compute the emergent (changed-but-uncatalogued) file set.

    Mirrors architecture-survey.md:104-120 chunk K, promoted to tested Python.

    Args:
        churned_all: all file paths changed across the tree in the diff window
            (the bash's ``churned-all.txt`` — e.g. from
            ``git log --since=<date> --name-only --pretty=format: | sort -u``).
        catalogued: file paths changed in the same window that fall under a
            catalogued system directory (the bash's ``catalogued.txt`` — the
            same diff, scoped to ``-- <system-dirs>``).
        head_present: file paths that exist at HEAD (the bash's
            ``git ls-files`` cross-check universe). A path in ``churned_all``
            that is NOT in ``head_present`` is a deletion record, never
            emergent (mitigation (b)).
        excluded_dirs: path prefixes to exclude from the emergent set before
            the chunk-K threshold test (mitigation (c)). Matched as a
            POSIX-style path-prefix test (e.g. ``"docs/"`` matches
            ``"docs/plans/x.md"`` but not ``"my-docs/x.md"``).

    Returns:
        ChurnResult with the final ``emergent`` set plus the two diagnostic
        lists showing what each mitigation removed.

    Mitigation (a) — collation-safe set-difference: implemented as a plain
    Python ``set`` difference (``set(churned_all) - set(catalogued)``), which
    is a literal element-wise comparison with no sort/locale precondition —
    the same guarantee ``grep -vxF`` gives over ``comm``, achieved natively
    rather than via shell-out.
    """
    head_present_set = set(head_present)
    catalogued_set = set(catalogued)

    # Mitigation (a): collation-safe hash-set difference (not comm/sort-dependent).
    raw_emergent = set(churned_all) - catalogued_set

    # Mitigation (b): deleted-file-at-HEAD filter.
    deleted_at_head = {p for p in raw_emergent if p not in head_present_set}
    present_at_head = raw_emergent - deleted_at_head

    # Mitigation (c): source-dir prefilter (exclude meta-churn dirs).
    excluded_by_prefilter = {
        p for p in present_at_head if _is_excluded(p, excluded_dirs)
    }
    emergent = present_at_head - excluded_by_prefilter

    return ChurnResult(
        emergent=sorted(emergent),
        excluded_by_prefilter=sorted(excluded_by_prefilter),
        deleted_at_head=sorted(deleted_at_head),
    )


@dataclass(frozen=True)
class SystemDrift:
    """One catalogued system whose live tracked-file membership diverges
    from its RECORDED `files:` fingerprint (docs/architecture/systems/
    <system>.md frontmatter).

    Attributes:
        system: system name (recorded page slug).
        recorded_files: the RECORDED `files:` count for this system.
        live_files: the LIVE count of tracked files the recorded mapping
            rule assigns to this system (RecordedExpansion.by_system).
        delta: live_files - recorded_files. Non-zero is what makes this
            system "drifted"; a system vanishing entirely from live
            membership reports here too (live_files=0, negative delta).
    """

    system: str
    recorded_files: int
    live_files: int
    delta: int


@dataclass(frozen=True)
class AtlasComparison:
    """Purely-additive comparison of a RecordedExpansion against a
    RecordedAtlas — see module docstring "Additive extension".

    Attributes:
        uncatalogued: sorted paths the RECORDED mapping maps to no system
            (file-level granularity — answers "outside any recorded
            system").
        drifted_systems: SystemDrift entries, sorted by system name, for
            every recorded system where delta != 0 (per-system-count
            granularity — answers "inside a recorded system but not in its
            recorded membership"). A system with delta == 0 is NOT
            included.
        denominator: the candidate population this comparison was drawn
            from (RecordedExpansion.considered_count) — the stated
            denominator so a consumer can tell "3 of 1225" from "3 of 12".
        last_mapped: the recorded stamp (RecordedAtlas.last_mapped) this
            comparison is frozen against — never a live re-evaluation.
    """

    uncatalogued: list[str]
    drifted_systems: list[SystemDrift]
    denominator: int
    last_mapped: str | None


def compare_against_recorded_atlas(
    expansion: "RecordedExpansion", atlas: "RecordedAtlas"
) -> AtlasComparison:
    """Compare a RECORDED-rule live expansion against the RECORDED atlas.

    Pure comparison — no disk access. The caller is responsible for
    producing `expansion` (coordinator_core.cartography.atlas_record.
    expand_recorded_mapping) and `atlas` (coordinator_core.cartography.
    atlas_record.load_recorded_atlas) beforehand.

    A system present in `atlas.system_files` but with zero live members
    (absent from `expansion.by_system`, or present with an empty tuple)
    still reports as drifted (live_files=0, negative delta) — a system
    that vanished from live membership is drift, not silence. A system
    present in `expansion.by_system` but absent from `atlas.system_files`
    reports with recorded_files=0.

    Args:
        expansion: live tracked-file membership under the RECORDED mapping
            rule (RecordedExpansion).
        atlas: the RECORDED per-system file counts and stamp (RecordedAtlas).

    Returns:
        AtlasComparison — see class docstring.
    """
    drifted: list[SystemDrift] = []
    systems = set(atlas.system_files) | set(expansion.by_system)
    for system in systems:
        recorded_files = atlas.system_files.get(system, 0)
        live_files = len(expansion.by_system.get(system, ()))
        delta = live_files - recorded_files
        if delta != 0:
            drifted.append(
                SystemDrift(
                    system=system,
                    recorded_files=recorded_files,
                    live_files=live_files,
                    delta=delta,
                )
            )
    drifted.sort(key=lambda d: d.system)

    return AtlasComparison(
        uncatalogued=sorted(expansion.uncatalogued),
        drifted_systems=drifted,
        denominator=expansion.considered_count,
        last_mapped=atlas.last_mapped,
    )


def _is_excluded(path: str, excluded_dirs: tuple[str, ...]) -> bool:
    """Return True if *path* falls under one of *excluded_dirs* prefixes.

    Uses PurePosixPath-normalized prefix comparison (path-segment aware, not
    a raw string ``startswith`` — ``"docs/"`` excludes ``"docs/x.md"`` but
    NOT ``"my-docs/x.md"``).
    """
    normalized = PurePosixPath(path.replace("\\", "/"))
    for excluded in excluded_dirs:
        excluded_norm = PurePosixPath(excluded.rstrip("/"))
        if normalized == excluded_norm or excluded_norm in normalized.parents:
            return True
    return False
