"""
coordinator_core.ops.session.migrate_touched_prefix — one-time corrective
migration for the historical touched.txt '../'-prefix/absolute-path
poisoning fixed at the writer (C0/C2) and reader (C0) layers.

Purpose: before the C0/C2 fix landed, `track_touched_files.py` handed
`_normalize_path` the git COMMON dir instead of the worktree root, so every
absolute path it recorded was written one directory too high with a '../'
prefix (or, when the relpath attempt itself failed, left fully absolute).
The writer is fixed and the fixed writer is the serving engine as of this
session — new writes are clean — but 337 pre-existing session
`touched.txt` files (and `.agents/<aid>/touched.txt` files) still carry the
poisoned dialect and corrupt `compute_scope`'s candidate/other_owner key
space. This module migrates that historical corpus IN PLACE, once.

Spec backlink: pln-touched-txt-path-poisoning-nor-ecab01
§ C6 (Tasks), § Recommendation on the corpus fork (AC7), § AC7.

Scope, precisely
-----------------
- `.git/coordinator-sessions/**/touched.txt`, INCLUDING `.agents/<aid>/`
  subdirs, EXCLUDING every path with a `.archive` path component.
  `compute_scope`'s other-session scan skips dot-entries, so archived
  poisoning is inert and deliberately left unmigrated (see plan § AC7).
- Nothing else. This module does not touch live session state, meta.json,
  claim files, or any file outside a `touched.txt` leaf.

Transform (per entry line), see § Recommendation for the full rationale
------------------------------------------------------------------------
Retirement (C1/C1b, this session): the former `stripped_one_level` /
`multi_level` split — a leading-'../'-token-COUNT heuristic that left a
real containment hole (an entry can escape the worktree without starting
with a literal '../' at all, e.g. `docs/../../peer/x.md`) — is retired.
Both collapse into the single `dropped` outcome below, keyed on worktree
CONTAINMENT, not '../'-prefix depth. See
`coordinator_core.session.scope.classify_touch_entry`, the canonical home
for this transform.

  clean             — one canonical value `posixpath.normpath(entry.
                      replace("\\", "/"))` is contained inside the
                      worktree. Returns the entry UNCHANGED if it is
                      already canonical, otherwise rewritten to the
                      canonical value (e.g. a redundant `./`/doubled
                      separator or a backslash-separated path is
                      canonicalized, not merely passed through).
  absolute_rescued  — an absolute entry that `normalize_touch_path` (run
                       with the CORRECTED worktree root) resolves to a
                       clean, non-absolute, in-tree path: rewritten to the
                       rescued value.
  dropped           — an absolute entry `normalize_touch_path` still cannot
                       resolve (still absolute, or the relpath attempt
                       failed), or a non-absolute entry whose canonical
                       value escapes the worktree (is `.`/`..`, starts with
                       `../`, or is absolute): DROPPED, recorded in the
                       manifest. Collapses the former `stripped_one_level`/
                       `multi_level` split into one outcome.

Consequence for `--apply`: a `../`-escaping entry is no longer REWRITTEN
to a stripped in-tree remainder — it is DELETED outright. This module is a
PRUNER for that class now, not a rewriter: a re-run over a previously-
"stripped_one_level" corpus removes those lines from `touched.txt` rather
than leaving a rewritten path behind.

Data-format note: the persisted drop-manifest JSON's `class` field
(`_rebuild_drop_manifest`) now emits `"dropped"` where it used to emit
`"multi_level"` for a two-or-more-level escaping entry — a consumer
reading an old manifest (pre-this-session) alongside a new one sees this
as a schema change on that field's value space.

Existence-on-disk plays no role in `clean`-vs-`dropped` classification: a
`clean`-but-canonicalized entry naming a path that no longer exists on
disk still classifies `clean`, and a `../`-escaping entry classifies
`dropped` unconditionally, whether or not its would-be remainder exists
(historically, a single-level escape was rescued as `stripped_one_level`
when its remainder existed; that rescue is retired — see Retirement
above — and such entries are now dropped regardless of existence). The
only disambiguator applied is worktree containment
(`posixpath.normpath`), per § Recommendation's correction that
`normalize_touch_path` itself has no containment check to lean on.

Negative-spec
-------------
- Dry-run is the DEFAULT. Apply is opt-in via `run_migration(apply=True)` /
  the CLI's `--apply` flag. Dry-run never opens a `touched.txt` for
  writing and never creates a backup.
- Do NOT rewrite via a plain read-then-`Path.write_text` round trip in
  apply mode: every `touched.txt` is written continuously by live sessions
  through `locked_write.locked_rmw` under an flock. This module mutates
  through the SAME lock domain (`locked_write.locked_rmw`), one file at a
  time, AND the classification itself is recomputed from the lock-scoped
  value `locked_rmw` hands the `mutate` callback — not from a pre-lock
  scan — so a concurrent hook append landing between the scan and the
  lock acquisition is preserved in the written output rather than
  silently clobbered by a stale pre-computed replacement.
- The backup destination MUST NOT be a plain (non-dot-prefixed) subdirectory
  directly under `.git/coordinator-sessions/` — `compute_scope`'s
  other-session scan does `os.listdir(<sessions_base>)` and would read such
  a subdir as a live session dir. The default backup destination is a
  dot-prefixed sibling of `coordinator-sessions/` under the git common dir.
- Idempotent: a second `apply=True` run over its own output is a no-op
  (every remaining entry classifies as `clean`) — proven by this module's
  own test suite, not merely asserted here.
"""

