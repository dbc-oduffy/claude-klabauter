"""
coordinator_core.distill.log_normalize — one-time legacy-log NORMALIZER (C8).

Purpose: migrate a repo's legacy pipe-table distillation log (columns
`date | action | path | last_sha | belongs_to_spec | reason`) to the DoE C1 canonical
schema (`## Run <run-id>` headers, rows `- <path> -> <disposition>, <fate> (run: <run-id>)`)
so no repo carries a legacy shape going forward. Run exactly once per repo.

Disposition mapping (DoE contract §7, binding on this surface — DR-053, exact-string
match on `action`, NO regex / NO case-fold; the enumeration below is the entire
recognized set):
    ARCHIVED         -> DISTILLED   (keyed on legacy `path`)
    DELETED          -> EPHEMERAL   (keyed on legacy `path`)
    DELETE           -> EPHEMERAL   (keyed on legacy `path`)
    distill-harvest  -> DISTILLED   (keyed on legacy `belongs_to_spec`, NOT `path` —
                                      see the field-remap note below)
    DELETE-GROUP     -> skipped, recognized-and-intentional (bulk-deletion glob, no
                         per-spec disposition)
    dr-create / wiki-update / judgment-create / distill-run
                     -> skipped, recognized-and-intentional (distillation output/event
                         rows, not spec dispositions)
A legacy row whose `action` is none of the 7 tokens above is NOT silently mapped to a
default disposition and is NOT treated as "recognized-and-skipped" — it is recorded
under `skipped` with an "unrecognized action" reason, never dropped from the row count.
§7's fail-loud discipline depends on this distinction: a permissive/case-folding matcher
would risk silently swallowing a future action whose name merely resembles a known one.

Field mapping (DoE contract §7):
    legacy `path`            -> canonical `<path>`, for every action EXCEPT distill-harvest
    legacy `belongs_to_spec` -> canonical `<path>`, for distill-harvest ONLY (DR-053: the
                                 harvest-debt reader keys on specs-dir-relative paths;
                                 keying a distill-harvest row on the wiki-target `path`
                                 column instead would silently produce 0 matches against
                                 the specs corpus)
    legacy `reason`          -> canonical `<fate>` (free text); for distill-harvest, the
                                 wiki target (legacy `path` column) is folded into `<fate>`
                                 so it is not lost when `<path>` is remapped to
                                 `belongs_to_spec`
    legacy `date`            -> the enclosing `## Run <run-id>` grouping

New §7 invariant (fail-loud, enforced by `normalize_log`):
    rows_migrated + rows_skipped == total legacy data-row count
No legacy data row may be left unaccounted for on either side of the ledger. This is
asserted at the end of `normalize_log` — a discrepancy raises `RuntimeError` rather than
returning a `NormalizeResult` whose counts silently don't reconcile.

Run-id grouping choice (DR-047 HOW-is-non-binding — claude-klabauter's call, documented here):
    ONE RUN PER UNIQUE `date` VALUE, not a single synthetic run spanning the whole file.
    The legacy log's `date` column already partitions rows into meaningful batches (each
    date is one historical distill/archive pass); collapsing all rows under one synthetic
    run-id would discard that batching information for zero benefit, and downstream
    consumers (harvest-debt, memo-triage) key off `## Run <id>` boundaries to reason about
    "this run's" rows. The run-id rendered is the literal date string (e.g. `2026-05-06`),
    which is already a legal `\\S+` token for `_RUN_HEADER_RE` / the row's `(run: <id>)`
    suffix.

Invariants (fail-loud, all contract-mandated):
  1. Run exactly once per repo — refuses to re-run against an already-canonical log
     (detected via the `# Columns: run | path | disposition | fate` header, OR a
     successful `parse_distillation_log` yielding >=1 row).
  2. Preserves the original — the legacy file is backed up to `backup_path` BEFORE the
     canonical file is written (no data-loss one-way door). The backup itself refuses to
     clobber a pre-existing `backup_path` sibling — a second attempt against a stale
     `.legacy-backup` fails loud (`FileExistsError`) rather than silently destroying the
     first run's only remaining copy of the true original.
  3. An unrecognized `action` value is recorded under `skipped` with a reason, never
     silently mapped to a default disposition and never dropped from the row count.
  4. Every written canonical row is round-trip-validated (re-parsed via
     `_common.parse_distillation_log` and compared field-for-field to the source values)
     before being counted as migrated. A legacy `path`/`run_id` cell containing embedded
     whitespace cannot survive `_common`'s non-whitespace-anchored grammar — such a row is
     routed to `skipped` with a reason instead of being written-but-miscounted (contract
     §7: "every written canonical row MUST validate/round-trip").
  5. The canonical file is written atomically (temp file + `os.replace`) so a mid-write
     crash can never leave `log_path` truncated or partially written.

Negative-spec: this module does not run itself against any live repo log as a side effect
of import — invocation is always explicit, via `normalize_log` or the `bin/` CLI. It does
not delete the legacy file; the original is preserved in-place at `backup_path` (a sibling
copy, never overwritten if already present), and the canonical file is written to
`log_path` only after that backup succeeds.

Spec backlink: pln-distill-ceremony-mechanical-su-1bcb38 § C8;
DoE-claude/docs/contracts/distill-engine-scripts.md § 7 (binding I/O contract).

Arrow-dialect migration (2026-08-06) — `normalize_arrow_dialects_log`
----------------------------------------------------------------------
A second, independent normalizer in this module for a THIRD non-canonical input shape,
distinct from the legacy pipe-table format above: a log that is ALREADY under real
`## Run <run-id>` headers, where some rows parse as canonical and others are arrow-edged
near-misses that `_common._ROW_RE` rejects. Source: inbound memo
`cross-repo/inbox/2026-08-06-doe-claude-em-distill-log-correction-the-defect-is-ours.md`
(retracting an earlier, wrong ask to loosen `_RUN_HEADER_RE`/`_ROW_RE` — those canonical
matchers are correct and are NOT modified by this module; a non-conforming log is fixed by
migrating its rows, never by loosening the reader).

Three recognized arrow-edged dialects (`ARROW_DIALECT_DISPOSITION_MAP`):
    - <path> -> DISTILLED (harvested; ...)   no comma before fate, no (run: ...) tail
    - <path> -> DELETED ...                  DELETED is not in `_common.DISPOSITIONS`
    - <path> -> deleted ...                  same, lowercase

`DELETED`/`deleted` -> EPHEMERAL, not a contradiction with `harvest_debt`: both arrow
dialects map to canonical `EPHEMERAL`, following the same DR-053 precedent this module's
pipe-table `ACTION_DISPOSITION_MAP` already applies to legacy `DELETED`/`DELETE`. This
LOOKS like it disagrees with `coordinator_core.distill.harvest_debt.HARVESTED_DISPOSITIONS`,
which counts lowercase `deleted` as *harvested* — it does not disagree, because the two
answer different questions. `HARVESTED_DISPOSITIONS` is a reader-side compatibility shim
for un-normalized ACTION-TABLE logs: counting a deleted path as harvested there keeps the
warn-ratio denominator honest, because a deleted file is gone from `archive/specs` and so
can never be counted as debt. This module answers a different question — what canonical
disposition does this row BECOME — and for that direction DR-053 binds, so `EPHEMERAL` is
what gets written. Post-normalization, the reader shim no longer applies to these rows in
any case: debt is cohort-minus-harvested, and a deleted file was never in the cohort to
begin with, so mapping it to `EPHEMERAL` here cannot resurrect debt downstream.

Row headers carrying trailing prose (one repo's real four `## Run` headers include a
description, one with a parenthesised date before an em-dash) are NEVER rewritten by
`normalize_arrow_dialects_log` — a local, more permissive header regex
(`_ARROW_RUN_HEADER_RE`) is used ONLY to associate a row with its enclosing run_id for
rendering; the header LINE ITSELF is always copied to output byte-for-byte. Unlike the
legacy pipe-table normalizer above (which regroups all migrated rows under synthesized
run blocks), this normalizer rewrites in place, line-by-line: every non-candidate line
(headers, comments, blanks, already-canonical rows, and any row this normalizer cannot
confidently migrate) is preserved verbatim at its original position — never dropped,
never reordered — and only a successfully-migrated arrow-dialect row's own line is
replaced with its rendered canonical form.

Same accounting invariant, reinterpreted for this shape: `rows_migrated + rows_skipped ==`
the count of candidate data-row lines (`- <path> -> ...`) in the file. "Migrated" here
covers BOTH a row rewritten from an arrow dialect AND an already-canonical row left
unchanged (both are present, accounted-for, canonical output); "skipped" covers a
candidate row occurring before any `## Run` header, one whose dialect this module does not
recognize, or one whose rendered form fails `_common.parse_distillation_log` round-trip —
each routed to `skipped` with a reason, never dropped from the count.

Hard constraint: this normalizer only ever operates on the single `log_path` its caller
passes in-process — it has no cross-repo reach and never opens, walks, or writes any path
outside that one file (plus its own `.arrow-dialect-backup` sibling). A sibling repo's log
is normalized by that repo invoking this tool against its own `log_path`, never by this
tool reaching into another repo's tree.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from coordinator_core.distill._common import _ROW_RE, parse_distillation_log
from coordinator_core.distill.log_append import render_row

__all__ = [
    "CANONICAL_HEADER_MARKER",
    "ACTION_DISPOSITION_MAP",
    "RECOGNIZED_SKIP_ACTIONS",
    "ARROW_DIALECT_DISPOSITION_MAP",
    "NormalizeResult",
    "AlreadyCanonicalError",
    "NotLegacyShapedError",
    "NotArrowShapedError",
    "NoArrowDialectRowsError",
    "is_already_canonical",
    "normalize_log",
    "normalize_arrow_dialects_log",
]


CANONICAL_HEADER_MARKER = "# Columns: run | path | disposition | fate"
"""Presence of this literal header line marks a log as already-canonical."""

ACTION_DISPOSITION_MAP = {
    "ARCHIVED": "DISTILLED",
    "DELETED": "EPHEMERAL",
    "DELETE": "EPHEMERAL",
    "distill-harvest": "DISTILLED",
}
"""Legacy `action` -> canonical `disposition`, DoE-blessed (contract §7, binding,
DR-053). Exact-string match only — deliberately NOT case-folded, so `distill-harvest`
(lowercase, hyphenated) and `ARCHIVED`/`DELETED`/`DELETE` (uppercase) coexist as
distinct literal keys rather than being normalized to one case. Any `action` value not
present as a key here is NOT silently mapped — see `normalize_log`'s skip handling,
which further distinguishes "recognized-and-intentionally-skipped"
(`RECOGNIZED_SKIP_ACTIONS`) from a genuinely unrecognized action."""

RECOGNIZED_SKIP_ACTIONS: dict[str, str] = {
    "DELETE-GROUP": "bulk-deletion glob, no per-spec disposition (DR-053)",
    "dr-create": "distillation output/event row, not a spec disposition — dropped per DR-053",
    "wiki-update": "distillation output/event row, not a spec disposition — dropped per DR-053",
    "judgment-create": "distillation output/event row, not a spec disposition — dropped per DR-053",
    "distill-run": "distillation output/event row, not a spec disposition — dropped per DR-053",
}
"""Legacy `action` values that are DR-053-recognized but intentionally carry no
canonical disposition — routed to `skipped` with the reason given here, NOT the
"unrecognized action" fallback reason. Distinguishing this set from a truly-unknown
action preserves §7's fail-loud guarantee: only a genuinely unrecognized action (none
of the 7 DR-053 tokens) falls through to "unrecognized action '<x>'"."""

_LEGACY_ROW_RE = re.compile(
    r"^\|\s*(?P<date>[^|]*?)\s*\|\s*(?P<action>[^|]*?)\s*\|\s*(?P<path>[^|]*?)\s*\|"
    r"\s*(?P<last_sha>[^|]*?)\s*\|\s*(?P<belongs_to_spec>[^|]*?)\s*\|\s*(?P<reason>[^|]*?)\s*\|\s*$"
)
_LEGACY_HEADER_OR_SEPARATOR_RE = re.compile(
    r"^\|\s*-{2,}.*\|\s*$|^\|\s*date\s*\|\s*action\s*\|", re.IGNORECASE
)


@dataclass(frozen=True)
class SkippedRow:
    """One legacy row that could not be mapped to a canonical disposition."""

    line: int
    reason: str


@dataclass(frozen=True)
class NormalizeResult:
    """Outcome of one `normalize_log` invocation — matches the contract §7 JSON shape."""

    log_path: str
    rows_migrated: int
    rows_skipped: int
    skipped: list[SkippedRow] = field(default_factory=list)
    backup_path: str = ""
    rows_rewritten: int | None = None
    rows_already_canonical: int | None = None

    # Review: review-integrator — `rows_migrated` conflates "rows actually rewritten
    # from a non-canonical dialect" with "rows already canonical, left untouched"
    # (both were counted into one counter per the module docstring's own "'Migrated'
    # here covers BOTH..." admission). These two optional fields split the ledger so a
    # consumer can distinguish the two without reading that disambiguation first.
    # `rows_migrated` is left as the sum for backward compatibility with existing
    # callers/tests; `normalize_log` (the legacy pipe-table normalizer) never has an
    # "already canonical, left untouched" case, so both fields are None there.

    def to_dict(self) -> dict:
        """Render the exact contract §7 JSON shape.

        # Review: code-reviewer (Finding 6) — the field order below matches the spec's
        # written key order for readability, but that is an insertion-order incidental
        # of Python's dict/json.dump behavior, not a JSON contract guarantee (JSON objects
        # are unordered by spec). A consumer must key-access, never position-access,
        # this CLI's stdout.
        """
        result = {
            "log_path": self.log_path,
            "rows_migrated": self.rows_migrated,
            "rows_skipped": self.rows_skipped,
            "skipped": [{"line": s.line, "reason": s.reason} for s in self.skipped],
            "backup_path": self.backup_path,
        }
        if self.rows_rewritten is not None:
            result["rows_rewritten"] = self.rows_rewritten
        if self.rows_already_canonical is not None:
            result["rows_already_canonical"] = self.rows_already_canonical
        return result


class AlreadyCanonicalError(RuntimeError):
    """Raised when `normalize_log` is invoked against a log already in canonical shape.

    Contract §7 invariant: running the migration twice against an already-canonical file
    must not corrupt or duplicate rows — refusing outright is the fail-loud response."""


class NotLegacyShapedError(RuntimeError):
    """Raised when `log_path` is neither already-canonical nor legacy-pipe-table-shaped.

    # Review: code-reviewer (Finding 4) — `is_already_canonical` returning False does not
    # imply "safe to run the legacy parse": a canonical-shaped file whose every row happens
    # to be unparseable (0 well-formed rows, e.g. a corrupted canonical file) would also
    # return False from `is_already_canonical`, and since none of its lines start with `|`,
    # `_parse_legacy_rows` would find zero legacy rows AND zero malformed-row skips too —
    # silently writing an empty canonical shell over the unrecognized content with no error
    # and no skip-reason. Raised instead when a file has no pipe-delimited lines at all (not
    # "legacy with zero valid rows" — "not legacy-shaped in the first place")."""


