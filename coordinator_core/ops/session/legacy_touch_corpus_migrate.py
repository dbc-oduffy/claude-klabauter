"""
coordinator_core.ops.session.legacy_touch_corpus_migrate — one-shot corpus
migration from the legacy per-dir ``touched.txt`` dialect onto the C4/C7
touch-record (``touch-record.jsonl``, ``coordinator_core.session.touch_
record``).

Spec backlink: docs/plans/2026-08-25-the-legacy-touch-record-is-retired-by-
repointing-its-writers.md § AC8 (C9a). C9a is the DRAIN mechanism, C9 (a
separate chunk, ``legacy_touch_corpus_drain_check.py``) is the MEASUREMENT
that reports whether it worked.

AC8/EM decision (settled at plan-write time, restated here as the module's
own contract): the drain is a ONE-SHOT MIGRATION, never an age-out window.
After C7 lands, an old agent/session dir can never spontaneously grow a
sibling ``touch-record.jsonl`` on its own — nothing rewrites history — so
an age-out window would mean the dirs must DIE, destroying claims rather
than draining them. This module instead WRITES the sibling record file
those dirs are missing, once, and never touches the legacy source.

Prior art: ``coordinator_core.ops.session.migrate_touched_prefix`` is the
shape this module borrows its CLI/report structure from (dry-run-by-
default argparse surface, a per-entry classification pass, a
``MigrationReport``/``totals()`` aggregate) — it does NOT share code with
it, because the two migrations write to different files under different
safety contracts (that module rewrites ``touched.txt`` in place under a
lock; this one only ever CREATES a new sibling file and never opens
``touched.txt`` for writing).

Scope, precisely
-----------------
- Every ``touched.txt`` under ``<sessions_base>/**``, INCLUDING
  ``.agents/<aid>/`` dirs, EXCLUDING any path with a ``.archive`` path
  component (mirrors ``migrate_touched_prefix``'s own scope — archived
  poisoning/corpus is inert to every live reader, so migrating it buys
  nothing and only grows the corpus this module has to walk).
- Exactly two directory shapes are recognized under ``sessions_base``:
    session-keyed  — ``<sessions_base>/<sid>/touched.txt``
    agent-keyed    — ``<sessions_base>/.agents/<aid>/touched.txt``
  Anything else (an unexpected nesting depth) is reported as
  ``unrecognized_shape`` and left untouched — this module never guesses at
  a directory shape it cannot positively identify.
- Nothing else. This module does not touch live session state, meta.json,
  claim files, or any file outside a ``touched.txt``/``touch-record.jsonl``
  pair.

Idempotency (AC8's "one-shot")
-------------------------------
A dir's on-disk state alone decides whether it is migrated:
``touch-record.jsonl`` present (however it got there — a prior run of this
module, OR a live writer that has since started emitting the new dialect
for that dir) means this module treats the dir as ALREADY DRAINED and
never opens either file. Re-running this module over a fully-migrated
corpus is therefore a no-op by construction — no flag, timestamp, or
manifest state is consulted to make that call, only ``Path.exists()`` on
the sibling.

Session-id attribution for an agent-keyed dir
-----------------------------------------------
A session-keyed ``touched.txt`` names its own owning session in its parent
directory name; an agent-keyed one does not — the owning session is
recorded separately, in ``em-session-id.txt`` inside the same agent dir
(``track_dispatched_agents._write_backpointer_sync`` /
``track_touched_files``'s Piece 2 write are its two writers). A
touch-record.jsonl line requires a non-empty ``sid`` (see
``touch_record.decode_line``'s validation) — this module refuses to
FABRICATE one. An agent dir with a missing or empty ``em-session-id.txt``
is reported as ``skipped_no_owner`` and left fully untouched (no sibling
file is created, so it remains visibly undrained to C9's check) rather
than guessing an owner or writing a structurally-invalid record. This is
the same "ownerless agent dir" class ``track_touched_files.py``'s own
comment already names (343 of 1353 observed on this repo at one point) —
this module does not attempt to solve that separate problem, only avoids
compounding it with a fabricated attribution.

Per-entry transform
--------------------
Each ``touched.txt`` line is parsed with
``coordinator_core.session.scope.parse_touch_event`` (verb, timestamp,
path — its own documented fail-safe reports a legacy bare-path or
otherwise-unparseable line as ``("T", None, <whole line>)``), then the path
field is classified and canonicalized with
``coordinator_core.session.scope.classify_touch_entry`` — the SAME
worktree-containment transform ``compute_scope``'s own defensive read-side
normalization and ``migrate_touched_prefix`` already share, imported here
rather than re-derived (see that function's own negative-spec: importing,
never re-deriving, is how the whole corpus stays on one dialect). A
``dropped`` classification (an absolute or worktree-escaping entry
``classify_touch_entry`` cannot rescue) is recorded in the per-dir
``drop_manifest`` and never written; a ``blank`` entry is silently
skipped; a ``clean``/``absolute_rescued`` entry is written as one
``touch_record`` event via ``touch_record.append_event``, preserving its
original verb.

A legacy entry with an unparseable/absent timestamp (``ts is None`` from
``parse_touch_event``) is written with ``timestamp=0.0`` (Unix epoch) —
the earliest representable value, chosen for the same reason
``scope.py``'s own ``_TOUCH_EVENT_EPOCH_MIN`` sentinel exists: an
"unknown time" claim must never be able to out-rank (via
``touch_record._merge_across_streams``'s later-``ts``-wins rule) a real,
stamped event from another stream. It is a value, not a `None` — the new
schema's ``ts`` field is a required float (``decode_line`` coerces via
``float(record["ts"])``), so there is no "unknown" representation to
preserve; epoch is the transform's designed answer to that gap.

Negative-spec
-------------
- Dry-run is the DEFAULT. ``run_migration(apply=False)`` (or the CLI with
  no ``--apply``) classifies every entry and reports what WOULD happen;
  it opens no file for writing and creates nothing.
- NEVER deletes, truncates, or rewrites ``touched.txt``. This module's
  only write is creating (or appending a fresh line to) the SIBLING
  ``touch-record.jsonl`` — the legacy file is left in place unconditionally,
  for C8/C9's checks to find and for manual recovery if something is
  wrong (AC8's own text, restated as a hard contract here).
- Writes route through ``touch_record.append_event`` (which itself routes
  through ``atomic_append.append_line``) — one append per surviving entry,
  never a batch write or a bare ``open(..., "w")``. A dir whose
  ``touched.txt`` has zero surviving entries (empty file, or every entry
  drops/blanks) still gets an EMPTY ``touch-record.jsonl`` created directly
  (``Path.touch()``) so the dir stops reading as "undrained" to C9's
  check — creating a zero-byte sibling is not a claim write and needs no
  append machinery.
- No backup is taken and none is needed: this module never opens the
  legacy source for writing, so there is nothing destructive to back out
  of (contrast ``migrate_touched_prefix``, which rewrites its source in
  place and therefore backs it up first).
"""