from __future__ import annotations

GENERATES = []  # rewrites touched.txt files under <git-common-dir>/coordinator-sessions/ and writes a timestamped backup dir under the git common dir; session bookkeeping under .git/, never a tracked path

import argparse
import json
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from coordinator_core import locked_write
from coordinator_core.session.scope import (
    TouchEntryClassification,
    classify_touch_entry,
    format_touch_event,
    parse_touch_event,
)

#: The one path component that excludes a touched.txt from migration
#: entirely (see § Scope above). Matched against any path segment, not just
#: the top level, so a hypothetical nested `.archive` also excludes.
_EXCLUDED_COMPONENT = ".archive"

#: The three (plus "blank") mutually-exclusive per-entry classes this module
#: reports. "stripped_one_level" and "multi_level" are RETIRED (C1/C1b) —
#: classify_touch_entry never produces either label now, so they are not
#: kept here as permanently-zero entries (a live-but-unproducible _CLASSES
#: member would misdescribe the transform); both collapse into "dropped".
_CLASSES = (
    "clean",
    "absolute_rescued",
    "dropped",
)


#: Re-exported for backward compatibility — the transform and its result
#: dataclass now live in ``coordinator_core.session.scope`` (C7/AC8), the
#: canonical home shared with `compute_scope`'s Step 1/3/3b defensive
#: read-side normalization, so the two never fork into separate dialects.
#: Do NOT redefine either name locally; import and re-export only.
EntryOutcome = TouchEntryClassification
classify_entry = classify_touch_entry


