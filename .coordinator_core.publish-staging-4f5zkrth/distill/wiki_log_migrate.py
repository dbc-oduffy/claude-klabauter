"""
coordinator_core.distill.wiki_log_migrate — one-time RETIREMENT of makima's legacy
wiki-side distill ledger (C7).

Purpose: `docs/wiki/.distill-log.md` (the `<spec> → <wiki-target>` unicode-arrow
ledger) is a SECOND, divergent distillation-log instance — the real one opticon's §2
reflection was pointing at (opticon's premise that "DoE has two logs" is wrong; DoE's
schema declares the wiki-side path fictional. Makima's own copy is the genuine
divergent instance). This module folds every row of that ledger into the canonical
`state/distillation-log.md` (DoE C1 canonical schema, `_common.parse_distillation_log`
grammar) as a DISTILLED or SKIP row, then replaces the wiki-side file with a one-line
tombstone. Run exactly once per repo.

Why a sibling module rather than extending `log_normalize` (chunk brief named
`log_normalize.py` as the surface, but left the module choice to the executing agent
— "or a one-shot sibling migrator, EM's call at execute time"): the two source
formats are unrelated schemas serving unrelated grammars — `log_normalize` migrates
the legacy PIPE-TABLE format (`date | action | path | last_sha | belongs_to_spec |
reason`, DR-053-governed, a DoE-binding contract §7), while this module migrates a
freeform markdown ledger with THREE distinct section shapes (see below) and no
DR-053 contract obligations of its own. Merging them into one module would blur two
independently-evolving contracts; this module instead reuses `log_append` (the
canonical-log WRITER both modules share) and mirrors `log_normalize`'s
backup-before-write + refuse-rerun discipline verbatim.

Source ledger shape (as authored, `docs/wiki/.distill-log.md`):
    ## Harvested-upstream — <label> (reclaimed to DoE, pruned <date>)
    <glob-or-path> → harvested-upstream (<free text>) — <free text>

    ## Harvested — <label> (run <run-id>)
    <spec-path> → <wiki-target-path>

    ## Skipped — superseded (<free text>)
    <spec-path>  # <reason>

Row mapping to the canonical schema (`- <path> -> <disposition>, <fate> (run: <run_id>)`):
    Harvested-upstream row -> disposition DISTILLED; path = the section's left-hand
        token (may be a glob, e.g. `archive/specs/2026-03..06/**` — the canonical
        parser only requires a non-whitespace `\\S+` token, it does not require the
        path to resolve to a real file); fate = the right-hand free text, prefixed
        "reclaimed-upstream: ".
    Harvested row -> disposition DISTILLED; path = the spec path; fate =
        "harvested into <wiki-target>" (mirrors log_normalize's distill-harvest
        field-remap: the wiki target is folded into fate, never overloaded onto path).
    Skipped row -> disposition SKIP; path = the spec path; fate = the `#`-comment
        reason text verbatim.
Every migrated row's fate carries a `(migrated from docs/wiki/.distill-log.md)`
provenance suffix so a reader of `state/distillation-log.md` can tell a
migrated-historical row from a row `log_append` wrote live.

Run-id choice (mirrors log_normalize's per-batch-not-synthetic-single-run rationale):
each section's own header names its batching key (a `pruned <date>` token for the
harvested-upstream section, a `(run <run-id>)` token for the harvested section) and
that literal token is reused as the canonical run-id so downstream `## Run <id>`
consumers keep the same batch boundary this ledger already used. The Skipped section
carries no such token in its header — those rows use the fallback constant
`FALLBACK_RUN_ID` ("wiki-log-migration", not date-stamped, so the migration itself
stays deterministic and reproducible in tests regardless of wall-clock time).

Invariants (fail-loud, mirrors log_normalize's discipline):
  1. Run exactly once per repo — refuses (`AlreadyMigratedError`) if the wiki-log file
     is already tombstoned (detected via `TOMBSTONE_MARKER`).
  2. Preserves the original — the wiki-log file is backed up to `<path>.legacy-backup`
     BEFORE being overwritten with the tombstone. Refuses (`FileExistsError`) if that
     backup sibling already exists — never clobbers a prior run's only remaining copy.
  3. Every row in the source ledger is accounted for: `rows_migrated == ` the total
     count of recognized data rows across all three sections; a row this parser
     cannot recognize raises `MalformedWikiLogError` rather than being silently
     dropped (there is no legitimate "unrecognized but intentional" category in this
     ledger, unlike the DR-053 pipe-table's RECOGNIZED_SKIP_ACTIONS — every line in a
     recognized section is a data row).
  4. The canonical-log append is atomic-per-invocation via `log_append.append_rows`
     (validate-all-then-write-once; nothing is written if any row is invalid).
  5. The tombstone write is atomic (temp file + `os.replace`).

Negative-spec: this module does not run itself against any live repo file as a side
effect of import — invocation is always explicit, via `migrate_wiki_log`. It does not
delete the wiki-log file; the original is preserved in-place at `backup_path`.

Spec backlink: pln-makima-driven-ceremony-redesig-c7fe9a § C7.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from coordinator_core.distill.log_append import append_rows

__all__ = [
    "TOMBSTONE_MARKER",
    "FALLBACK_RUN_ID",
    "WikiLogMigrateResult",
    "AlreadyMigratedError",
    "MalformedWikiLogError",
    "is_already_tombstoned",
    "render_tombstone",
    "migrate_wiki_log",
]


TOMBSTONE_MARKER = "# RETIRED — migrated to state/distillation-log.md"
"""Presence of this literal leading line marks the wiki-log file as already-tombstoned
(the refuse-rerun signal, mirroring log_normalize.CANONICAL_HEADER_MARKER)."""

FALLBACK_RUN_ID = "wiki-log-migration"
"""Run-id used for rows whose source section carries no date/run token of its own
(the Skipped — superseded section) — deliberately not wall-clock-derived, so a test
fixture's expected run-id never depends on the day the test runs."""