from __future__ import annotations

GENERATES = []  # writes touch-record.jsonl siblings under <git-common-dir>/coordinator-sessions/; never touched.txt, never a tracked path

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from coordinator_core.session import touch_record
from coordinator_core.session.scope import classify_touch_entry, parse_touch_event

#: See module docstring's "Per-entry transform" section.
_UNKNOWN_TS = 0.0

#: The one path component that excludes a touched.txt from migration
#: entirely (see § Scope). Matched against any path segment.
_EXCLUDED_COMPONENT = ".archive"

_TOUCH_RECORD_FILENAME = "touch-record.jsonl"
_TOUCHED_FILENAME = "touched.txt"
_EM_SESSION_ID_FILENAME = "em-session-id.txt"

#: Per-dir status labels this module reports. Mutually exclusive.
_STATUSES = (
    "migrated",
    #: Entries existed and EVERY one was dropped by the containment rule, so
    #: nothing was salvaged and no sink is written (2026-08-27). Distinct from
    #: `migrated` with `entries_written == 0`, which this used to be reported
    #: as: that reading left an empty sink which then satisfied the old
    #: exists()-based already-drained predicate here and in
    #: `legacy_touch_corpus_drain_check`, permanently marking the dir done with
    #: its claims invisible. Distinct from `unrecognized_shape` too -- the dir
    #: is recognized and readable, its corpus is simply unsalvageable, which is
    #: `migrate_touched_prefix`'s repair to make, not this drain's.
    "stranded_all_dropped",
    "already_drained",
    "skipped_no_owner",
    "unrecognized_shape",
    "read_error",
)


