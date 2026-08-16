"""
coordinator_core.ops.check_harvest_debt — read-only harvest-debt nudge probe.

Purpose: count plan files under archive/specs/**/*.md that are absent from
the canonical distillation log (state/distillation-log.md). Plans there are
"harvest debt" — they exist in the archive but /distill has not yet processed
them. Prints a one-line nudge to stdout when the debt count exceeds 5; silent
otherwise. This is the DoE workday-start nudge probe, distinct from
coordinator_core.distill.harvest_debt (the deterministic specs_dir-relative
engine backing /distill's own bin/distill-harvest-debt.py CLI) — the two
intentionally use DIFFERENT keying strategies (basename here vs
specs_dir-relative there) and serve different consumers; do not conflate or
merge them.

Port of: check-harvest-debt.sh (DoE b5a4192c, 2026-07-20)
Spec backlink: DoE-claude:pln-bash-polyglot-clean-slate-full-5c71ee

Matching strategy (INTENTIONALLY basename-only, not specs_dir-relative):
the distill-log records archive/specs/<basename>.md (the pre-YYYY-MM-subdir
flat path). The on-disk layout uses YYYY-MM/ subdirectory grouping. Matching
by basename covers both the old flat layout and the current nested layout.

Negative-spec / known, accepted limitation (ported verbatim from the bash
original, NOT fixed here): two archived plans in different YYYY-MM/ subdirs
sharing an identical basename cannot be disambiguated by this basename-only
match. Claude-klabauter's deterministic engine (coordinator_core.distill.harvest_debt)
is collision-safe (specs_dir-relative keying); this probe accepts the
narrower basename limitation as a tradeoff for flat/nested layout
compatibility, exactly as the bash original did.

Negative-spec / ported awk quirk: the bash original's row-to-basename
extractor uses a per-row token scan with a `seen_specs` flag that, in the
original awk program, is a script-global variable NOT reset between rows (an
artifact of awk's default global-scope semantics, never an explicit design
choice). In practice this never changes observed behavior for canonical-shape
rows (the path token always self-matches the archive/specs/...\\.md$ pattern
directly, so the `seen_specs` fallback branch is never reached on well-formed
input) — reproduced faithfully here via a per-invocation (not per-row) mutable
flag rather than "fixed" to be per-row-scoped.

Sidecar exclusions (C5): sidecar-suffixed files — per
coordinator_core.distill._common.SIDECAR_SUFFIXES / is_sidecar_filename, the
single vocabulary (C1) that this module now consumes instead of its own
formerly-local 3-tuple (`.review.md`, `-check.md`, `.the Director of Engineering-review.md`) — are
review/process scaffolding, not plan files, and are excluded from the count.

Canonical log schema: coordinator/schemas/distillation-log.schema.md.
Row format: `- <path> -> <disposition>, <fate> (run: <run-id>)`, ASCII `->`
(NOT the U+2192 `→` glyph) is canonical; a stray U+2192 glyph is tolerated
defensively, matching the bash original.

Exit codes (unchanged from the bash original):
    0 — probe completed (nudge printed or silent, per debt threshold), OR
        archive/specs/ is absent (consumer-project no-op — nothing to check).
    1 — archive/specs/ exists (there is content that COULD be harvest debt)
        but the canonical log is absent. Fail-loud: absence of the log must
        NEVER be silently treated as "harvest everything" or silently
        skipped. See coordinator/schemas/distillation-log.schema.md §
        Negative-spec.
"""

from __future__ import annotations

import re
from coordinator_core.git.repo_root import show_toplevel
import sys
from pathlib import Path
from shutil import which

from coordinator_core.distill._common import is_sidecar_filename

_ARROW_RE = r"(?:->|→)"
_HARVEST_ROW_RE = re.compile(
    r"^- .*archive/specs/.*" + _ARROW_RE + r"\s*(?:DISTILLED|PROMOTE)\b"
)
_LOGGED_ROW_RE = re.compile(_ARROW_RE + r"\s*(?:DISTILLED|PROMOTE)\b")
_PATH_TOKEN_ARCHIVE_SPECS_RE = re.compile(r"archive/specs/.*\.md$")
_PATH_TOKEN_MD_RE = re.compile(r"\.md$")
_PATH_TOKEN_HAS_ARCHIVE_SPECS_RE = re.compile(r"archive/specs/")

_WARN_RATIO = 5
_NUDGE_THRESHOLD = 5


