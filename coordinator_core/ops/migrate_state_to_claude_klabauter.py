"""
coordinator_core.ops.migrate_state_to_claude_klabauter — two-phase state/docs migration
from ~/.claude (CLAUDE_HOME) to the claude-klabauter sibling repo (CLAUDE_KLABAUTER_ROOT).

Purpose: DR-059 bash-to-naked-Python port of the example-doctrine-repo-owned script
`coordinator/bin/migrate-state-to-claude-klabauter.sh` (~346 lines). Implements the
two-phase cross-repo excision contract per cleanup-sweep-hazards.md §10 and
the populate-before-repoint live-safety sequence from the C6 plan (PM-directed
2026-07-03 re-sequence).

Two subcommands:

  --populate (C6a)
    COPY (do NOT remove) CLAUDE_HOME's state/ and docs/{plans,research,problems}/
    (plus archive/ — see Negative-spec below) trees into CLAUDE_KLABAUTER_ROOT's matching
    trees. Preserves directory structure and file modes (`shutil.copy2`,
    mirroring the bash oracle's `cp -p`). Idempotent: safe to re-run; existing
    destination files are overwritten.
    Originals stay UNTOUCHED.

  --finalize (C6b)
    1. Delta re-sync: copies any CLAUDE_HOME source file NEWER than its claude-klabauter
       counterpart (captures writes that happened after --populate).
    2. Per-path guard: before removing ANY source file, confirms the claude-klabauter
       destination copy exists. Fails loud on any missing destination.
    3. Removes CLAUDE_HOME originals for all source trees.
    DO NOT RUN until after: C3/C4/C12 repoint + C11 dogfood consistency gate.

Spec backlink: docs/plans/2026-07-03-stop-the-rot-claude-klabauter-state-home-placement.md
               § Phase 2 / C6 / AC5 / § Execution Notes (C6a/C6b split)
               docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md
Port of: migrate-state-to-claude-klabauter.sh (example-doctrine-repo b5a4192c, 2026-07-20)

Exit codes (parity-critical, preserved from the bash oracle):
    0 — success (populate complete, or finalize complete)
    1 — business error: no subcommand / unknown subcommand, CLAUDE_HOME not
        found, or (--finalize only) the pre-removal guard found source files
        with no confirmed claude-klabauter copy (fail-closed; nothing was removed)

Cross-platform: pure `os`/`shutil`/`pathlib` — no subprocess, no GNU-isms, no
POSIX-only assumptions. `shutil.copy2` preserves mode + mtime like the bash
oracle's `cp -p`, without any temp-file-and-replace step (so no separate
chmod-before-replace is needed — see docs/wiki, addendum item 5, N/A here).

Negative-spec (faithfully reproduced bash-oracle quirks -- do NOT "fix" these
mid-port; behavior-parity with the retired .sh is the contract):
    - `TREE_PAIRS` includes an `archive/` → `archive/` pair. The bash oracle's
      own header-comment prose only advertises "state/ + docs/{plans,research,
      problems}/" — `archive/` is present in the oracle's `TREE_PAIRS` array
      but never mentioned in its header. This module reproduces the ARRAY
      (the actual behavior), not the header prose (a pre-existing doc/code
      drift in the oracle, not a bug in the copy logic itself).
    - The bash oracle declared an unused `total_files` accumulator in
      `cmd_populate` (incremented nowhere, read nowhere) — a dead local, not
      reproduced here since it has zero observable effect on stdout/exit code.
    - Unknown-subcommand usage output is intentionally SHORTER than the
      no-args usage output: the bash oracle's `case ... *)` branch prints
      only the one-line `Usage: ...` string, not the full --populate/
      --finalize help block that the `$# -eq 0` branch prints. Reproduced
      verbatim (see `main`) — do not "fix" this into a single shared
      `_usage()` call for both branches.
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import List, TextIO, Tuple

from coordinator_core.session.declared_writes import declare_write

# Relative-to-CLAUDE_HOME / CLAUDE_KLABAUTER_ROOT tree pairs mirrored (source, dest) —
# see Negative-spec above re: archive/ inclusion.
TREE_PAIRS: List[Tuple[str, str]] = [
    ("state", "state"),
    ("archive", "archive"),
    ("docs/plans", "docs/plans"),
    ("docs/research", "docs/research"),
    ("docs/problems", "docs/problems"),
]


def _walk_files(root: str) -> List[str]:
    """Enumerate all files under `root`, mirroring the bash oracle's
    `find <root> -type f` (does not follow symlinked directories, matching
    `os.walk`'s default `followlinks=False`).
    """
    out: List[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            out.append(os.path.join(dirpath, name))
    return out


def copy_tree(src: str, dst: str, out: TextIO) -> None:
    """Copy every file under `src` into the matching relative path under
    `dst`, preserving mode + mtime (`shutil.copy2`, the `cp -p` analogue).
    Idempotent: existing destination files are simply overwritten.
    """
    if not os.path.isdir(src):
        print(f"  SKIP (source absent): {src}", file=out)
        return

    os.makedirs(dst, exist_ok=True)

    file_count = 0
    skip_count = 0

    for src_file in _walk_files(src):
        rel = os.path.relpath(src_file, src)
        dst_file = os.path.join(dst, rel)
        dst_parent = os.path.dirname(dst_file)
        if dst_parent:
            os.makedirs(dst_parent, exist_ok=True)

        try:
            shutil.copy2(src_file, dst_file)
            file_count += 1
            # DR-276: declared AFTER the copy lands, never before — the
            # contract is a report of what was ACTUALLY written.
            declare_write(dst_file)
        except OSError:
            print(f"  WARN: failed to copy: {src_file}", file=sys.stderr)
            skip_count += 1

    print(f"  Copied {file_count} files from {src} → {dst} ({skip_count} failed)", file=out)


def cmd_populate(claude_home: str, claude_klabauter_root: str, out: TextIO) -> int:
    """--populate (C6a): copy source trees to claude-klabauter; originals untouched."""
    print("=== migrate-state-to-claude-klabauter --populate (C6a) ===", file=out)
    print(f"  Source (CLAUDE_HOME): {claude_home}", file=out)
    print(f"  Destination (CLAUDE_KLABAUTER_ROOT): {claude_klabauter_root}", file=out)
    print("", file=out)

    for src_rel, dst_rel in TREE_PAIRS:
        src = os.path.join(claude_home, src_rel)
        dst = os.path.join(claude_klabauter_root, dst_rel)

        print(f"[populate] {src_rel} → {dst_rel}", file=out)
        copy_tree(src, dst, out)
        print("", file=out)

    print(f"=== populate complete — originals UNTOUCHED in {claude_home} ===", file=out)
    print("    Next: run C3/C4/C12 repoint, then C11 dogfood gate, then --finalize", file=out)
    return 0


def cmd_finalize(claude_home: str, claude_klabauter_root: str, out: TextIO) -> int:
    """--finalize (C6b): delta re-sync, verify, then remove source originals.

    Two-phase cross-repo excision per cleanup-sweep-hazards.md §10: Phase 1
    (--populate) already ran and populated the destination. Phase 2 (this
    step): verify destination, then remove source.
    """
    print("=== migrate-state-to-claude-klabauter --finalize (C6b) ===", file=out)
    print(f"  Source (CLAUDE_HOME): {claude_home}", file=out)
    print(f"  Destination (CLAUDE_KLABAUTER_ROOT): {claude_klabauter_root}", file=out)
    print("", file=out)

    # --- Step 1: Delta re-sync — copy any source file NEWER than its claude-klabauter
    # copy (or whose claude-klabauter copy is missing). Captures writes that happened
    # between --populate and now.
    print("--- Step 1: Delta re-sync ---", file=out)

    for src_rel, dst_rel in TREE_PAIRS:
        src = os.path.join(claude_home, src_rel)
        dst = os.path.join(claude_klabauter_root, dst_rel)

        if not os.path.isdir(src):
            print(f"  SKIP (source absent): {src}", file=out)
            continue

        print(f"[delta] {src_rel} → {dst_rel}", file=out)
        delta_count = 0

        for src_file in _walk_files(src):
            rel = os.path.relpath(src_file, src)
            dst_file = os.path.join(dst, rel)
            dst_parent = os.path.dirname(dst_file)

            newer_or_missing = (not os.path.isfile(dst_file)) or (
                os.path.getmtime(src_file) > os.path.getmtime(dst_file)
            )
            if newer_or_missing:
                if dst_parent:
                    os.makedirs(dst_parent, exist_ok=True)
                try:
                    shutil.copy2(src_file, dst_file)
                    delta_count += 1
                    # DR-276: declared AFTER the copy lands, never before.
                    declare_write(dst_file)
                except OSError:
                    print(f"  WARN: delta copy failed: {src_file}", file=sys.stderr)

        print(f"  Delta-synced {delta_count} files from {src}", file=out)
        print("", file=out)

    # --- Step 2: Pre-removal verification guard — verify EVERY source file
    # is confirmed in claude-klabauter before removing ANYTHING. Fail loud on any
    # missing destination (cleanup-sweep-hazards.md §10).
    print("--- Step 2: Pre-removal verification guard ---", file=out)

    missing_count = 0

    for src_rel, dst_rel in TREE_PAIRS:
        src = os.path.join(claude_home, src_rel)
        dst = os.path.join(claude_klabauter_root, dst_rel)

        if not os.path.isdir(src):
            continue

        for src_file in _walk_files(src):
            rel = os.path.relpath(src_file, src)
            dst_file = os.path.join(dst, rel)
            if not os.path.isfile(dst_file):
                print(f"  GUARD FAIL: claude-klabauter copy missing for: {src_file}", file=sys.stderr)
                print(f"             expected at: {dst_file}", file=sys.stderr)
                missing_count += 1

    if missing_count > 0:
        print("", file=sys.stderr)
        print(f"ERROR: {missing_count} source file(s) have no confirmed claude-klabauter copy.", file=sys.stderr)
        print("  Aborting --finalize. Run --populate again to re-sync missing files,", file=sys.stderr)
        print("  then retry --finalize.", file=sys.stderr)
        return 1

    print("  Guard PASSED: all source files confirmed present in claude-klabauter.", file=out)
    print("", file=out)

    # --- Step 3: Remove source originals from CLAUDE_HOME. Removes files
    # first, then cleans up empty directories left behind.
    print("--- Step 3: Remove source originals ---", file=out)

    for src_rel, _dst_rel in TREE_PAIRS:
        src = os.path.join(claude_home, src_rel)

        if not os.path.isdir(src):
            print(f"  SKIP (source absent): {src}", file=out)
            continue

        print(f"[remove] {src_rel}", file=out)
        remove_count = 0

        for src_file in _walk_files(src):
            try:
                os.remove(src_file)
                remove_count += 1
            except OSError:
                print(f"  WARN: failed to remove: {src_file}", file=sys.stderr)

        print(f"  Removed {remove_count} files from {src}", file=out)

        # Clean up empty directories left behind (depth-first, mirroring the
        # bash oracle's `find -type d -empty -delete`).
        for dirpath, _dirnames, _filenames in os.walk(src, topdown=False):
            try:
                if not os.listdir(dirpath):
                    os.rmdir(dirpath)
            except OSError:
                print(f"skip: cmd_finalize: if not os.listdir(dirpath): failed: {sys.exc_info()[1]}", file=sys.stderr)
                pass
        print(f"  Cleaned empty directories under {src}", file=out)
        print("", file=out)

    print("=== finalize complete ===", file=out)
    print(f"    Source trees removed from {claude_home}.", file=out)
    print(f"    Working data now lives in {claude_klabauter_root}.", file=out)
    return 0


def _default_claude_klabauter_root() -> str:
    """This module's own repo root — coordinator_core/ops/<file>.py is two
    directories below the repo root (ops/, coordinator_core/), so
    parents[2] is the repo root itself. Used as CLAUDE_KLABAUTER_ROOT unless overridden.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.dirname(here))