_HARVESTED_UPSTREAM_HEADER_RE = re.compile(r"^##\s+Harvested-upstream\b")
_HARVESTED_HEADER_RE = re.compile(r"^##\s+Harvested\s+—")
_SKIPPED_HEADER_RE = re.compile(r"^##\s+Skipped\b")
_OTHER_HEADER_RE = re.compile(r"^##\s+")

_PRUNED_DATE_RE = re.compile(r"pruned\s+(\d{4}-\d{2}-\d{2})")
_RUN_TOKEN_RE = re.compile(r"\(run\s+([^)\s]+)\)")

_ARROW_ROW_RE = re.compile(r"^(?P<left>\S+)\s+→\s+(?P<right>.+)$")
_HASH_ROW_RE = re.compile(r"^(?P<left>\S+)\s*#\s*(?P<reason>.+)$")


@dataclass(frozen=True)
class MigratedRow:
    """One row folded into the canonical log."""

    path: str
    disposition: str
    fate: str
    run_id: str


@dataclass(frozen=True)
class WikiLogMigrateResult:
    """Outcome of one `migrate_wiki_log` invocation."""

    canonical_log_path: str
    wiki_log_path: str
    backup_path: str
    rows_migrated: int
    rows: list[MigratedRow] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "canonical_log_path": self.canonical_log_path,
            "wiki_log_path": self.wiki_log_path,
            "backup_path": self.backup_path,
            "rows_migrated": self.rows_migrated,
            "rows": [
                {
                    "path": r.path,
                    "disposition": r.disposition,
                    "fate": r.fate,
                    "run_id": r.run_id,
                }
                for r in self.rows
            ],
        }


class AlreadyMigratedError(RuntimeError):
    """Raised when `migrate_wiki_log` is invoked against an already-tombstoned file.

    Invariant 1: running the migration twice must not double-append rows to the
    canonical log — refusing outright is the fail-loud response."""


class MalformedWikiLogError(RuntimeError):
    """Raised when a line inside a recognized section matches neither the arrow-row
    nor the hash-row shape — this ledger has no "recognized-and-intentionally-skipped"
    category (unlike DR-053's pipe-table), so an unrecognized line is a hard stop
    rather than a silent drop."""


def is_already_tombstoned(text: str) -> bool:
    """Return True if `text` already begins with the tombstone marker line."""
    return text.lstrip().startswith(TOMBSTONE_MARKER)


def render_tombstone(canonical_log_relpath: str = "state/distillation-log.md") -> str:
    """Render the one-line tombstone body (plus trailing newline) that replaces the
    retired wiki-log file's content."""
    return (
        f"{TOMBSTONE_MARKER} — see `{canonical_log_relpath}` for all harvest history.\n"
    )


def _extract_run_id(header_line: str) -> str:
    """Pull a batching key out of a section header: a `pruned <date>` token, a
    `(run <id>)` token, or the fallback constant if neither is present."""
    match = _PRUNED_DATE_RE.search(header_line)
    if match:
        return match.group(1)
    match = _RUN_TOKEN_RE.search(header_line)
    if match:
        return match.group(1)
    return FALLBACK_RUN_ID