class NotArrowShapedError(RuntimeError):
    """Raised by `normalize_arrow_dialects_log` when `log_path` has no `## Run <id>`
    header at all — this normalizer only ever operates on a file already grouped under
    real run headers (the arrow-dialect input shape by definition), never on a bare
    pipe-table legacy file (use `normalize_log` for that) or unrecognized content."""


class NoArrowDialectRowsError(RuntimeError):
    """Raised by `normalize_arrow_dialects_log` when `log_path` has `## Run <id>` headers
    but not a single candidate row matches one of the three recognized arrow dialects —
    the run-once posture's refusal signal for this normalizer (mirrors
    `AlreadyCanonicalError` for the pipe-table normalizer): nothing to migrate, refuse
    rather than write a no-op backup+rewrite."""


def is_already_canonical(text: str) -> bool:
    """Return True if `text` is already in C1 canonical shape.

    Detected via either signal named in contract §7's invariant:
      (a) presence of the literal `# Columns: run | path | disposition | fate` header, or
      (b) a successful `parse_distillation_log` parse yielding >=1 row.
    """
    if CANONICAL_HEADER_MARKER in text:
        return True
    return len(parse_distillation_log(text)) >= 1


def _parse_legacy_rows(text: str) -> tuple[list[tuple[int, dict]], list[SkippedRow]]:
    """Parse the legacy pipe-table body into (line_no, fields) pairs plus malformed-row
    skips. Header/separator lines are recognized and excluded (not treated as data rows,
    not counted as skipped)."""
    parsed: list[tuple[int, dict]] = []
    malformed: list[SkippedRow] = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith("|"):
            continue
        if _LEGACY_HEADER_OR_SEPARATOR_RE.match(line):
            continue
        match = _LEGACY_ROW_RE.match(line)
        if not match:
            malformed.append(
                SkippedRow(line=line_no, reason=f"malformed legacy row: {line!r}")
            )
            continue
        parsed.append((line_no, match.groupdict()))
    return parsed, malformed