def _usage(out: TextIO) -> None:
    print("Usage: migrate-state-to-claude-klabauter.sh --populate | --finalize", file=out)
    print("", file=out)
    print("  --populate   Copy ~/.claude working data to claude-klabauter (C6a).", file=out)
    print("               Safe to re-run. Originals stay untouched.", file=out)
    print("  --finalize   Delta re-sync + remove ~/.claude originals (C6b).", file=out)
    print("               Run ONLY after: C3/C4/C12 repoint + C11 dogfood gate.", file=out)


def main(argv: List[str]) -> int:
    """CLI entry point.

    Environment overrides (mirror the bash oracle):
      CLAUDE_KLABAUTER_ROOT  — override the claude-klabauter repo path (default: this module's
                     own repo root, see `_default_claude_klabauter_root`).
      CLAUDE_HOME  — override the ~/.claude meta-repo path (default:
                     `~/.claude`).
    """
    if not argv:
        _usage(sys.stderr)
        return 1

    subcommand = argv[0]
    if subcommand not in ("--populate", "--finalize"):
        # Bash oracle prints only the short one-line usage here (NOT the full
        # --populate/--finalize help block, which is reserved for the
        # no-args case above) — reproduced faithfully, not "fixed" to match.
        print(f"ERROR: unknown subcommand: {subcommand}", file=sys.stderr)
        print("Usage: migrate-state-to-claude-klabauter.sh --populate | --finalize", file=sys.stderr)
        return 1

    # Negative-spec: the HOME rung is load-bearing, not redundant with the
    # expanduser terminal. Drop it and a harness that overrides HOME without
    # also setting USERPROFILE falls through to the real machine home, with
    # no error -- the isolated-env test shape this repo uses everywhere.
    claude_home = os.environ.get("CLAUDE_HOME") or os.path.join(
        os.environ.get("HOME") or os.environ.get("USERPROFILE") or os.path.expanduser("~"),
        ".claude",
    )
    if not os.path.isdir(claude_home):
        print(f"ERROR: CLAUDE_HOME not found at: {claude_home}", file=sys.stderr)
        return 1

    claude_klabauter_root = os.environ.get("CLAUDE_KLABAUTER_ROOT") or _default_claude_klabauter_root()
    claude_klabauter_root = claude_klabauter_root.rstrip("/")

    if subcommand == "--populate":
        return cmd_populate(claude_home, claude_klabauter_root, sys.stdout)
    return cmd_finalize(claude_home, claude_klabauter_root, sys.stdout)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
