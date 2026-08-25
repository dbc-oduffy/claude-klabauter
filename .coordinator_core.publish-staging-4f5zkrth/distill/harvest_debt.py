"""
coordinator_core.distill.harvest_debt — harvest-debt computation logic.

Purpose: compute "harvest debt" — archive/specs paths (keyed specs_dir-relative,
NOT bare basename — same-named specs across different date-prefixed subdirs must
not collide) that have never been recorded as harvested in the canonical
distillation log. Harvested means the path appears in at least one log row whose
disposition/action is in HARVESTED_DISPOSITIONS — either a canonical-format row
(DISTILLED/PROMOTE, per the DoE canonical format parsed by
coordinator_core.distill._common.parse_distillation_log) or an action-table row
(harvested/deleted, per the DoE schema-header `date | action | path | ...`
format found live on sibling repos, e.g. project-opticon — see
HARVESTED_DISPOSITIONS for why both vocabularies exist). This is the
deterministic mechanical half of the old "did we distill this spec?" judgment
call — it answers "is there a log row for it," not "should it have been
distilled."

Sidecar exclusion (C5): process-scaffolding sidecar files (see
coordinator_core.distill._common.SIDECAR_SUFFIXES /
is_sidecar_filename — the single vocabulary, C1) are excluded from both
total_specs and the harvest_debt set. Sidecars are scaffolding, not
unharvested knowledge — they will never earn their own DISTILLED/PROMOTE
row, so counting them as debt is a false positive, not a real backlog item.

FAIL LOUD on an absent log: an absent/missing distillation log is NEVER treated
as "nothing has been harvested yet, so harvest everything" (the #1 hazard this
module exists to avoid) — it is a hard error, because a renamed-away or
deleted log would otherwise silently manufacture a false-positive debt list
covering the entire archive/specs tree.

Negative-spec: this module performs no writes and does not mutate the log or
archive/specs. It computes and returns; the CLI (bin/distill-harvest-debt.py)
is responsible for stdout/JSON emission only.

Spec backlink: pln-distill-ceremony-mechanical-su-1bcb38 § C4
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from coordinator_core.distill._common import is_sidecar_filename, parse_distillation_log

HARVESTED_DISPOSITIONS = frozenset(
    {
        # Canonical-format disposition tokens (`- <path> -> <DISPOSITION>, <fate>
        # (run: <id>)` rows under `## Run` headers — the format _common's parser reads).
        "DISTILLED",
        "PROMOTE",
        # Action-table tokens from the DoE schema-header format
        # (`date | action | path | last_sha | belongs_to_spec | reason`), which is
        # what sibling repos' live logs actually carry (verified on project-opticon
        # 2026-07-23: 52 `harvested` + 120 `deleted` rows). Both vocabularies exist
        # because the fleet's logs predate the canonical-format cutover and the
        # action-table schema header still mints these rows; matching only the
        # canonical pair read a real log as 0-harvested and emitted a spurious
        # warn. Lowercase exact-match only — no mixed-case occurrence of these two
        # tokens was found on disk. (`deleted` counts as harvested here because a
        # deleted-row path is a RECORDED resolution: the file is gone from
        # archive/specs, so it can never be debt, and counting it keeps the warn
        # ratio's harvested denominator honest. NOTE this is distinct from
        # log_normalize's DR-053 legacy `DELETED`->EPHEMERAL migration mapping,
        # which governs a different, `|`-edged legacy dialect.)
        "harvested",
        "deleted",
    }
)
"""Dispositions/actions that count as "harvested" for harvest-debt purposes —
both the canonical-format disposition vocabulary and the DoE schema-header
action-table vocabulary (see inline comments for why both exist). The other
canonical dispositions (EPHEMERAL, SKIP, PRESERVE) do NOT count — a row logged
under one of those is a recorded decision NOT to fold the spec into the wiki,
not evidence of harvest."""

# One action-table data row of the DoE schema-header format:
#   date | action | path | last_sha | belongs_to_spec | reason
# Anchored on the leading ISO date cell so schema-header comment lines ("# Columns:
# date | action | ...") and canonical-format rows ("- <path> -> ...") never match.
# Tolerates an optional leading "|" (the `|`-edged variant of the same table shape).
_ACTION_TABLE_ROW_RE = re.compile(
    r"^\|?\s*\d{4}-\d{2}-\d{2}\s*\|\s*(?P<action>[^|]+?)\s*\|\s*(?P<path>[^|]+?)\s*(?:\||$)"
)

# Debt-ratio threshold past which compute_harvest_debt sets warn=True. Chosen
# as a coarse "debt dwarfs the log" signal, not a hard failure gate — the
# CLI/consumer decides what to do with the warning.
_WARN_RATIO = 5


class DistillationLogMissingError(FileNotFoundError):
    """Raised when the canonical distillation log path does not exist on disk.

    Deliberately a distinct exception type (not a bare FileNotFoundError) so
    callers can catch specifically for the fail-loud contract without also
    swallowing unrelated FileNotFoundErrors from elsewhere in a call stack."""


@dataclass(frozen=True)
class HarvestDebtResult:
    """Result of compute_harvest_debt.

    Attributes:
        harvest_debt: sorted list of specs_dir-relative paths absent from any
            HARVESTED_DISPOSITIONS row in the log. Sidecar-suffixed paths
            (is_sidecar_filename) are excluded (C5).
        harvested_count: number of distinct specs_dir-relative paths with a
            HARVESTED_DISPOSITIONS row (the "logged harvested" denominator used
            for the warn ratio).
        total_specs: total distinct specs_dir-relative paths found under
            archive/specs, excluding sidecar-suffixed paths (C5).
        warn: True when harvest_debt count is disproportionately large
            relative to harvested_count (debt >> logged rows) — a signal that
            the log may be stale/incomplete, not a hard failure.
    """

    harvest_debt: list[str]
    harvested_count: int
    total_specs: int
    warn: bool


def _specs_dir_relative_paths(specs_dir: Path) -> set[str]:
    # Review: code-reviewer (Finding 1, 2026-07-12) — was keyed on bare Path.name,
    # colliding same-named specs across different archive/specs/<subdir>/ trees and
    # silently under-counting debt. Keyed on specs_dir-relative path (POSIX-normalized)
    # so it compares like-for-like with the log-row path component below.
    #
    # C5: sidecars (is_sidecar_filename, _common.SIDECAR_SUFFIXES) are excluded here
    # — they are process-scaffolding, not unharvested knowledge, and must never enter
    # total_specs or the debt set (they were never going to get a DISTILLED/PROMOTE
    # row of their own; counting them as debt is a false positive).
    return {
        p.relative_to(specs_dir).as_posix()
        for p in specs_dir.rglob("*")
        if p.is_file() and not is_sidecar_filename(p.name)
    }


def _harvested_relative_paths(
    log_text: str, specs_dir: Path, all_specs: set[str] | None = None
) -> set[str]:
    # Two independent scans, one per log vocabulary (see HARVESTED_DISPOSITIONS):
    # canonical-format rows via the shared parser, action-table rows via the local
    # regex. The action-table scan lives HERE (not in _common's parser) because
    # log_normalize.is_already_canonical uses "parser yields >=1 row" as its
    # canonical-shape signal — teaching the shared parser to read action-table rows
    # would misclassify legacy logs as already-canonical.
    #
    # Canonical-format rows join on PATH (specs_dir-relative, as before): those
    # rows are written post-cutover, already carry the current (possibly
    # month-foldered) path shape, and a path join is strictly more precise than
    # a basename join when it is available.
    #
    # Legacy action-table rows join on BASENAME, not path. A legacy row's path
    # (e.g. "archive/specs/foo-plan.md") predates any later month-foldering
    # rewrite (e.g. "archive/specs/2026-07/foo-plan.md") — the row's path
    # component is stale relative to the file's current location, so a path
    # join silently fails to match and manufactures false harvest-debt for the
    # whole month-foldered cohort (cross-repo memo 2026-08-06-project-rag-em-
    # distill-fate-coverage-and-legacy-log-reader.md, ask 2: ~130-spec gap on
    # project-rag's corpus). A basename join survives the rewrite.
    #
    # Collision handling: basenames are NOT unique across different
    # date-prefixed archive/specs subdirectories (same spec name reused, or
    # coincidental name reuse). When a legacy row's basename matches more than
    # one path in `all_specs`, this deliberately credits NONE of them as
    # harvested rather than guessing — last-write-wins would silently mark an
    # arbitrary one of several same-named specs as harvested on behalf of a
    # row that cannot disambiguate which one it meant. Understating harvest
    # credit (more debt, never less) is the safe failure direction here,
    # consistent with this module's fail-loud posture elsewhere. `all_specs`
    # is optional so this function stays testable/callable against a bare
    # log_text; a caller that omits it gets no basename disambiguation (every
    # legacy basename match is credited via the path-relative reduction,
    # matching pre-fix behavior).
    harvested: set[str] = set()
    for row in parse_distillation_log(log_text):
        if row.disposition in HARVESTED_DISPOSITIONS:
            harvested.add(_row_path_relative_to_specs_dir(row.path, specs_dir))

    legacy_rows: dict[str, str] = {}
    for line in log_text.splitlines():
        match = _ACTION_TABLE_ROW_RE.match(line)
        if match and match.group("action") in HARVESTED_DISPOSITIONS:
            basename = Path(match.group("path")).name
            legacy_rows.setdefault(basename, match.group("path"))

    if not legacy_rows:
        return harvested

    if all_specs is None:
        for row_path in legacy_rows.values():
            harvested.add(_row_path_relative_to_specs_dir(row_path, specs_dir))
        return harvested

    basename_index: dict[str, list[str]] = {}
    for rel in all_specs:
        basename_index.setdefault(Path(rel).name, []).append(rel)

    for basename in legacy_rows:
        matches = basename_index.get(basename, [])
        if len(matches) == 1:
            harvested.add(matches[0])
        # 0 matches: nothing on disk to credit, no-op.
        # >1 matches: ambiguous — deliberately uncredited (see comment above).

    return harvested


def _row_path_relative_to_specs_dir(row_path: str, specs_dir: Path) -> str:
    """Reduce a canonical-log row's path (repo-relative, e.g.
    "archive/specs/2026-07/foo-plan.md") to its component below `specs_dir`'s
    own directory name — e.g. "2026-07/foo-plan.md" for a specs_dir named
    ".../specs" — matching the specs_dir-relative shape
    _specs_dir_relative_paths produces.

    Falls back to the basename if `specs_dir.name` is not found as a path
    segment in the row's path (row path doesn't follow the
    <...>/<specs_dir.name>/... convention) — never raises, since a log row's
    path shape is caller-supplied text, not a guaranteed structure.
    """
    parts = Path(row_path).as_posix().split("/")
    dir_name = specs_dir.name
    if dir_name in parts:
        idx = parts.index(dir_name)
        remainder = parts[idx + 1 :]
        if remainder:
            return "/".join(remainder)
    return Path(row_path).name


def compute_harvest_debt(specs_dir: Path, log_path: Path) -> HarvestDebtResult:
    """Compute harvest debt: archive/specs paths (keyed specs_dir-relative, not
    bare basename) absent from any log row whose disposition/action is in
    HARVESTED_DISPOSITIONS (canonical DISTILLED/PROMOTE rows and action-table
    harvested/deleted rows both count — see HARVESTED_DISPOSITIONS).

    FAIL LOUD contract: if `log_path` does not exist, raises
    DistillationLogMissingError — an absent log is NEVER treated as "harvest
    everything" (comm -23 against an empty harvested-set would silently do
    exactly that, which is the hazard this guards against).

    Args:
        specs_dir: the archive/specs directory (or equivalent) to scan for
            files, recursively. Debt/harvested paths are keyed relative to
            this directory (e.g. "2026-07/foo-plan.md"), not bare basename —
            same-named specs across different date-prefixed subdirectories
            must not collide into a single debt-set entry.
        log_path: path to the canonical distillation log file.

    Returns:
        HarvestDebtResult with the sorted harvest-debt specs_dir-relative path
        list plus counts and a warn flag for "debt dwarfs the logged harvested
        set."
    """
    if not log_path.exists():
        raise DistillationLogMissingError(
            f"canonical distillation log not found at {log_path} — refusing to "
            "compute harvest-debt against an absent log (an absent log is NOT "
            "'nothing harvested yet'; it is an unknown state and must fail loud)"
        )

    log_text = log_path.read_text()
    all_specs = _specs_dir_relative_paths(specs_dir) if specs_dir.is_dir() else set()
    harvested = _harvested_relative_paths(log_text, specs_dir, all_specs)

    # comm -23 semantics: everything in all_specs not present in harvested.
    debt = sorted(all_specs - harvested)

    harvested_count = len(harvested)
    warn = harvested_count == 0 and len(debt) > 0
    if not warn and harvested_count > 0:
        warn = len(debt) > harvested_count * _WARN_RATIO

    return HarvestDebtResult(
        harvest_debt=debt,
        harvested_count=harvested_count,
        total_specs=len(all_specs),
        warn=warn,
    )