def _classify_line(
    line: str, worktree_root: Path
) -> Tuple[TouchEntryClassification, bool]:
    """Event-line-aware wrapper around `classify_touch_entry` — parses
    ``line`` with `parse_touch_event` (imported from `session.scope`, never
    reimplemented here) and transforms the PATH FIELD ONLY, leaving any real
    verb/timestamp prefix byte-for-byte untouched on re-emission.

    A legacy bare-path line (or any line `parse_touch_event` cannot parse as
    a real event — its documented fail-safe reports `ts is None` for both)
    is classified and rewritten exactly as before: the whole line IS the
    path, and a rewritten value is re-emitted as a bare line, never upgraded
    to a stamped `T` — this tool does prefix rewriting, not claim release,
    and inventing a timestamp would fabricate ordering evidence.

    A real event line (`ts` is not None) has its path field classified in
    isolation; the verb and original timestamp are preserved via
    `format_touch_event(verb, new_path, ts)` — round-tripping through
    `datetime.fromisoformat`/`.isoformat()` reproduces the original
    timestamp text byte-for-byte for any value `format_touch_event` itself
    produced. The returned classification's `entry_class`/`drop_reason`
    reflect the PATH's classification; `new_value` is the full re-emitted
    line (or None, dropping the whole record, if the path itself drops).

    Returns ``(classification, value_changed)``. ``value_changed`` is the
    file-level change discriminator (fixes the bug documented in the plan's
    (e): a class-keyed `entry_class not in ("clean", "blank")` check wrongly
    assumed `clean` implies value-identity — post-C1 a `clean` outcome can
    carry a REWRITTEN canonical value. The naive alternative,
    `new_value != original`, is ALSO wrong: for a real event line
    `new_value` is the full re-emitted `verb + timestamp + path` string
    while `original` is the path field alone, so that comparison is true
    for every T/R line regardless of whether the path itself changed.
    ``value_changed`` is computed from the PATH-LEVEL comparison
    (`path_outcome.new_value` vs `path_field`) before it is folded into a
    re-emitted event line, so it reflects only the path's own change.
    """
    verb, ts, path_field = parse_touch_event(line)
    path_outcome = classify_touch_entry(path_field, worktree_root)
    value_changed = (
        path_outcome.new_value is None or path_outcome.new_value != path_field
    )
    if ts is None:
        # Legacy/unparseable-as-event line: path_field IS the whole line
        # (parse_touch_event's own fail-safe contract) — rewrite as a bare
        # line, never stamped.
        return path_outcome, value_changed
    if path_outcome.new_value is None:
        new_value = None
    else:
        new_value = format_touch_event(verb, path_outcome.new_value, ts)
    classification = TouchEntryClassification(
        original=path_outcome.original,
        new_value=new_value,
        entry_class=path_outcome.entry_class,
        drop_reason=path_outcome.drop_reason,
    )
    return classification, value_changed


@dataclass
class FileOutcome:
    """Per-file migration result — one per touched.txt processed."""

    path: Path
    outcomes: List[EntryOutcome] = field(default_factory=list)
    changed: bool = False
    read_error: Optional[str] = None

    def counts(self) -> Dict[str, int]:
        c: Dict[str, int] = {name: 0 for name in _CLASSES}
        for o in self.outcomes:
            if o.entry_class in c:
                c[o.entry_class] += 1
        return c


@dataclass
class MigrationReport:
    """Aggregate result of a `run_migration` call (dry-run or apply)."""

    apply: bool
    sessions_base: Path
    worktree_root: Path
    files: List[FileOutcome] = field(default_factory=list)
    backup_dir: Optional[Path] = None
    drop_manifest: List[dict] = field(default_factory=list)
    read_errors: List[str] = field(default_factory=list)

    def totals(self) -> Dict[str, int]:
        totals: Dict[str, int] = {name: 0 for name in _CLASSES}
        for fo in self.files:
            for name, n in fo.counts().items():
                totals[name] += n
        return totals

    def files_changed_count(self) -> int:
        return sum(1 for fo in self.files if fo.changed)


def _iter_touched_files(sessions_base: Path):
    """Yield every touched.txt under ``sessions_base``, EXCLUDING any path
    with a ``.archive`` path component (top-level or nested) — see § Scope.
    Sorted for deterministic report/manifest ordering.
    """
    if not sessions_base.is_dir():
        return
    for path in sorted(sessions_base.rglob("touched.txt")):
        rel = path.relative_to(sessions_base)
        if _EXCLUDED_COMPONENT in rel.parts:
            continue
        yield path


