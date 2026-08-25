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
# Review: workflow-review (2026-07-12) — the fate capture group was previously
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
(lines 20-28 as of 2026-07-23) — currently exactly these two keys; `provenance:` and
`judgment_provenance:` are named there as under-consideration, NOT yet part of the
enforced set, and must not be added here until that doc says so.

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


_FALLBACK_IGNORE_DIRS = frozenset({
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
})
"""Directory names skipped by the rg-absent fallback walk in `active_reference_guard`,
approximating rg's default VCS/build-dir ignore behavior. Deliberately a simple name
check, not `.gitignore` parsing — see Finding 5, code-reviewer 2026-07-28."""


def active_reference_guard(
    needle: str,
    repo_root: Path,
    *,
    scope: tuple[str, ...] = ACTIVE_REFERENCE_SCOPE,
) -> bool:
    """Return True if `needle` (typically a candidate-for-deletion path or slug) is still
    actively referenced somewhere under `scope` (default: docs/ tasks/ archive/) in `repo_root`.

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
    if shutil.which("rg") is not None:
        from coordinator_core.win_portability import no_console_creationflags

        result = subprocess.run(
            ["rg", "--fixed-strings", "--files-with-matches", needle, *existing_scope],
            cwd=repo_root,
            capture_output=True,
            text=True,
            **no_console_creationflags(),
        )
        # rg exit codes: 0 = match(es) found, 1 = no match, 2 = error.
        if result.returncode == 2:
            raise RuntimeError(
                f"active_reference_guard: ripgrep failed (exit 2): {result.stderr.strip()}"
            )
        if result.returncode == 1:
            return False
        matched_paths = result.stdout.splitlines()
    else:
        # ripgrep is an external CLI with no install-time provisioning and no
        # bundled Windows binary — a dev box (or a minimal container) without
        # it on PATH must not hard-crash a read-only reference guard. Fall
        # back to an equivalent in-process fixed-string files-with-matches
        # walk over the same scope dirs, approximating `rg`'s default
        # behavior in two respects:
        #   - rg skips files it detects as binary by default rather than
        #     treating "can't search this file" as "found a match here", so
        #     a file that fails to UTF-8-decode is SKIPPED here too (Review:
        #     code-reviewer — a prior version appended undecodable files to
        #     matched_paths unconditionally, which made ANY binary/non-UTF8
        #     file anywhere in scope force `active_reference_guard` to return
        #     True for every needle, independent of content).
        #   - rg respects .gitignore and skips well-known VCS/build dirs by
        #     default; the fallback approximates this with a simple
        #     directory-name skip (no .gitignore parsing).
        # A genuinely unreadable file (OSError, not merely non-text) still
        # keeps the conservative "ambiguity blocks" posture below, since that
        # is a different failure mode from "this file is binary."
        matched_paths = []
        for scope_dir in existing_scope:
            for dirpath, dirnames, filenames in os.walk(scope_dir):
                dirnames[:] = [d for d in dirnames if d not in _FALLBACK_IGNORE_DIRS]
                for filename in filenames:
                    candidate = Path(dirpath) / filename
                    try:
                        text = candidate.read_text(encoding="utf-8")
                    except UnicodeDecodeError:
                        # Binary/non-UTF-8 file: rg would skip it by default.
                        continue
                    except OSError:
                        # Genuinely unreadable (permissions, race, etc.) —
                        # ambiguity blocks, keep the conservative append.
                        matched_paths.append(str(candidate))
                        continue
                    if needle in text:
                        matched_paths.append(str(candidate))
    for matched_path in matched_paths:
        if not matched_path:
            continue
        try:
            file_text = Path(matched_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Cannot verify this citation is provenance-only -> ambiguity blocks.
            return True
        if needle in _text_excluding_provenance_blocks(file_text):
            return True
    return False


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