def _row_round_trips(row_text: str, run_id: str, path: str, disposition: str, reason: str) -> bool:
    """Return True if a single rendered canonical row, embedded under its own
    `## Run <run_id>` header, re-parses via `_common.parse_distillation_log` back to
    the exact (path, disposition, fate, run_id) values it was rendered from.

    # Review: code-reviewer (Finding 1) — a legacy `path`/`run_id` cell can contain
    # embedded whitespace (free-text pipe-table cells, not validated \\S+ tokens).
    # `render_row` only checks non-emptiness, never the `\\S+` shape `_common`'s
    # `_RUN_HEADER_RE`/`_ROW_RE` grammar requires, so such a row would render, get
    # counted as migrated, and then silently fail to parse back — vanishing from the
    # canonical file's queryable surface with `rows_migrated` overstating success. This
    # helper closes that gap by actually round-tripping each row through the real
    # parser (not just checking `\\s` absence) before it is ever counted or written,
    # per contract §7's binding round-trip invariant.
    """
    probe_text = f"## Run {run_id}\n{row_text}\n"
    parsed = parse_distillation_log(probe_text)
    if len(parsed) != 1:
        return False
    row = parsed[0]
    return (
        row.run_id == run_id
        and row.path == path
        and row.disposition == disposition
        and row.fate == reason
    )