def plan_file(path: Path, worktree_root: Path) -> FileOutcome:
    """Classify every line of ``path`` without writing anything.

    On an unreadable file (``OSError``), returns a ``FileOutcome`` with
    ``read_error`` set to the exception text instead of silently reporting
    it identically to "no changes needed" — see ``MigrationReport.read_errors``.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return FileOutcome(path=path, outcomes=[], changed=False, read_error=str(exc))

    lines = text.splitlines()
    classified = [_classify_line(line, worktree_root) for line in lines]
    outcomes = [c for c, _ in classified]
    # Post-C1: a `clean` outcome can carry a rewritten (canonicalized) value,
    # so the class label alone no longer implies value-identity — see
    # `_classify_line`'s `value_changed` docstring paragraph for why the
    # naive `new_value != original` alternative is also wrong.
    changed = any(value_changed for _, value_changed in classified)
    return FileOutcome(path=path, outcomes=outcomes, changed=changed, read_error=None)


def _rewrite_text(outcomes: List[EntryOutcome]) -> str:
    kept = [o.new_value for o in outcomes if o.new_value is not None]
    if not kept:
        return ""
    return "\n".join(kept) + "\n"


def _default_backup_dir(git_common_dir: Path) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # Dot-prefixed sibling of coordinator-sessions/, deliberately NOT a
    # subdirectory of it — see module docstring's Negative-spec.
    return git_common_dir / f".touched-prefix-migration-backup-{ts}"


def run_migration(
    sessions_base: Path,
    worktree_root: Path,
    *,
    apply: bool = False,
    backup_dir: Optional[Path] = None,
    git_common_dir: Optional[Path] = None,
) -> MigrationReport:
    """Dry-run (default) or apply the touched.txt prefix migration.

    ``sessions_base`` is ``<git-common-dir>/coordinator-sessions``.
    ``worktree_root`` is the CORRECTED worktree root (main_worktree_root's
    return value), used both for the containment check and as the ``cwd``
    passed to ``normalize_touch_path`` for the absolute-rescue class.

    Dry-run (``apply=False``, the default): classifies every entry, writes
    NOTHING, creates no backup. Safe to call against the live corpus.

    Apply (``apply=True``): takes a full backup of every in-scope
    touched.txt FIRST (see ``_default_backup_dir`` / the Negative-spec on
    backup placement), then rewrites each changed file's content through
    ``locked_write.locked_rmw`` — the same lock domain the hook writer
    uses — one file at a time. A file with no changed entries is never
    opened for writing.
    """
    report = MigrationReport(
        apply=apply, sessions_base=sessions_base, worktree_root=worktree_root
    )

    files = list(_iter_touched_files(sessions_base))
    file_outcomes = [plan_file(p, worktree_root) for p in files]
    report.files = file_outcomes
    report.read_errors = [str(fo.path) for fo in file_outcomes if fo.read_error is not None]

    def _rebuild_drop_manifest() -> List[dict]:
        manifest: List[dict] = []
        for fo in file_outcomes:
            for o in fo.outcomes:
                if o.new_value is None:
                    manifest.append(
                        {
                            "path": o.original,
                            "source": str(fo.path),
                            "reason": o.drop_reason,
                            "class": o.entry_class,
                        }
                    )
        return manifest

    # Pre-lock scan estimate — used verbatim for dry-run reporting. Under
    # apply, this is superseded below by a rebuild from the lock-scoped
    # recomputation, since the scan cannot see writes that land between
    # itself and the lock acquisition.
    report.drop_manifest = _rebuild_drop_manifest()

    if not apply:
        return report

    resolved_backup_dir = backup_dir
    if resolved_backup_dir is None:
        if git_common_dir is None:
            raise ValueError(
                "apply=True requires either backup_dir or git_common_dir "
                "(to derive the default backup destination)"
            )
        resolved_backup_dir = _default_backup_dir(git_common_dir)

    to_change = [fo for fo in file_outcomes if fo.changed]

    resolved_backup_dir.mkdir(parents=True, exist_ok=True)
    for fo in to_change:
        rel = fo.path.relative_to(sessions_base)
        dest = resolved_backup_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fo.path, dest)
    report.backup_dir = resolved_backup_dir

    for fo in to_change:
        # Recompute the classification from the LOCK-SCOPED text `locked_rmw`
        # hands us, not from `fo.outcomes` (the pre-lock scan) — a concurrent
        # append landing between the scan and the lock must be classified and
        # preserved, not silently overwritten by a stale pre-computed value.
        # If the file was deleted between scan and lock, `missing_ok=True`
        # hands us "" here; recomputing from "" yields new_text == "" too, so
        # `locked_rmw`'s identical-content skip (old == new) fires and the
        # deletion is left alone rather than the file being resurrected with
        # stale content.
        def _mutate(_old: str, _fo: FileOutcome = fo) -> str:
            lines = _old.splitlines()
            fresh_classified = [_classify_line(line, worktree_root) for line in lines]
            fresh_outcomes = [c for c, _ in fresh_classified]
            _fo.outcomes = fresh_outcomes
            _fo.changed = any(value_changed for _, value_changed in fresh_classified)
            return _rewrite_text(fresh_outcomes)

        locked_write.locked_rmw(
            fo.path, _mutate, repo_root=worktree_root, missing_ok=True
        )

    # The manifest must reflect what was actually dropped under the lock,
    # not the pre-lock scan's prediction — rebuild it now that `to_change`
    # entries' `fo.outcomes` carry the lock-scoped recomputation.
    report.drop_manifest = _rebuild_drop_manifest()

    manifest_path = resolved_backup_dir / "drop-manifest.json"
    manifest_path.write_text(
        json.dumps(report.drop_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return report


def _print_report(report: MigrationReport) -> None:
    totals = report.totals()
    print(
        f"=== migrate_touched_prefix: apply={report.apply} "
        f"sessions_base={report.sessions_base} worktree_root={report.worktree_root} ==="
    )
    print(f"files scanned: {len(report.files)}  files changed: {report.files_changed_count()}")
    if report.read_errors:
        print(f"read errors ({len(report.read_errors)} file(s) could not be read — NOT backed up, NOT migrated):")
        for path_str in report.read_errors:
            print(f"  {path_str}")
    for name in _CLASSES:
        print(f"  {name}: {totals[name]}")
    dropped_total = totals["dropped"]
    print(f"TOTAL DROPPED: {dropped_total}")
    if report.apply:
        print(f"backup written to: {report.backup_dir}")
        print(f"drop manifest: {report.backup_dir / 'drop-manifest.json' if report.backup_dir else '(none)'}")
    else:
        print("(dry-run — nothing written; pass --apply to write, after review)")
        if report.drop_manifest:
            print(f"\n{len(report.drop_manifest)} entries WOULD be dropped:")
            for entry in report.drop_manifest:
                print(f"  [{entry['class']}] {entry['source']}: {entry['path']!r} — {entry['reason']}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "One-time corrective migration for touched.txt '../'/absolute-path "
            "poisoning. Dry-run by default; pass --apply to write."
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
        help="the CORRECTED worktree root (main_worktree_root's return value)",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="write the migration (default: dry-run, writes nothing)",
    )
    ap.add_argument(
        "--backup-dir",
        default=None,
        help="override the default backup destination (apply mode only)",
    )
    args = ap.parse_args(argv)

    sessions_base = Path(args.sessions_base)
    worktree_root = Path(args.worktree_root)
    backup_dir = Path(args.backup_dir) if args.backup_dir else None
    git_common_dir = sessions_base.parent

    report = run_migration(
        sessions_base,
        worktree_root,
        apply=args.apply,
        backup_dir=backup_dir,
        git_common_dir=git_common_dir,
    )
    _print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
