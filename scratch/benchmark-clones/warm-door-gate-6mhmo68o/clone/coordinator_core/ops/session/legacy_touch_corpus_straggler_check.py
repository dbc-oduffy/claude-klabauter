"""
coordinator_core.ops.session.legacy_touch_corpus_straggler_check — measures
whether any legacy ``touched.txt`` event is absent from its dir's
``touch-record.jsonl`` sibling, i.e. whether deleting the compat union-read
would strand a claim that the existence-keyed drain check cannot see.

Spec backlink: docs/plans/2026-08-26-the-claim-release-path-releases-nothing.md,
and the union's own contract in ``session.scope._read_touch_record_as_legacy_
lines``.

WHY THIS EXISTS — THE DRAIN CHECK ANSWERS A DIFFERENT QUESTION.
``legacy_touch_corpus_drain_check`` reports a dir as drained on
``Path.exists()`` of the jsonl sibling, and ``legacy_touch_corpus_migrate`` is
idempotent on that same existence test: its own docstring states that a dir
holding a ``touch-record.jsonl`` "is treated as ALREADY DRAINED and never opens
either file". Both are correct for the one-shot migration they were built for.

Neither can see a legacy event appended AFTER that dir was drained. So
"re-run the migration to sweep stragglers, then delete the union" — the
sequence the union's own docstring prescribed — is not executable by those two
modules: the re-run reports ``already_drained`` for every dir and writes
nothing, and the drain check then reports zero undrained. Both green, both
uninformative, and the union looks safe to delete when it may not be. This
module is the missing content-level measurement.

NEGATIVE SPEC
-------------
- Reports only. Never writes to the corpus, never migrates, never deletes.
- Not a liveness check and not a reaper.
- Does NOT supersede ``legacy_touch_corpus_drain_check``: an UNDRAINED dir (no
  jsonl at all) is that module's category and is skipped here, so the union
  deletion gate is BOTH checks reading zero, never this one alone.

Scope, precisely
----------------
Mirrors ``legacy_touch_corpus_migrate``'s scope so all three modules walk one
corpus: every ``touched.txt`` under ``<sessions_base>/**`` including
``.agents/<aid>/``, EXCLUDING any path with a ``.archive`` component (archived
corpus is inert to every live reader).

An entry that the migration itself would have DROPPED is not a straggler and is
not counted: an absolute path, or one that escapes the worktree after
canonicalization. Those never entered the jsonl by design, and the readers
screen them out too (see ``claim_index._read_stream_claims``'s containment
guard), so counting them would report a gate failure that can never clear —
the exact failure mode the drain check's own ``.archive`` exclusion avoids.

PUBLISH LAG IS NOT MEASURED HERE, AND IT GATES THE SAME DELETION.
A zero from this module describes THIS repo's corpus at THIS instant. Live
sessions import the engine from the published mirror, so until a writer
migration reaches that mirror, peers keep running the old writers and keep
appending legacy events after this check has read a dir. Re-run this after the
writer change is published and the pre-publish cohort has exited; a single
zero taken before that proves nothing about the fleet.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, List, NamedTuple, Tuple

from coordinator_core.session import scope, touch_record
from coordinator_core.session.path_dialect import canonicalize_relative_path

_LEGACY_FILENAME = "touched.txt"
_RECORD_FILENAME = "touch-record.jsonl"
_ARCHIVE_COMPONENT = ".archive"


class StragglerReport(NamedTuple):
    """One drained dir's verdict. ``missing`` is the canonicalized paths whose
    legacy last-state has no counterpart in the jsonl family."""

    directory: str
    missing: List[str]


def _is_migratable(key: str) -> bool:
    """False for the entries ``legacy_touch_corpus_migrate`` drops by design —
    absolute, or worktree-escaping after canonicalization. Containment is judged
    on the CANONICAL value, not a leading ``../``: ``docs/../../peer/x.md``
    escapes without ever starting with one."""
    if not key or os.path.isabs(key):
        return False
    return not (key == ".." or key.startswith("../"))


def _legacy_last_state(legacy_path: str) -> Dict[str, str]:
    """Last-verb-wins over one legacy file, keyed by canonical path. Unreadable
    file yields an empty map — this module reports, it does not raise."""
    out: Dict[str, str] = {}
    try:
        with open(legacy_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                verb, _ts, raw = scope.parse_touch_event(line)
                if not raw:
                    continue
                key = canonicalize_relative_path(raw)
                if key is None or not _is_migratable(key):
                    continue
                out[key] = verb
    except OSError:
        return {}
    return out


def scan(sessions_base: str) -> Tuple[int, List[StragglerReport]]:
    """Return ``(drained_dirs_examined, reports)``; ``reports`` holds only dirs
    with at least one straggler."""
    examined = 0
    reports: List[StragglerReport] = []

    for root, _dirs, files in os.walk(sessions_base):
        if _ARCHIVE_COMPONENT in root.split(os.sep):
            continue
        if _LEGACY_FILENAME not in set(files):
            continue
        sink = os.path.join(root, _RECORD_FILENAME)
        if not (os.path.isfile(sink) or touch_record.discover_family(sink)):
            continue  # UNDRAINED -- drain_check's category, deliberately not ours
        examined += 1

        legacy = _legacy_last_state(os.path.join(root, _LEGACY_FILENAME))
        if not legacy:
            continue
        try:
            jclaims, _degraded, _reasons = touch_record._read_stream_claims(sink)
        except Exception:
            continue
        jkeys = {canonicalize_relative_path(p) or p for p in jclaims}
        missing = sorted(k for k in legacy if k not in jkeys)
        if missing:
            reports.append(StragglerReport(directory=root, missing=missing))

    return examined, reports


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Report legacy touched.txt events absent from their touch-record.jsonl "
            "sibling. Read-only. Zero here plus zero from drain_check is the union "
            "deletion gate -- neither alone."
        )
    )
    ap.add_argument(
        "--sessions-base",
        required=True,
        help="path to <git-common-dir>/coordinator-sessions",
    )
    args = ap.parse_args(argv)

    examined, reports = scan(args.sessions_base)
    print(
        f"=== legacy_touch_corpus_straggler_check: "
        f"sessions_base={args.sessions_base} ==="
    )
    print(f"drained dirs examined: {examined}")
    print(f"dirs with legacy-only events: {len(reports)}")
    for rep in reports:
        print(f"  {len(rep.missing):4d}  {rep.directory}")
        for key in rep.missing[:5]:
            print(f"          {key}")
    if reports:
        print(
            "\nDELETING THE UNION WOULD STRAND THESE CLAIMS. "
            "Drain them before removing any legacy read arm."
        )
        return 1
    print("\nNo stragglers. This says nothing about publish lag -- see module docstring.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