def normalize_log(log_path: Path) -> NormalizeResult:
    """Migrate the legacy pipe-table log at `log_path` to C1 canonical shape in place.

    Steps:
      1. Refuse (raise AlreadyCanonicalError) if `log_path` is already canonical.
         Raise NotLegacyShapedError if the file is neither canonical nor legacy-shaped
         (no pipe-delimited lines at all) — see Finding 4's empty-shell hazard.
      2. Back up the original legacy file to `<log_path>.legacy-backup` BEFORE writing.
         Refuses (FileExistsError) if a `.legacy-backup` sibling already exists — never
         clobbers a prior run's only remaining copy of the true original.
      3. Parse legacy rows; map action -> disposition per `ACTION_DISPOSITION_MAP`
         (DR-053's 4 disposition-bearing tokens); route `RECOGNIZED_SKIP_ACTIONS`
         tokens (DELETE-GROUP + the 4 event-row actions) to `skipped` with their
         DR-053 reason; any other `action` (or a malformed row) goes to `skipped`
         with an "unrecognized action" reason.
      4. Render each row via `log_append.render_row` and round-trip-validate it
         (`_row_round_trips`) BEFORE counting it as migrated — a row whose rendered
         form cannot be re-parsed back to its source values (e.g. embedded whitespace
         in a legacy `path`/`date` cell) goes to `skipped` with a reason instead of
         being written-but-miscounted. `distill-harvest` rows are keyed on
         `belongs_to_spec` (not `path`) for this step — see the field-remap note
         in the module docstring.
      5. Write the canonical file to `log_path` atomically (temp file + `os.replace`),
         overwriting the legacy content (the original is safe at `backup_path`).
      6. Assert the §7 accounting invariant (`rows_migrated + rows_skipped ==` the
         total legacy data-row count) before returning — a discrepancy raises
         `RuntimeError` rather than a `NormalizeResult` whose counts silently don't
         reconcile.

    Raises AlreadyCanonicalError without touching the file if `log_path` is already
    canonical. Raises NotLegacyShapedError without touching the file if `log_path` is
    neither canonical nor legacy-pipe-table-shaped. Raises FileNotFoundError if
    `log_path` does not exist. Raises FileExistsError if `backup_path` already exists.
    """
    if not log_path.exists():
        raise FileNotFoundError(f"normalize_log: log_path does not exist: {log_path}")

    original_text = log_path.read_text(encoding="utf-8")

    if is_already_canonical(original_text):
        raise AlreadyCanonicalError(
            f"normalize_log: {log_path} is already canonical — refusing to re-run "
            "(contract §7 invariant: run exactly once per repo)."
        )

    parsed_rows, malformed_skips = _parse_legacy_rows(original_text)

    if not parsed_rows and not malformed_skips:
        raise NotLegacyShapedError(
            f"normalize_log: {log_path} is neither already-canonical nor "
            "legacy-pipe-table-shaped (no pipe-delimited lines found) — refusing to "
            "write an empty canonical shell over unrecognized content."
        )

    backup_path = log_path.with_name(log_path.name + ".legacy-backup")
    if backup_path.exists():
        raise FileExistsError(
            f"normalize_log: refusing to overwrite existing backup at {backup_path} "
            "(a prior migration attempt may have left it — resolve manually before "
            "re-running; contract §7 invariant: never destroy the preserved original)."
        )
    shutil.copy2(log_path, backup_path)

    skipped: list[SkippedRow] = list(malformed_skips)
    migrated_by_run: dict[str, list[str]] = {}
    rows_migrated = 0

    for line_no, fields in parsed_rows:
        action = fields["action"].strip()

        if action in RECOGNIZED_SKIP_ACTIONS:
            skipped.append(
                SkippedRow(line=line_no, reason=RECOGNIZED_SKIP_ACTIONS[action])
            )
            continue

        disposition = ACTION_DISPOSITION_MAP.get(action)
        if disposition is None:
            skipped.append(
                SkippedRow(
                    line=line_no,
                    reason=f"unrecognized action {fields['action']!r}",
                )
            )
            continue

        run_id = fields["date"].strip()
        reason = fields["reason"].strip()

        # distill-harvest keys canonical <path> on belongs_to_spec (the
        # archive/specs/... source), NOT the legacy `path` column (which holds the
        # wiki target) — DR-053. The wiki target is folded into <fate> so it is not
        # lost by the remap.
        if action == "distill-harvest":
            path = fields["belongs_to_spec"].strip()
            wiki_target = fields["path"].strip()
            if wiki_target:
                reason = f"{reason} (wiki target: {wiki_target})" if reason else f"wiki target: {wiki_target}"
        else:
            path = fields["path"].strip()

        if not run_id or not path or not reason:
            skipped.append(
                SkippedRow(
                    line=line_no,
                    reason=(
                        "missing required field(s) after trim "
                        f"(date={fields['date']!r}, path={fields['path']!r}, "
                        f"belongs_to_spec={fields['belongs_to_spec']!r}, reason={fields['reason']!r})"
                    ),
                )
            )
            continue

        row_text = render_row(path, disposition, reason, run_id)

        if not _row_round_trips(row_text, run_id, path, disposition, reason):
            skipped.append(
                SkippedRow(
                    line=line_no,
                    reason=(
                        "rendered canonical row does not round-trip through "
                        "parse_distillation_log — legacy path/date cell likely "
                        f"contains embedded whitespace (path={path!r}, run_id={run_id!r})"
                    ),
                )
            )
            continue

        migrated_by_run.setdefault(run_id, []).append(row_text)
        rows_migrated += 1

    skipped.sort(key=lambda s: s.line)

    total_legacy_data_rows = len(parsed_rows) + len(malformed_skips)
    if rows_migrated + len(skipped) != total_legacy_data_rows:
        raise RuntimeError(
            "normalize_log: §7 accounting invariant violated — "
            f"rows_migrated ({rows_migrated}) + rows_skipped ({len(skipped)}) != "
            f"total legacy data-row count ({total_legacy_data_rows}) for {log_path}. "
            "No legacy row may be dropped or double-counted; refusing to return a "
            "NormalizeResult whose ledger does not reconcile."
        )

    canonical_lines = [
        "# Distillation Log (canonical)",
        CANONICAL_HEADER_MARKER,
        "",
    ]
    for run_id in sorted(migrated_by_run):
        canonical_lines.append(f"## Run {run_id}")
        canonical_lines.extend(migrated_by_run[run_id])
    canonical_text = "\n".join(canonical_lines) + "\n"

    # Review: code-reviewer (Finding 3) — atomic write (temp file + os.replace) so a
    # process crash mid-write can never leave log_path truncated/partially written;
    # the backup (already on disk, guarded above) remains the recovery path either way.
    tmp_path = log_path.with_name(log_path.name + ".tmp")
    tmp_path.write_text(canonical_text, encoding="utf-8", newline="\n")
    os.replace(tmp_path, log_path)

    return NormalizeResult(
        log_path=str(log_path),
        rows_migrated=rows_migrated,
        rows_skipped=len(skipped),
        skipped=skipped,
        backup_path=str(backup_path),
    )