def _parse_wiki_log(text: str) -> list[MigratedRow]:
    """Parse the three recognized section shapes into MigratedRow entries.

    Raises MalformedWikiLogError if a non-blank, non-comment line inside a
    recognized section matches neither row shape.
    """
    rows: list[MigratedRow] = []
    section: str | None = None
    run_id = FALLBACK_RUN_ID

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()

        if _HARVESTED_UPSTREAM_HEADER_RE.match(line):
            section = "harvested-upstream"
            run_id = _extract_run_id(line)
            continue
        if _HARVESTED_HEADER_RE.match(line):
            section = "harvested"
            run_id = _extract_run_id(line)
            continue
        if _SKIPPED_HEADER_RE.match(line):
            section = "skipped"
            run_id = _extract_run_id(line)
            continue
        if _OTHER_HEADER_RE.match(line):
            # An unrecognized "## " section (e.g. the file's own H1/intro) — not a
            # data section; subsequent lines belong to no recognized cohort.
            section = None
            continue

        if not line:
            continue
        if line.startswith(">") or line.startswith("#"):
            # blockquote / markdown-comment prose lines (the intro note) — never data.
            continue
        if section is None:
            continue

        if section == "harvested-upstream":
            match = _ARROW_ROW_RE.match(line)
            if not match:
                raise MalformedWikiLogError(
                    f"wiki_log_migrate: line {line_no}: expected an arrow row in the "
                    f"Harvested-upstream section, got {raw_line!r}"
                )
            rows.append(
                MigratedRow(
                    path=match.group("left"),
                    disposition="DISTILLED",
                    fate=(
                        f"reclaimed-upstream: {match.group('right')} "
                        "(migrated from docs/wiki/.distill-log.md)"
                    ),
                    run_id=run_id,
                )
            )
            continue

        if section == "harvested":
            match = _ARROW_ROW_RE.match(line)
            if not match:
                raise MalformedWikiLogError(
                    f"wiki_log_migrate: line {line_no}: expected an arrow row in a "
                    f"Harvested section, got {raw_line!r}"
                )
            rows.append(
                MigratedRow(
                    path=match.group("left"),
                    disposition="DISTILLED",
                    fate=(
                        f"harvested into {match.group('right')} "
                        "(migrated from docs/wiki/.distill-log.md)"
                    ),
                    run_id=run_id,
                )
            )
            continue

        if section == "skipped":
            match = _HASH_ROW_RE.match(line)
            if not match:
                raise MalformedWikiLogError(
                    f"wiki_log_migrate: line {line_no}: expected a '<path>  # <reason>' "
                    f"row in the Skipped section, got {raw_line!r}"
                )
            rows.append(
                MigratedRow(
                    path=match.group("left"),
                    disposition="SKIP",
                    fate=(
                        f"{match.group('reason').strip()} "
                        "(migrated from docs/wiki/.distill-log.md)"
                    ),
                    run_id=run_id,
                )
            )
            continue

    return rows


def migrate_wiki_log(wiki_log_path: Path, canonical_log_path: Path) -> WikiLogMigrateResult:
    """Fold every row of `wiki_log_path` into `canonical_log_path`, then replace
    `wiki_log_path` with a one-line tombstone.

    Steps:
      1. Refuse (AlreadyMigratedError) if `wiki_log_path` is already tombstoned.
      2. Back up the original to `<wiki_log_path>.legacy-backup` BEFORE writing.
         Refuses (FileExistsError) if that sibling already exists.
      3. Parse all three recognized sections (Harvested-upstream, Harvested,
         Skipped) into MigratedRow entries — MalformedWikiLogError on any
         unrecognized line inside a recognized section (no silent drop).
      4. Append all rows to `canonical_log_path` in ONE `log_append.append_rows`
         call (validate-all-then-write-once; nothing written if any row is bad).
      5. Overwrite `wiki_log_path` with the one-line tombstone, atomically
         (temp file + os.replace).

    Raises FileNotFoundError if `wiki_log_path` does not exist.
    """
    if not wiki_log_path.exists():
        raise FileNotFoundError(
            f"migrate_wiki_log: wiki_log_path does not exist: {wiki_log_path}"
        )

    original_text = wiki_log_path.read_text(encoding="utf-8")

    if is_already_tombstoned(original_text):
        raise AlreadyMigratedError(
            f"migrate_wiki_log: {wiki_log_path} is already tombstoned — refusing to "
            "re-run (rows would double-append to the canonical log)."
        )

    backup_path = wiki_log_path.with_name(wiki_log_path.name + ".legacy-backup")
    if backup_path.exists():
        raise FileExistsError(
            f"migrate_wiki_log: refusing to overwrite existing backup at "
            f"{backup_path} (a prior migration attempt may have left it — resolve "
            "manually before re-running; never destroy the preserved original)."
        )

    rows = _parse_wiki_log(original_text)
    if not rows:
        raise MalformedWikiLogError(
            f"migrate_wiki_log: {wiki_log_path} yielded zero recognized rows — "
            "refusing to tombstone a file with nothing migrated."
        )

    shutil.copy2(wiki_log_path, backup_path)

    append_rows(
        canonical_log_path,
        [
            {
                "path": r.path,
                "disposition": r.disposition,
                "fate": r.fate,
                "run_id": r.run_id,
            }
            for r in rows
        ],
    )

    tombstone_text = render_tombstone()
    tmp_path = wiki_log_path.with_name(wiki_log_path.name + ".tmp")
    tmp_path.write_text(tombstone_text, encoding="utf-8", newline="\n")
    os.replace(tmp_path, wiki_log_path)

    return WikiLogMigrateResult(
        canonical_log_path=str(canonical_log_path),
        wiki_log_path=str(wiki_log_path),
        backup_path=str(backup_path),
        rows_migrated=len(rows),
        rows=rows,
    )