@dataclass
class DirOutcome:
    """Per-directory migration result — one per ``touched.txt`` found."""

    touched_path: Path
    record_path: Path
    kind: str  # "session" | "agent" | "unknown"
    session_id: Optional[str]
    agent_id: Optional[str]
    status: str
    entries_written: int = 0
    entries_dropped: int = 0
    entries_blank: int = 0
    drop_manifest: List[dict] = field(default_factory=list)
    read_error: Optional[str] = None


@dataclass
class MigrationReport:
    """Aggregate result of one ``run_migration`` call (dry-run or apply)."""

    apply: bool
    sessions_base: Path
    worktree_root: Path
    dirs: List[DirOutcome] = field(default_factory=list)

    def totals(self) -> Dict[str, int]:
        totals: Dict[str, int] = {name: 0 for name in _STATUSES}
        for d in self.dirs:
            if d.status in totals:
                totals[d.status] += 1
        return totals

    def entries_written_total(self) -> int:
        return sum(d.entries_written for d in self.dirs)

    def entries_dropped_total(self) -> int:
        return sum(d.entries_dropped for d in self.dirs)

    def drop_manifest(self) -> List[dict]:
        manifest: List[dict] = []
        for d in self.dirs:
            manifest.extend(d.drop_manifest)
        return manifest


def _iter_touched_files(sessions_base: Path):
    """Yield every ``touched.txt`` under ``sessions_base``, EXCLUDING any
    path with a ``.archive`` path component. Sorted for deterministic
    report ordering — mirrors ``migrate_touched_prefix``'s own helper."""
    if not sessions_base.is_dir():
        return
    for path in sorted(sessions_base.rglob(_TOUCHED_FILENAME)):
        rel = path.relative_to(sessions_base)
        if _EXCLUDED_COMPONENT in rel.parts:
            continue
        yield path


def _classify_dir(touched_path: Path, sessions_base: Path) -> Tuple[str, Optional[str], Optional[str]]:
    """Return ``(kind, session_id, agent_id)`` for one ``touched.txt`` path,
    per § Scope's two recognized shapes. ``kind`` is ``"unknown"`` for
    anything else — see module docstring's ``unrecognized_shape`` status."""
    rel = touched_path.relative_to(sessions_base)
    parts = rel.parts  # e.g. (sid, "touched.txt") or (".agents", aid, "touched.txt")
    if len(parts) == 2:
        return "session", parts[0], None
    if len(parts) == 3 and parts[0] == ".agents":
        return "agent", None, parts[1]
    return "unknown", None, None


def _read_owner_session_id(agent_dir: Path) -> Optional[str]:
    """Read the agent dir's ``em-session-id.txt`` back-pointer. Returns
    ``None`` if absent, empty, or unreadable — never fabricates a value
    (see module docstring's Session-id attribution section)."""
    backpointer = agent_dir / _EM_SESSION_ID_FILENAME
    try:
        text = backpointer.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _plan_entries(
    touched_path: Path, worktree_root: Path
) -> Tuple[List[Tuple[str, Optional["object"], str]], int, List[dict]]:
    """Classify every line of ``touched_path``.

    Returns ``(surviving_events, blank_count, drop_manifest)`` where each
    surviving event is ``(verb, ts, canonical_path)`` — ``ts`` is the
    ``datetime`` `parse_touch_event` returned, or ``None`` for the legacy/
    unparseable-timestamp case (written as ``_UNKNOWN_TS`` at apply time —
    see module docstring).
    """
    text = touched_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    surviving: List[Tuple[str, Optional["object"], str]] = []
    blank_count = 0
    drop_manifest: List[dict] = []
    for line in lines:
        verb, ts, path_field = parse_touch_event(line)
        outcome = classify_touch_entry(path_field, worktree_root)
        if outcome.entry_class == "blank":
            blank_count += 1
            continue
        if outcome.entry_class == "dropped":
            drop_manifest.append(
                {
                    "source": str(touched_path),
                    "path": outcome.original,
                    "reason": outcome.drop_reason,
                    "class": outcome.entry_class,
                }
            )
            continue
        # clean / absolute_rescued
        surviving.append((verb, ts, outcome.new_value))
    return surviving, blank_count, drop_manifest