def _resolve_root(explicit_root: str | None) -> str | None:
    if explicit_root:
        return explicit_root
    if which("git") is None:
        return None
    top = show_toplevel()
    return top or None


def _extract_basename_from_row(line: str, seen_specs: list[bool]) -> str | None:
    """Port of the bash original's awk token scan (per-row field split on
    whitespace). `seen_specs` is a 1-element mutable box mirroring the awk
    global's persistence ACROSS THE WHOLE matched-line stream of one script
    invocation (not per-row, not per-process) — see module docstring
    negative-spec note. Callers must share one box across an entire
    `main()` invocation and start a fresh box per invocation."""
    for token in line.split():
        if _PATH_TOKEN_ARCHIVE_SPECS_RE.search(token) or (
            _PATH_TOKEN_MD_RE.search(token) and seen_specs[0]
        ):
            return token.rsplit("/", 1)[-1]
        if _PATH_TOKEN_HAS_ARCHIVE_SPECS_RE.search(token):
            seen_specs[0] = True
    return None


def _harvested_basenames_and_logged_count(log_text: str) -> tuple[set[str], int]:
    """Scan `log_text` (the full text of the canonical distillation log, e.g.
    state/distillation-log.md, read as a single str — NOT a path and NOT an
    iterable of lines) for HARVEST-shaped rows (archive/specs/... -> DISTILLED
    or PROMOTE) and return (harvested_basenames, logged_harvest_rows):

      harvested_basenames: the set of bare basenames (per _extract_basename_from_row's
          awk-port token scan) this module considers "harvested" — NOT
          specs_dir-relative paths (see the module docstring's basename-vs-
          specs_dir-relative negative-spec; this is the probe's own,
          intentionally coarser keying, distinct from
          coordinator_core.distill.harvest_debt's collision-safe keying).
      logged_harvest_rows: count of ALL rows matching the harvest disposition
          arrow anywhere in the text (`_LOGGED_ROW_RE`), used only as the
          warn-ratio denominator — not the same set as harvested_basenames
          (this count is not basename-deduplicated).

    central-em #2 named the caller-side confusion this docstring/annotation
    resolves: `log_text` was previously undocumented and easy to mistake for
    a log *path* rather than its already-read contents."""
    harvested: set[str] = set()
    seen_specs = [False]
    for line in log_text.splitlines():
        if _HARVEST_ROW_RE.search(line):
            basename = _extract_basename_from_row(line, seen_specs)
            if basename:
                harvested.add(basename)
    logged_harvest_rows = sum(
        1 for line in log_text.splitlines() if _LOGGED_ROW_RE.search(line)
    )
    return harvested, logged_harvest_rows


def main(argv: list[str]) -> int:
    explicit_root: str | None = None
    i = 0
    while i < len(argv):
        if argv[i] == "--root" and i + 1 < len(argv):
            explicit_root = argv[i + 1]
            i += 2
        else:
            i += 1

    root = _resolve_root(explicit_root)
    if not root:
        return 0

    root_path = Path(root)
    specs_dir = root_path / "archive" / "specs"
    distill_log = root_path / "state" / "distillation-log.md"

    if not specs_dir.is_dir():
        return 0

    if not distill_log.is_file():
        print(
            f"check-harvest-debt: canonical log absent at {distill_log} — cannot "
            "compute harvest debt. Run /distill at least once to create it, or "
            "verify --root points at a repo with a distillation history.",
            file=sys.stderr,
        )
        return 1

    log_text = distill_log.read_text(encoding="utf-8", errors="replace")
    harvested_basenames, logged_harvest_rows = _harvested_basenames_and_logged_count(
        log_text
    )

    unharvested = 0
    for fpath in sorted(specs_dir.rglob("*.md")):
        fname = fpath.name
        if is_sidecar_filename(fname):
            continue
        if fname not in harvested_basenames:
            unharvested += 1

    if unharvested > _NUDGE_THRESHOLD:
        print(f"{unharvested} un-harvested archived plans — run /distill")

        if logged_harvest_rows > 0 and unharvested > logged_harvest_rows * _WARN_RATIO:
            print(
                f"WARNING: harvest-debt count ({unharvested}) is more than 5x the "
                f"logged DISTILLED/PROMOTE row count ({logged_harvest_rows}) in "
                f"{distill_log}. This can indicate the log's row format has "
                "drifted from what a reader expects (silent no-op), not merely a "
                "backlog. Verify rows use the canonical schema: "
                "coordinator/schemas/distillation-log.schema.md.",
                file=sys.stderr,
            )

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
