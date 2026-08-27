"""
coordinator_core.ops.session.legacy_touch_corpus_drain_check — measures
whether the legacy on-disk ``touched.txt`` corpus has been drained onto its
C4/C7 replacement (``touch-record.jsonl``, ``coordinator_core.session.
touch_record``) before the union-read that reaches ``touched.txt`` is
removed.

Spec backlink: docs/plans/2026-08-25-the-legacy-touch-record-is-retired-by-
repointing-its-writers.md § AC8 (C9). C9a
(``coordinator_core.ops.session.legacy_touch_corpus_migrate``) is the DRAIN
mechanism; this module is the MEASUREMENT that reports whether it worked —
it never writes to the corpus and is not itself the drain.

Why this matters (AC8): deleting the union-read while a live session or
``.agents/<id>/`` dir still carries only ``touched.txt`` (no sibling
``touch-record.jsonl``) strands every claim recorded in it — the same
silent scope loss C7b's regression produced, arriving from the corpus
instead of from a writer. This module is C8's gate: C8 (the union-read
removal) must not proceed while this check reports a nonzero undrained
count.

Deliberately its own module, not folded into
``coordinator_core.ops.reap_orphaned_agent_dirs``: that module is a
liveness-gated *reaper* (archives orphaned agent dirs); this one is a
*corpus-drain reporter* with no archival, deletion, or liveness logic of
its own. Sharing a file for convenience would have broken write-
disjointness between this chunk (C9) and C2 (which already owns
``reap_orphaned_agent_dirs.py`` and its test) — the two could otherwise
never be scheduled concurrently on the same two files.

Scope, precisely
-----------------
Mirrors ``legacy_touch_corpus_migrate``'s own scope so the drain mechanism
and the check that verifies it walk the identical corpus:
- Every ``touched.txt`` under ``<sessions_base>/**``, INCLUDING
  ``.agents/<aid>/`` dirs, EXCLUDING any path with a ``.archive`` path
  component (archived corpus is inert to every live reader; counting it as
  "undrained" would report a gate failure C8 can never clear).
- A dir "counts as undrained" purely by ``Path.exists()`` on its sibling
  ``touch-record.jsonl`` — the SAME on-disk-state-only rule
  ``legacy_touch_corpus_migrate`` uses for its own ``already_drained``
  status (see that module's "Idempotency" section). No directory-shape
  classification (session-keyed vs. agent-keyed vs. unrecognized) is
  needed here: this check only counts touched.txt presence vs. sibling
  presence, never attributes ownership.

Negative-spec
-------------
- Read-only. Opens no file for writing, creates nothing, deletes nothing.
- Reports a COUNT, not a boolean (AC8: "must report a count, not a
  boolean") — a caller wanting a pass/fail gate reads
  ``DrainCheckReport.undrained_count == 0`` itself; this module does not
  collapse that decision internally.
- Does not perform the drain. Fixing an undrained dir is
  ``legacy_touch_corpus_migrate``'s job (C9a), never this module's.
- Does not re-measure or cite a fixed historical count anywhere in this
  module — the whole point of a runnable check is that the corpus moves
  under it; see module docstring's own "RE-MEASURE; do not cite that
  number" instruction in the chunk brief this module was built from.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

GENERATES = []  # read-only: writes nothing, creates nothing, deletes nothing

_TOUCHED_FILENAME = "touched.txt"
from coordinator_core.session import touch_record

_TOUCH_RECORD_FILENAME = "touch-record.jsonl"
_EXCLUDED_COMPONENT = ".archive"


@dataclass
class DrainCheckReport:
    """Result of one ``check_drain`` call.

    ``undrained`` holds the ``touched.txt`` paths found with no sibling
    ``touch-record.jsonl`` — a count AND the paths behind it, per AC8's
    "must report a count, not a boolean".
    """

    sessions_base: Path
    scanned: int = 0
    undrained: List[Path] = field(default_factory=list)

    @property
    def undrained_count(self) -> int:
        return len(self.undrained)

    @property
    def drained_count(self) -> int:
        return self.scanned - self.undrained_count


def _iter_touched_files(sessions_base: Path):
    """Yield every ``touched.txt`` under ``sessions_base``, EXCLUDING any
    path with a ``.archive`` path component. Sorted for deterministic
    report ordering — mirrors ``legacy_touch_corpus_migrate``'s identically
    named helper (see module docstring's Scope section)."""
    if not sessions_base.is_dir():
        return
    for path in sorted(sessions_base.rglob(_TOUCHED_FILENAME)):
        rel = path.relative_to(sessions_base)
        if _EXCLUDED_COMPONENT in rel.parts:
            continue
        yield path


def check_drain(sessions_base: Path) -> DrainCheckReport:
    """Scan ``sessions_base`` and report how many ``touched.txt`` dirs
    still lack a sibling ``touch-record.jsonl`` — see module docstring.
    """
    report = DrainCheckReport(sessions_base=sessions_base)
    for touched_path in _iter_touched_files(sessions_base):
        report.scanned += 1
        record_path = touched_path.with_name(_TOUCH_RECORD_FILENAME)
        # EXISTENCE IS NOT DRAINAGE (2026-08-27). This was `not record_path.
        # exists()`, which counted an EMPTY sibling as drained. That is the one
        # shape the drain actually produces on failure: `legacy_touch_corpus_
        # migrate` creates the sink before writing into it, so a migration that
        # creates the file and then writes nothing leaves a zero-byte record
        # that satisfied the old predicate exactly. Measured on this repo
        # 2026-08-27: the check reported `undrained: []` over 133 scanned dirs
        # while eight sessions carried 167 legacy claims against an empty
        # sibling -- claims `compute_scope` cannot see, on paths any peer's
        # scope check therefore reads as orphans and is free to sweep. That is
        # precisely the "strands every claim recorded in it" loss this module's
        # own docstring exists to gate against, and the gate read green through
        # it.
        #
        # A record that cannot be read is not a record: the predicate now
        # requires READABLE, NON-BLANK content, and an unreadable sibling
        # counts as UNDRAINED (fail toward reporting work left to do, never
        # toward a green gate -- this module gates C8's union-read removal,
        # where a false green is unrecoverable claim loss).
        if not touch_record.record_carries_content(record_path):
            report.undrained.append(touched_path)
    return report


def _print_report(report: DrainCheckReport) -> None:
    print(f"=== legacy_touch_corpus_drain_check: sessions_base={report.sessions_base} ===")
    print(f"touched.txt dirs scanned: {report.scanned}")
    print(f"drained (has touch-record.jsonl): {report.drained_count}")
    print(f"undrained (touched.txt only, no sibling): {report.undrained_count}")
    if report.undrained:
        print("\nundrained dirs:")
        for path in report.undrained:
            print(f"  {path}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Reports how many touched.txt dirs under --sessions-base still "
            "lack a sibling touch-record.jsonl (C8's drain gate, AC8). "
            "Read-only: measures the drain, never performs it — see "
            "coordinator_core.ops.session.legacy_touch_corpus_migrate for "
            "the drain mechanism itself."
        )
    )
    ap.add_argument(
        "--sessions-base",
        required=True,
        help="path to <git-common-dir>/coordinator-sessions",
    )
    args = ap.parse_args(argv)

    sessions_base = Path(args.sessions_base)
    report = check_drain(sessions_base)
    _print_report(report)
    return 1 if report.undrained_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
