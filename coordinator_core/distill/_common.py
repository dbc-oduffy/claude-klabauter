"""
coordinator_core.distill._common — shared barrier module for the distill-ceremony scripts.

Purpose: the 3 independent scripts (ripe-filter, sidecar-sweep, delete-guard) and the
2 coupled scripts (harvest-debt, memo-triage) all import this module. It carries:

  1. The canonical distillation-log PARSER for the DoE canonical format (used by C4/C7's
     `/pickup` stamp + handoff-symmetry work) — NOT the legacy pipe-table format found in
     today's `state/distillation-log.md` (date | action | path | last_sha | belongs_to_spec
     | reason). The canonical format is line-oriented under `## Run <id>` headers:

         ## Run <run-id>
         - <path> -> <disposition>, <fate> (run: <run-id>)

     disposition is one of DISTILLED, PROMOTE, EPHEMERAL, SKIP, PRESERVE. The arrow is the
     ASCII two-character sequence "->", never a unicode arrow glyph.
  2. The active-reference ripgrep guard, scoped to docs/ tasks/ archive/ (sidecar-sweep and
     delete-guard both gate deletions on this before emitting a deletion-manifest row).
     `active_reference_guard_many` is the batched form — one `rg -f <patternfile>` call for
     a whole candidate set instead of one `rg` call per candidate; sidecar-sweep uses it to
     stay off the per-item git-spawn amplification list.
  3. SIDECAR_SUFFIXES — the single named, full-suffix-anchored constant enumerating the
     process-scaffolding sidecar suffixes sidecar-sweep matches against. C8 (DoE's contract)
     is the eventual single owner of this enumeration; this constant is claude-klabauter's half of that
     shared list, not an independently-authored duplicate.
  4. Frontmatter access — re-exports EXACTLY split_frontmatter and read_fm_field from
     coordinator_core.frontmatter.primitives (the read-only surface). The mutation helpers
     (insert_fm_field / replace_fm_field / remove_fm_field / rebuild) are deliberately NOT
     imported here: this is a read-only distill plane, and those helpers raise ValueError on
     block scalars when misapplied to a read path.

Negative-spec: this module performs no writes. It does not call subprocess in a way that
mutates repo state, does not open files for write, and does not maintain a durable store.

Spec backlink: pln-distill-ceremony-mechanical-su-1bcb38 § C0
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from coordinator_core.temp_layout import managed_tempdir
from dataclasses import dataclass
from pathlib import Path

from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter

__all__ = [
    "DISPOSITIONS",
    "SIDECAR_SUFFIXES",
    "PROVENANCE_MARKER_KEYS",
    "DistillLogRow",
    "parse_distillation_log",
    "active_reference_guard",
    "active_reference_guard_many",
    "read_fm_field",
    "split_frontmatter",
]


# ---------------------------------------------------------------------------
# Canonical distillation-log parser
# ---------------------------------------------------------------------------

DISPOSITIONS = frozenset({"DISTILLED", "PROMOTE", "EPHEMERAL", "SKIP", "PRESERVE"})
"""Valid disposition values for a canonical-log row (per the DoE canonical format)."""

_RUN_HEADER_RE = re.compile(r"^##\s+Run\s+(?P<run_id>\S+)\s*$")

# ASCII "->" only — never a unicode arrow (→ U+2192). The row shape is:
#   - <path> -> <disposition>, <fate> (run: <run-id>)
#
# The fate capture group was previously
# `[^()]+?` (parenthesis-excluding), which meant a writer-emitted row whose free-text
# `fate` legitimately contained a parenthetical aside (e.g. "folded into wiki (see
# also X)") would round-trip to disk but then silently fail to parse back — the row
# would vanish from parse_distillation_log with no error, surfacing downstream as a
# false "never harvested" harvest-debt report for a spec that was actually DISTILLED.
# Fixed by anchoring the run-id capture at the END of the line (greedy `.+` for fate,
# backtracking to the last ` (run: <id>)` occurrence) rather than forbidding parens
# in fate — fate is free text and must never be constrained by the log GRAMMAR.
_ROW_RE = re.compile(
    r"^-\s+(?P<path>\S+)\s+->\s+(?P<disposition>[A-Z]+),\s*(?P<fate>.+?)\s*"
    r"\(run:\s*(?P<row_run_id>\S+)\)\s*$"
)


@dataclass(frozen=True)
class DistillLogRow:
    """One parsed row of the canonical distillation log.

    run_id is taken from the enclosing ``## Run <id>`` header, not re-derived from the
    row's own ``(run: <id>)`` suffix — the two are expected to agree; callers that need to
    detect drift between header-run and row-run should compare `run_id` against the row's
    raw text themselves (this parser is deliberately silent on that mismatch, not fail-loud —
    see C4/C7 for consumer-side handling)."""

    run_id: str
    path: str
    disposition: str
    fate: str


def parse_distillation_log(text: str) -> list[DistillLogRow]:
    """Parse the canonical distillation-log format into a list of DistillLogRow.

    Rows are grouped under ``## Run <id>`` headers; each row line has the shape
    ``- <path> -> <disposition>, <fate> (run: <id>)`` with an ASCII ``->`` arrow. A row
    whose disposition is not in DISPOSITIONS is skipped (not raised) — this parser is a
    read-only scan, not a validator; validation is a consumer concern.

    Rows encountered before any ``## Run`` header are skipped (no enclosing run_id to
    attach).
    """
    rows: list[DistillLogRow] = []
    current_run_id: str | None = None
    for line in text.splitlines():
        header_match = _RUN_HEADER_RE.match(line)
        if header_match:
            current_run_id = header_match.group("run_id")
            continue
        if current_run_id is None:
            continue
        row_match = _ROW_RE.match(line)
        if not row_match:
            continue
        disposition = row_match.group("disposition")
        if disposition not in DISPOSITIONS:
            continue
        rows.append(
            DistillLogRow(
                run_id=current_run_id,
                path=row_match.group("path"),
                disposition=disposition,
                fate=row_match.group("fate").strip(),
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Active-reference ripgrep guard
# ---------------------------------------------------------------------------

ACTIVE_REFERENCE_SCOPE = ("docs", "tasks", "archive", "state/handoffs")
"""Directories (relative to repo_root) the active-reference guard scans.