# ---------------------------------------------------------------------------
# Arrow-dialect normalizer — see module docstring § "Arrow-dialect migration"
# ---------------------------------------------------------------------------

ARROW_DIALECT_DISPOSITION_MAP = {
    "DISTILLED": "DISTILLED",
    "DELETED": "EPHEMERAL",
    "deleted": "EPHEMERAL",
}
"""Arrow-dialect token -> canonical disposition (DR-053 precedent, exact-string match,
NO case-fold — `DELETED` and `deleted` are distinct literal keys that both happen to
resolve to `EPHEMERAL`, never merged into one case-insensitive lookup). See module
docstring § "DELETED/deleted -> EPHEMERAL, not a contradiction" for why this agrees with
DR-053 while appearing to disagree with `harvest_debt.HARVESTED_DISPOSITIONS`'s unrelated
reader-side treatment of lowercase `deleted`."""

_ARROW_RUN_HEADER_RE = re.compile(r"^##\s+Run\s+(?P<run_id>\S+)(?:\s+.*)?$")
"""Permissive header-line matcher, local to this module only, used solely to associate
arrow-dialect rows with their enclosing run for canonical rendering. Deliberately more
permissive than `_common._RUN_HEADER_RE` (which is binding as DoE's canonical parser and
is NOT modified here, per the retracted `distill-log-parser-discards-every-run` memo) —
it tolerates trailing prose after the run-id token (e.g. `## Run 2026-05-06 (harvest
sweep) — description`) purely so this scanner can locate the enclosing run_id for a row;
header LINES THEMSELVES are always copied to output byte-for-byte, never rewritten, so
any trailing description is preserved losslessly regardless of this regex's permissiveness."""