def plan_dir(touched_path: Path, sessions_base: Path, worktree_root: Path) -> DirOutcome:
    """Classify one ``touched.txt`` dir without writing anything — the
    shared planning step used by both dry-run and apply."""
    record_path = touched_path.with_name(_TOUCH_RECORD_FILENAME)
    kind, session_id, agent_id = _classify_dir(touched_path, sessions_base)

    # EXISTENCE IS NOT DRAINAGE (2026-08-27) -- shares one predicate with
    # `legacy_touch_corpus_drain_check`, homed in `touch_record` (see
    # `record_carries_content`'s docstring for the incident). This was
    # `record_path.exists()`, which is the one shape a FAILED run of THIS
    # function leaves behind: the sink is created before events are written,
    # so a run that creates the file and writes nothing marks the dir
    # `already_drained` on every subsequent run and can never repair itself.
    # Eight dirs on claude-klabauter sat that way with 167 stranded claims.
    if touch_record.record_carries_content(record_path):
        return DirOutcome(
            touched_path=touched_path,
            record_path=record_path,
            kind=kind,
            session_id=session_id,
            agent_id=agent_id,
            status="already_drained",
        )

    if kind == "unknown":
        return DirOutcome(
            touched_path=touched_path,
            record_path=record_path,
            kind=kind,
            session_id=session_id,
            agent_id=agent_id,
            status="unrecognized_shape",
        )

    if kind == "agent":
        session_id = _read_owner_session_id(touched_path.parent)
        if not session_id:
            return DirOutcome(
                touched_path=touched_path,
                record_path=record_path,
                kind=kind,
                session_id=None,
                agent_id=agent_id,
                status="skipped_no_owner",
            )

    try:
        surviving, blank_count, drop_manifest = _plan_entries(touched_path, worktree_root)
    except OSError as exc:
        return DirOutcome(
            touched_path=touched_path,
            record_path=record_path,
            kind=kind,
            session_id=session_id,
            agent_id=agent_id,
            status="read_error",
            read_error=str(exc),
        )

    # A run that salvages NOTHING is not a migration (2026-08-27). Reporting
    # `migrated` with `entries_written == 0` was the lie that made this drain
    # unrepeatable: the apply path still created the sink, the empty sink then
    # satisfied the old `record_path.exists()` predicate in BOTH this function
    # and `legacy_touch_corpus_drain_check`, and the dir was permanently
    # `already_drained` with every one of its claims stranded. Measured here
    # 2026-08-27: eight session dirs, 167 legacy entries, 100% dropped
    # (141 escaping the worktree after normalization, 26 unresolvable
    # absolutes), reported as eight successful migrations over a gate showing
    # zero undrained.
    #
    # `stranded_all_dropped` is deliberately its OWN status rather than a
    # variant of `migrated` or a reuse of `unrecognized_shape`: the dir IS
    # recognized and IS readable, the entries are simply all unsalvageable
    # under the containment rule, which is a corpus-repair question
    # (`migrate_touched_prefix`'s territory) and not something re-running this
    # drain can ever fix. The apply path must leave NO sink for these -- see
    # its own guard -- so a later corpus repair can still drain the dir.
    # `drop_manifest` non-empty is the discriminator, NOT `not surviving`
    # alone: an EMPTY (or all-blank) touched.txt has nothing to salvage and
    # its empty sibling is a CORRECT drain -- that dir really is finished, and
    # `test_empty_touched_file_still_creates_empty_sibling` pins it. Only a dir
    # that HAD entries and lost every one of them to the containment rule is
    # stranded, because only that dir still holds claims nothing can see.
    if drop_manifest and not surviving:
        return DirOutcome(
            touched_path=touched_path,
            record_path=record_path,
            kind=kind,
            session_id=session_id,
            agent_id=agent_id,
            status="stranded_all_dropped",
            entries_written=0,
            entries_dropped=len(drop_manifest),
            entries_blank=blank_count,
            drop_manifest=drop_manifest,
        )

    return DirOutcome(
        touched_path=touched_path,
        record_path=record_path,
        kind=kind,
        session_id=session_id,
        agent_id=agent_id,
        status="migrated",
        entries_written=len(surviving),
        entries_dropped=len(drop_manifest),
        entries_blank=blank_count,
        drop_manifest=drop_manifest,
    )