`state/handoffs` was added (2026-07-23, alongside the delete_guard lineage-
denormalization fix — see `coordinator_core.ops.distill_apply_disposal`
module docstring § Lineage denormalization) as a belt-and-braces widening,
explicitly NOT the fix for that defect: a LIVE handoff under
`state/handoffs/` naming an archived record via `predecessor:` /
`additional_predecessors:` / `origin_handoff:` previously could not protect
that record from delete, because the guard never scanned `state/` at all.

Scoped to `state/handoffs` specifically, NOT all of `state/` — `state/` also
holds high-churn, append-only, non-referencing files (`state/backlog-
snapshots*.jsonl`, ledgers, scratch/) that can mention many candidate paths
incidentally (e.g. a stale path recorded in a JSONL snapshot row) without
that mention being a genuine "still in use" reference. Widening to all of
`state/` risks making a broad class of otherwise-eligible candidates
undeletable on incidental snapshot/ledger hits — `state/handoffs` is the one
subtree whose entire contents are genuine authored cross-references (the
edge fields named above), so it carries the real signal this guard exists to
catch without that false-positive class."""


PROVENANCE_MARKER_KEYS = ("archived_handoff:", "cross_repo_memo:")
"""Frontmatter keys that mark a harvest-provenance TOMBSTONE block — a record of where a
`/distill`-harvested artifact's content went, not a live dependency on it.