_CANDIDATE_ARROW_ROW_RE = re.compile(r"^-\s+\S+.*->.*$")
"""Recognizes a line as a distillation-log data-row candidate (leading `- `, a path
token, an arrow) without judging its dialect. Used only to decide which lines are
counted toward the §7-style accounting total for this normalizer — everything else (run
headers, comments, blank lines) passes through untouched and uncounted."""

_ARROW_DIALECT_ROW_RE = re.compile(
    r"^-\s+(?P<path>\S+)\s+->\s+(?P<token>DISTILLED|DELETED|deleted)\b\s*(?P<rest>.*)$"
)
"""Matches one of the three non-canonical arrow-edged dialects this normalizer migrates
(none is a near-miss on the canonical grammar — see module docstring § "Arrow-dialect
migration"):
    - <path> -> DISTILLED (harvested; ...)   no comma before fate, no (run: ...) tail
    - <path> -> DELETED ...                  DELETED is not in `_common.DISPOSITIONS`
    - <path> -> deleted ...                  same, lowercase
`token` distinguishes the three cases case-sensitively (no case-folding). A line already
in canonical shape is matched by `_common._ROW_RE` FIRST by the caller and never reaches
this pattern (its disposition-then-comma grammar cannot match an arrow-dialect line, so
there is no ordering ambiguity between the two)."""