def _apply_dir(outcome: DirOutcome, touched_path: Path, worktree_root: Path) -> None:
    """Write ``outcome.record_path`` for one ``migrated``-status dir.

    Recomputes the entry classification at write time (never trusts the
    dry-run-era ``outcome`` values for the write itself) so a concurrent
    legacy append landing between planning and this call is still
    included — the same "recompute under the write, don't trust the
    pre-scan" discipline ``migrate_touched_prefix.run_migration`` documents
    for its own apply path, applied here even though this module holds no
    lock (it has nothing to serialize against: each surviving entry is one
    independent, atomic ``touch_record.append_event`` call, never a
    whole-file rewrite).
    """
    surviving, _blank_count, _drop_manifest = _plan_entries(touched_path, worktree_root)
    if not surviving:
        # No claims to carry forward — still create the sibling so the dir
        # reads as drained (see module docstring's Negative-spec).
        # Not a session dir (ensure_session): this is the ALREADY-EXISTING dir
        # the scanned touched.txt lives in; minting a record for a fossil dir
        # is exactly what this one-shot corpus drain must not do.
        outcome.record_path.parent.mkdir(parents=True, exist_ok=True)
        outcome.record_path.touch(exist_ok=True)
        return

    for verb, ts, canonical_path in surviving:
        timestamp = _UNKNOWN_TS if ts is None else ts.timestamp()
        touch_record.append_event(
            outcome.record_path,
            session_id=outcome.session_id,
            agent_id=outcome.agent_id,
            verb=verb,
            path=canonical_path,
            timestamp=timestamp,
        )


def run_migration(
    sessions_base: Path,
    worktree_root: Path,
    *,
    apply: bool = False,
) -> MigrationReport:
    """Dry-run (default) or apply the legacy touch corpus migration.

    ``sessions_base`` is ``<git-common-dir>/coordinator-sessions``.
    ``worktree_root`` is the worktree root ``classify_touch_entry``'s
    containment check and ``normalize_touch_path``'s absolute-rescue arm
    both need.

    Dry-run (``apply=False``): plans every dir, writes NOTHING. Safe to
    call against the live corpus.

    Apply (``apply=True``): for every ``migrated``-status dir (per-dir
    ``touched.txt`` present, no sibling ``touch-record.jsonl`` yet, and —
    for an agent dir — a resolvable owning session id), writes the
    sibling record. Never touches ``touched.txt`` itself. See module
    docstring's Negative-spec.
    """
    report = MigrationReport(apply=apply, sessions_base=sessions_base, worktree_root=worktree_root)

    for touched_path in _iter_touched_files(sessions_base):
        outcome = plan_dir(touched_path, sessions_base, worktree_root)
        report.dirs.append(outcome)
        if apply and outcome.status == "migrated":
            _apply_dir(outcome, touched_path, worktree_root)

    return report


def _print_report(report: MigrationReport) -> None:
    totals = report.totals()
    print(
        f"=== legacy_touch_corpus_migrate: apply={report.apply} "
        f"sessions_base={report.sessions_base} worktree_root={report.worktree_root} ==="
    )
    print(f"dirs scanned: {len(report.dirs)}")
    for name in _STATUSES:
        print(f"  {name}: {totals[name]}")
    print(f"entries written: {report.entries_written_total()}")
    print(f"entries dropped: {report.entries_dropped_total()}")
    manifest = report.drop_manifest()
    if manifest:
        print(f"\n{len(manifest)} entries WOULD be dropped:" if not report.apply else f"\n{len(manifest)} entries were dropped:")
        for entry in manifest:
            print(f"  [{entry['class']}] {entry['source']}: {entry['path']!r} — {entry['reason']}")
    no_owner = [d for d in report.dirs if d.status == "skipped_no_owner"]
    if no_owner:
        print(f"\n{len(no_owner)} agent dir(s) skipped — no resolvable em-session-id.txt owner:")
        for d in no_owner:
            print(f"  {d.touched_path}")
    if not report.apply:
        print("\n(dry-run — nothing written; pass --apply to write, after review)")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "One-shot migration of the legacy touched.txt corpus onto "
            "touch-record.jsonl. Dry-run by default; pass --apply to write. "
            "Never modifies or deletes touched.txt."
        )
    )
    ap.add_argument(
        "--sessions-base",
        required=True,
        help="path to <git-common-dir>/coordinator-sessions",
    )
    ap.add_argument(
        "--worktree-root",
        required=True,
        help="the worktree root used for path containment/rescue",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="write the migration (default: dry-run, writes nothing)",
    )
    args = ap.parse_args(argv)

    sessions_base = Path(args.sessions_base)
    worktree_root = Path(args.worktree_root)

    report = run_migration(sessions_base, worktree_root, apply=args.apply)
    _print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