DoE owns this vocabulary as a cross-repo contract; claude-klabauter mirrors it here in lockstep,
same ownership split `SIDECAR_SUFFIXES`'s own comment already documents (DoE authors and
evolves the list, the engine mirrors it locally rather than reading it live). Source of
truth: `coordinator/docs/wiki/provenance-markers.md` § "The marker key set (the contract)"
— currently exactly these two keys; `provenance:` and `judgment_provenance:` are named
there as under-consideration, NOT yet part of the enforced set, and must not be added here
until that doc says so.

Back-pointer integrity: `coordinator/docs/wiki/provenance-markers.md` § "The marker key
set (the contract)" was temporarily lost — a distill harvest (DoE `f064affb0`, 2026-08-06)
replaced the page's body with review-trail provenance — and was restored at DoE
`269adc5b9` (2026-08-29) after a memo. Re-verify that section still says what it is cited
for before widening this tuple; a back-pointer to a page that no longer carries the
contract reads as ratification nobody gave.

Each key introduces a YAML list block (`path:`, plus per-key metadata fields) whose entries
record a candidate's own repo-relative path as a harvest tombstone. Without this exclusion,
that tombstone trips `active_reference_guard` against itself and the candidate becomes
permanently undeletable — see `provenance-markers.md` § "The problem this contract closes"
and the originating cross-repo proposal,
`cross-repo/archive/2026-07-23-claude-central-em-distill-active-reference-provenance-exclusion.md`."""

_PROVENANCE_KEY_LINE_RE = re.compile(
    r"^(?:" + "|".join(re.escape(k) for k in PROVENANCE_MARKER_KEYS) + r")[ \t]*$"
)


def _text_excluding_provenance_blocks(text: str) -> str:
    """Return `text` with any recognized provenance-marker block's lines removed from its
    frontmatter, for use as the corpus a "still actively referenced" search runs against.

    A provenance-marker block is a top-level (column-0) frontmatter key line matching one of
    `PROVENANCE_MARKER_KEYS` exactly (trailing whitespace only — an inline scalar value on the
    same key line, e.g. "archived_handoff: []", is NOT recognized as a block start and is left
    in place, ambiguity-blocks-by-default), followed by its indented/blank YAML list body, up
    to (not including) the next column-0 line or the end of the frontmatter.

    No frontmatter (`split_frontmatter` returns None) -> `text` is returned unchanged: there is
    no block to exclude, so every reference in the file (including any bare mention of a
    provenance-shaped key in prose) still counts, exactly as before this exclusion existed.

    Negative-spec: this is a text transform for search purposes only — it returns no verdict
    and performs no writes; it never touches anything outside the frontmatter block, so a
    citation anywhere in the body always survives into the returned text."""
    split = split_frontmatter(text)
    if split is None:
        return text
    out_lines: list[str] = []
    in_block = False
    for line in split.fm_text.split("\n"):
        if in_block:
            if line == "" or line[0] in (" ", "\t"):
                continue
            in_block = False
        if _PROVENANCE_KEY_LINE_RE.match(line):
            in_block = True
            continue
        out_lines.append(line)
    stripped_fm = "\n".join(out_lines)
    return split.preamble + stripped_fm + split.body_with_leading_newline


class RipgrepUnavailableError(RuntimeError):
    """`rg` is not on PATH. The reference guards have no in-process fallback (a corpus walk
    reads every file); a delete path must treat this as "referenced" (ambiguity blocks)."""


def _require_rg(caller: str) -> None:
    if shutil.which("rg") is None:
        raise RipgrepUnavailableError(
            f"{caller}: ripgrep (rg) not on PATH; reference check refused, treat as referenced"
        )


def active_reference_guard(
    needle: str,
    repo_root: Path,
    *,
    scope: tuple[str, ...] = ACTIVE_REFERENCE_SCOPE,
    exclude_provenance_blocks: bool = True,
) -> bool:
    """Return True if `needle` (typically a candidate-for-deletion path or slug) is still
    actively referenced somewhere under `scope` (default: docs/ tasks/ archive/) in `repo_root`.

    `exclude_provenance_blocks=False` turns the exclusion OFF and restores a plain
    "cited anywhere" search. That is not a laxity knob — it is the OTHER reading of the same
    block. A caller asking "is this artifact's content durably captured somewhere?"
    (`delete_guard.check_harvest_provenance`, Guard 7) wants the tombstone to COUNT, because
    the tombstone is its positive proof; a caller asking "does anything still depend on this
    artifact?" (Guard 3) wants it excluded. One block, two opposite readings, by design — so
    the two readings must be two distinct calls, not one shared default. Defaulting to True
    keeps the delete-safety caller safe when a new call site forgets to choose.

    A citation that occurs ONLY inside a recognized provenance-marker block (see
    `PROVENANCE_MARKER_KEYS`) does NOT count as a live reference — that block is the
    candidate's own harvest tombstone, the opposite of a dependency on it. A citation
    anywhere else — prose body, non-provenance frontmatter, or a provenance-shaped key line
    carrying an inline value rather than a block — still counts exactly as before this
    exclusion existed. Conservative-by-construction: a needle cited BOTH inside and outside a
    provenance block still counts as referenced (the outside citation alone is sufficient),
    and any file this function cannot read (I/O error, non-UTF-8 content) is treated as a
    reference (ambiguity blocks, never silently opens a delete hole) rather than skipped.

    Read-only: shells out to ripgrep (`rg`) in fixed-string mode to find candidate files, then
    reads each candidate to distinguish an in-block citation from a live one; performs no
    writes. A deletion-eligible candidate must clear this guard (return False) before a caller
    emits a deletion-manifest row.

    Scope directories that do not exist under repo_root are silently skipped (not an error —
    a fresh checkout or a repo without a tasks/ tree is a legitimate absent-scope state, not
    a guard failure)."""
    existing_scope = [
        str(repo_root / d) for d in scope if (repo_root / d).is_dir()
    ]
    if not existing_scope:
        return False
    _require_rg("active_reference_guard")
    from coordinator_core.win_portability import leaf_spawn_creationflags

    result = subprocess.run(
        ["rg", "--fixed-strings", "--files-with-matches", needle, *existing_scope],
        cwd=repo_root,
        capture_output=True,
        text=True,
        **leaf_spawn_creationflags(),
    )
    # rg exit codes: 0 = match(es) found, 1 = no match, 2 = error.
    if result.returncode == 2:
        raise RuntimeError(
            f"active_reference_guard: ripgrep failed (exit 2): {result.stderr.strip()}"
        )
    if result.returncode == 1:
        return False
    matched_paths = result.stdout.splitlines()
    if not exclude_provenance_blocks:
        return bool(matched_paths)
    return referenced_needles([needle], matched_paths)[needle]


def referenced_needles(needles: list[str], matched_paths: list[str]) -> dict[str, bool]:
    """`{needle: is_live_reference}` from ONE read of each file in `matched_paths` (the files
    a fixed-string search flagged): provenance blocks are stripped once per file and every
    still-undecided needle is tested against it. A needle is True when it occurs outside a
    recognized provenance-marker block; a file that cannot be read makes every needle True
    (ambiguity blocks). Read-only."""
    result = {needle: False for needle in needles}
    pending = set(result)
    for matched_path in matched_paths:
        if not matched_path:
            continue
        try:
            file_text = Path(matched_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return {needle: True for needle in result}
        live_text = _text_excluding_provenance_blocks(file_text)
        for needle in [n for n in pending if n in live_text]:
            result[needle] = True
            pending.discard(needle)
        if not pending:
            break
    return result


def active_reference_guard_many(
    needles: list[str],
    repo_root: Path,
    *,
    scope: tuple[str, ...] = ACTIVE_REFERENCE_SCOPE,
) -> dict[str, bool]:
    """Batched form of `active_reference_guard`: return `{needle: is_referenced}` for every
    needle in `needles`, using ONE `rg` invocation for the whole set (`rg -f <patternfile>`,
    unioning every needle's pattern into a single fixed-strings search) instead of one `rg`
    call per needle. Needle -> file attribution moves into the per-file read this guard
    already performs per matched file (`referenced_needles`) — the union of files any
    needle matched is read once each, then checked against every needle, exactly the same
    ambiguity-blocks-if-unreadable and provenance-block-exclusion semantics as
    `active_reference_guard`.

    A duplicate needle collapses naturally (dict keyed by needle); order of `needles` is not
    preserved in the pattern file since fixed-strings union search is order-independent, but
    the returned dict has one entry per DISTINCT input needle.

    Negative-spec: performs no writes; behavior (per-needle result) is identical to calling
    `active_reference_guard` once per needle at its default `exclude_provenance_blocks=True`
    — this is a process-count optimization only. There is deliberately no
    `exclude_provenance_blocks` knob here: the only caller that turns the exclusion off
    (`delete_guard.check_harvest_provenance`) is single-needle, and an unused parameter on a
    delete-safety seam is a trap, not a convenience. Add it when a batched caller exists."""
    distinct_needles = list(dict.fromkeys(needles))
    if not distinct_needles:
        return {}

    existing_scope = [
        str(repo_root / d) for d in scope if (repo_root / d).is_dir()
    ]
    if not existing_scope:
        return {needle: False for needle in distinct_needles}

    _require_rg("active_reference_guard_many")
    from coordinator_core.win_portability import leaf_spawn_creationflags

    pattern_fd, pattern_path = tempfile.mkstemp(suffix=".txt", text=True, dir=managed_tempdir(repo_root))
    try:
        with os.fdopen(pattern_fd, "w", encoding="utf-8", newline="\n") as pattern_file:
            pattern_file.write("\n".join(distinct_needles) + "\n")
        result = subprocess.run(
            [
                "rg",
                "--fixed-strings",
                "--files-with-matches",
                "-f",
                pattern_path,
                *existing_scope,
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            **leaf_spawn_creationflags(),
        )
    finally:
        os.unlink(pattern_path)
    # rg exit codes: 0 = match(es) found, 1 = no match, 2 = error.
    if result.returncode == 2:
        raise RuntimeError(
            f"active_reference_guard_many: ripgrep failed (exit 2): {result.stderr.strip()}"
        )
    matched_paths = [] if result.returncode == 1 else result.stdout.splitlines()
    return referenced_needles(distinct_needles, matched_paths)


def files_citing(needles: list[str], repo_root: Path, paths: list[str]) -> dict[str, list[str]]:
    """`{needle: [repo-relative forward-slash file, ...]}` for every file under `paths` (dirs
    or files, repo-relative; absent ones skipped) whose text contains the needle, from ONE
    `rg -f` for the whole needle set. Raw containment: the caller decides which citing files
    count. A file that cannot be read is listed under every needle (ambiguity blocks)."""
    distinct = list(dict.fromkeys(needles))
    existing = [p for p in paths if (repo_root / p).exists()]
    hits: dict[str, list[str]] = {needle: [] for needle in distinct}
    if not distinct or not existing:
        return hits

    _require_rg("files_citing")
    from coordinator_core.win_portability import leaf_spawn_creationflags

    pattern_fd, pattern_path = tempfile.mkstemp(suffix=".txt", text=True, dir=managed_tempdir(repo_root))
    try:
        with os.fdopen(pattern_fd, "w", encoding="utf-8", newline="\n") as pattern_file:
            pattern_file.write("\n".join(distinct) + "\n")
        result = subprocess.run(
            ["rg", "--fixed-strings", "--files-with-matches", "-f", pattern_path, *existing],
            cwd=repo_root,
            capture_output=True,
            text=True,
            **leaf_spawn_creationflags(),
        )
    finally:
        os.unlink(pattern_path)
    if result.returncode == 2:
        raise RuntimeError(f"files_citing: ripgrep failed (exit 2): {result.stderr.strip()}")
    matched = [] if result.returncode == 1 else result.stdout.splitlines()

    root = repo_root.resolve()
    for raw in matched:
        if not raw:
            continue
        file_path = Path(raw) if Path(raw).is_absolute() else repo_root / raw
        try:
            rel = file_path.resolve().relative_to(root).as_posix()
        except ValueError:
            rel = file_path.as_posix()
        try:
            text = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        except OSError:
            for needle in distinct:
                hits[needle].append(rel)
            continue
        for needle in distinct:
            if needle in text:
                hits[needle].append(rel)
    return hits


# ---------------------------------------------------------------------------
# SIDECAR_SUFFIXES — single named constant, full-suffix-anchored
# ---------------------------------------------------------------------------

SIDECAR_SUFFIXES = (
    ".prior-art-check.md",
    ".plan-coverage-check.md",
    ".docs-check.md",
    ".plan-review-check.md",
    ".review.md",
    ".c0-findings.md",
    ".node-map.md",
    ".phase0.md",
    ".v3-divergence-check.md",
    ".patrik-review.md",
    ".sonnet-review.md",
    ".eng-director-review.md",
    ".zoli-review.md",
)
"""Process-scaffolding sidecar suffixes, full-suffix-anchored (matched via
str.endswith against the full filename, e.g. "<stem>.review.md" — never a bare
substring match on "review" anywhere in the filename). This is the single named
vocabulary; `coordinator_core/ops/check_harvest_debt.py`'s own local 3-tuple
(`.review.md`, `-check.md`, `.the Director of Engineering-review.md`) is reconciled against it here (C1
of the 2026-07-23 claude-klabauter-driven-ceremony-redesign plan) and is expected to adopt
this constant directly (C5, separate task) rather than keep an independently
authored duplicate. C8 (DoE's contract) is the eventual cross-repo single owner
of this enumeration; keep in lockstep with DoE's list.

The `-check.md`/`-review.md` dash-separated shape (as opposed to the plain
`.review.md` dotted generic above) is a DIFFERENT separator convention from the
other dotted entries, but it is NOT open-ended: a prior version of this tuple
matched the bare trailing segment alone (`"-check.md"`, `"-review.md"`), which
silently reclassified ANY filename ending in those two words as a sidecar —
including a legitimately-named plan/spec whose own slug happens to end in
"-check.md" or "-review.md" (this repo's domain vocabulary is full of
"check"/"review"/"gate" nouns). Narrowed (2026-07-23 code review) to a closed
enum of the specific reviewer/process tokens actually observed on disk —
`v3-divergence`, `the Staff Engineer`, `sonnet`, `eng-director`, `the Director of Engineering` — each still
anchored full-suffix (`.<token>-check.md` / `.<token>-review.md`, dot-prefixed
exactly as every real on-disk instance is named). A new reviewer/process token
needing this treatment is added here explicitly, by name, not inferred from a
trailing-hyphen-segment pattern.

A timestamped variant (e.g. "2026-07-12_143022-slug.review.md") is already caught
by this suffix's plain str.endswith check — no separate timestamped-variant regex
is needed, since the date prefix has no bearing on the trailing-suffix match.
(Review: workflow-review 2026-07-12 — a former TIMESTAMPED_SIDECAR_RE regex,
anchored to exactly the ".review"/".c0-findings" suffixes already in this tuple,
was confirmed dead code: every string it matched was already caught by this
endswith check, so it was removed with zero behavior change.)"""


def is_sidecar_filename(filename: str) -> bool:
    """Return True if `filename` matches a known sidecar suffix (full-suffix-anchored),
    including timestamped variants. A bare substring hit (e.g. a file literally named
    "review.md" with no stem, or "my-review-notes.md") does NOT match — anchoring is on
    the full trailing suffix, not a naive `"review" in filename` check."""
    return filename.endswith(SIDECAR_SUFFIXES)