def _arrow_dialect_fate(token: str, rest: str) -> str:
    """Extract the free-text fate from an arrow-dialect row's captured `rest` text.

    `DISTILLED` dialect rows wrap the whole fate in one parenthesized group
    (`(harvested; ...)`) — the parens are stripped, the inner text kept verbatim.
    `DELETED`/`deleted` dialect rows carry no such wrapping; a leading comma (if present,
    e.g. `-> DELETED, some reason`) is stripped so the fate text does not start with a
    stray punctuation mark, but the remaining free text is otherwise kept verbatim — this
    function never invents or drops content, only unwraps the dialect's own punctuation."""
    rest = rest.strip()
    if token == "DISTILLED":
        wrapped = re.match(r"^\((?P<inner>.*)\)$", rest)
        if wrapped:
            return wrapped.group("inner").strip()
        return rest
    return rest.lstrip(",").strip()


def normalize_arrow_dialects_log(log_path: Path) -> NormalizeResult:
    """Migrate the three arrow-edged non-canonical dialects (see module docstring) found
    in an already `## Run`-headered log at `log_path` to canonical rows, in place,
    line-by-line, preserving every other line (headers with trailing prose, comments,
    blanks, already-canonical rows, and any row this normalizer cannot confidently
    migrate) verbatim at its original position.

    Steps:
      1. Raise NotArrowShapedError if `log_path` has no `## Run <id>` header at all
         (per `_ARROW_RUN_HEADER_RE`) — this normalizer only ever operates on a file
         already grouped under real run headers.
      2. Scan every line; classify each candidate row (`_CANDIDATE_ARROW_ROW_RE`) as:
         already-canonical (`_common._ROW_RE` match, left unchanged, counted migrated),
         a recognized arrow dialect (`_ARROW_DIALECT_ROW_RE`, rendered + round-trip
         validated via `render_row`/`_row_round_trips`), or unrecognized/out-of-run/
         round-trip-failed (routed to `skipped` with a reason, line left unchanged).
      3. Raise NoArrowDialectRowsError (before any write) if zero candidate rows match
         a recognized arrow dialect — nothing to migrate, refuse rather than write a
         no-op backup+rewrite (this normalizer's run-once refusal signal).
      4. Back up the original file to `<log_path>.arrow-dialect-backup` BEFORE writing.
         Refuses (FileExistsError) if that backup sibling already exists.
      5. Write the rewritten file to `log_path` atomically (temp file + `os.replace`).
      6. Assert `rows_migrated + rows_skipped ==` the candidate-row-line count before
         returning — a discrepancy raises `RuntimeError` rather than a `NormalizeResult`
         whose counts silently don't reconcile.

    Raises FileNotFoundError if `log_path` does not exist. Raises FileExistsError if
    `<log_path>.arrow-dialect-backup` already exists.
    """
    if not log_path.exists():
        raise FileNotFoundError(
            f"normalize_arrow_dialects_log: log_path does not exist: {log_path}"
        )

    original_text = log_path.read_text(encoding="utf-8")
    lines = original_text.splitlines()

    if not any(_ARROW_RUN_HEADER_RE.match(line) for line in lines):
        raise NotArrowShapedError(
            f"normalize_arrow_dialects_log: {log_path} has no '## Run <id>' header — "
            "not arrow-dialect-shaped (use normalize_log for a bare legacy pipe-table "
            "file, or investigate the file's actual shape)."
        )

    skipped: list[SkippedRow] = []
    rows_migrated = 0
    rows_rewritten = 0
    rows_already_canonical = 0
    total_candidate_rows = 0
    output_lines: list[str] = list(lines)
    current_run_id: str | None = None
    any_arrow_dialect_match = False

    for idx, line in enumerate(lines):
        header_match = _ARROW_RUN_HEADER_RE.match(line)
        if header_match:
            current_run_id = header_match.group("run_id")
            continue

        if not _CANDIDATE_ARROW_ROW_RE.match(line):
            continue

        total_candidate_rows += 1
        line_no = idx + 1

        if current_run_id is None:
            skipped.append(
                SkippedRow(
                    line=line_no,
                    reason="candidate row occurs before any '## Run' header",
                )
            )
            continue

        if _ROW_RE.match(line):
            # Already canonical — left unchanged, counted as accounted-for output.
            rows_migrated += 1
            rows_already_canonical += 1
            continue

        dialect_match = _ARROW_DIALECT_ROW_RE.match(line)
        if not dialect_match:
            skipped.append(
                SkippedRow(
                    line=line_no,
                    reason=f"unrecognized arrow-shaped row: {line!r}",
                )
            )
            continue

        token = dialect_match.group("token")
        path = dialect_match.group("path").strip()
        disposition = ARROW_DIALECT_DISPOSITION_MAP[token]
        fate = _arrow_dialect_fate(token, dialect_match.group("rest"))

        if not path or not fate:
            skipped.append(
                SkippedRow(
                    line=line_no,
                    reason=(
                        "missing required field(s) after trim "
                        f"(path={path!r}, fate={fate!r})"
                    ),
                )
            )
            continue

        row_text = render_row(path, disposition, fate, current_run_id)

        if not _row_round_trips(row_text, current_run_id, path, disposition, fate):
            skipped.append(
                SkippedRow(
                    line=line_no,
                    reason=(
                        "rendered canonical row does not round-trip through "
                        "parse_distillation_log — arrow-dialect path/fate text likely "
                        f"contains embedded whitespace or a stray delimiter (path={path!r})"
                    ),
                )
            )
            continue

        output_lines[idx] = row_text
        rows_migrated += 1
        rows_rewritten += 1
        # Review: review-integrator — only a row that actually survived the
        # round-trip check (i.e. was really rewritten from a recognized arrow
        # dialect) may flip this flag; setting it on regex match alone let a
        # round-trip-failing row's file bypass NoArrowDialectRowsError, write a
        # no-op backup+rewrite, and permanently occupy the backup slot.
        any_arrow_dialect_match = True

    if not any_arrow_dialect_match:
        raise NoArrowDialectRowsError(
            f"normalize_arrow_dialects_log: {log_path} has no candidate row matching a "
            "recognized arrow dialect — refusing to write a no-op backup+rewrite "
            "(run-once posture: nothing to migrate)."
        )

    skipped.sort(key=lambda s: s.line)

    # `total_candidate_rows` is counted independently during the scan (not derived from
    # rows_migrated + len(skipped)) precisely so this assertion can catch a real
    # bookkeeping bug rather than being a tautology.
    if rows_migrated + len(skipped) != total_candidate_rows:
        raise RuntimeError(
            "normalize_arrow_dialects_log: accounting invariant violated — "
            f"rows_migrated ({rows_migrated}) + rows_skipped ({len(skipped)}) != "
            f"candidate-row-line count ({total_candidate_rows}) for {log_path}. "
            "No candidate row may be dropped or double-counted; refusing to return a "
            "NormalizeResult whose ledger does not reconcile."
        )

    backup_path = log_path.with_name(log_path.name + ".arrow-dialect-backup")
    if backup_path.exists():
        raise FileExistsError(
            f"normalize_arrow_dialects_log: refusing to overwrite existing backup at "
            f"{backup_path} (a prior migration attempt may have left it — resolve "
            "manually before re-running; never destroy the preserved original)."
        )
    shutil.copy2(log_path, backup_path)

    new_text = "\n".join(output_lines)
    if original_text.endswith("\n"):
        new_text += "\n"

    tmp_path = log_path.with_name(log_path.name + ".tmp")
    tmp_path.write_text(new_text, encoding="utf-8", newline="\n")
    os.replace(tmp_path, log_path)

    return NormalizeResult(
        log_path=str(log_path),
        rows_migrated=rows_migrated,
        rows_skipped=len(skipped),
        skipped=skipped,
        backup_path=str(backup_path),
        rows_rewritten=rows_rewritten,
        rows_already_canonical=rows_already_canonical,
    )
