"""
coordinator_core.install.substrate_migrate — one-time idempotent migration of
legacy `~/.claude/{machine-local,settings-manifest.md}` into the coordinator
settings-home.

Copies legacy files into `<settings-home>/`, then replaces
the legacy `~/.claude/machine-local` real directory with a compat pointer
(POSIX symlink or Windows directory junction) so unmigrated direct-binding
consumers (example-game-repo, example-retrieval-repo, example-retrieval-repo-ue-addon) keep resolving TOML
content through the legacy path unchanged.

`setup/` is intentionally NOT migrated here — nothing reads `setup/` from
settings-home at runtime (coordinator continues to read `~/.claude/setup/`),
and `substrate.py`'s own percolation step writes the canonical copy there.
Migrating `setup/` would create two diverging locations that the fail-loud
divergent-file guard would then block on every re-run.

Structural guard, ahead of the per-file walk (`_tracked_file_count`): if the
git repo at `claude_base` TRACKS the legacy `machine-local` directory, this
migration can never converge — its terminal state is that path replaced by a
pointer, and a checkout restores a tracked directory every time. It fails
loud naming that, and does NOT touch the operator's repository: `~/.claude`
being a meta-repo synced across machines is precisely the case that produced
the condition, and untracking it there is an operator decision with
consequences on every other machine.

Per-file guards (mirrors the bash `_copy_one_file` exactly):
    source-present AND destination-file-absent   -> copy (mode-preserving)
    both-present AND identical content           -> no-op (already migrated)
    both-present AND divergent content           -> FAIL LOUD (return 1)
    source-absent                                -> no-op (skip)

Idempotent: once the compat pointer is in place at the legacy path,
`is_pointer()` fires on the outer guard and re-running is a clean no-op.

Reuse (do not re-derive): `coordinator_core.install._shared.is_pointer` for
POSIX-symlink-or-Windows-junction detection; `coordinator_core.install.substrate`'s
`_cygpath_w` / `_quiet_output` / `_run` platform helpers are imported lazily
(function-local import, not module-level) to avoid a circular import — this
module is itself imported by `substrate.py` at module load time, so a
module-level `from coordinator_core.install.substrate import ...` would
deadlock the import graph; the deferred import resolves cleanly because by
the time the pointer-install path actually runs, `substrate` has already
finished importing this module and is fully initialized.

Only the MUTATING copy/move/pointer-install half plus `--dry-run`/check-only
is net-new versus what's already native in `_settings_home.py` /
`substrate.py` — settings-home resolution and whole-tree divergence
pre-checks stay in those modules; this module owns per-file content
divergence (byte compare — a distinct, narrower check, see module
`check_machine_local_divergence` docstring in `_settings_home.py`).

Port backlink: docs/plans/2026-07-17-retire-doe-bash-bridges-native-python.md
    (C1 chunk — Port A).
Spec backlink: DoE-claude:pln-relocate-durable-coordinator-s-d48415 § C3

Negative-spec:
  - Does NOT migrate `setup/` — see header note above.
  - Does NOT untrack, stage, commit, or otherwise mutate any git repository.
    The structural guard above reports; the operator decides.
  - Does NOT resolve settings-home itself — callers pass `settings_home_path`
    (from `coordinator_core._settings_home.settings_home()`), no second
    resolver is introduced here.
  - Does NOT read `os.environ` — all configuration (claude_base,
    settings_home_path, check_only) is threaded in as explicit parameters
    from the call site's own locals (AC G5).
"""

from __future__ import annotations

import filecmp
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core.git.run import run_git
from coordinator_core.install._shared import is_pointer
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.install.write_surface import (
    ShapedClause,
    StaticClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)

_MIGRATE_TREE_CLAUSE_INDEX = 1
"""Index of the SHAPED clause (`_migrate_tree`'s per-file copy) within
`WRITE_SURFACE.clauses` below — the clause `record_resolution` calls in
`migrate_substrate_to_settings_home` journal against. Read off the
declaration's own comment, not re-derived, since a future reordering of the
four clauses would otherwise silently desync this constant."""

LEGACY_MACHINE_LOCAL_DIRNAME = "machine-local"
"""The legacy real directory this module migrates and then replaces with a
compat pointer: `<claude_base>/machine-local`. A module-level constant so
`migrate_substrate_to_settings_home` and `WRITE_SURFACE` read one spelling
rather than each carrying their own literal."""

LEGACY_MANIFEST_FILENAME = "settings-manifest.md"
"""The legacy single-file migration target: `<claude_base>/settings-manifest.md`,
copied (not moved) to `<settings_home>/settings-manifest.md`."""


def _tracked_file_count(claude_base: Path, dirname: str) -> Optional[int]:
    """How many files the git repo at `claude_base` TRACKS under `dirname`,
    or None when `claude_base` is not a git work tree (or git cannot answer).

    Why this exists: this migration's terminal state is `dirname` replaced by
    a pointer to settings-home. A git repo that TRACKS `dirname` re-materialises
    it as a real directory on every checkout and sync, so the terminal state is
    unreachable by construction -- the migration does not "fail this run", it
    fails every run, and the per-file divergence it reports is a symptom of
    that, not the cause. `~/.claude` being a git-synced meta-repo across
    machines is the shape that produced it (observed 2026-08-22).

    One spawn, and only on a box that has not converged: the sole caller is
    already inside the `legacy dir is a real directory` branch, which stops
    firing the moment the pointer is in place. The `.git` probe short-circuits
    the spawn entirely on the ordinary box where `claude_base` is not a repo.

    Never raises: git being absent, slow, or unhappy is reported as "cannot
    establish", and the caller degrades to the per-file message it had before.
    """
    if not (claude_base / ".git").exists():
        return None
    # `run_git` never raises for a git-side failure, so the try/except this
    # replaces has nothing left to catch: absent git, a timeout and a non-zero
    # exit all arrive as a result whose `.ok` is False, which is the single
    # "cannot establish" answer this function already folded all three onto.
    result = run_git(["-C", str(claude_base), "ls-files", "--", dirname])
    if not result.ok:
        return None
    return len([line for line in result.stdout.splitlines() if line.strip()])


def _unconvergeable_message(claude_base: Path, legacy_ml: Path, tracked: int) -> str:
    """Guard-messaging register (`docs/wiki/guard-messaging.md` § Register):
    the structural fact once, then one alternative. No apology, no
    restatement of the per-file symptom -- the caller prints that only when
    there is no structural cause to print instead."""
    return (
        "migrate-substrate-to-settings-home: cannot converge — "
        f"{legacy_ml} is tracked by the git repo at {claude_base} "
        f"({tracked} tracked files).\n"
        "  This migration ends by replacing that directory with a pointer to "
        "settings-home; a tracked directory is restored by every checkout, so "
        "no run of this migration can reach that state.\n"
        f"  Remediation: stop tracking {LEGACY_MACHINE_LOCAL_DIRNAME}/ in "
        f"{claude_base} (untrack it and add an ignore rule), then re-run. "
        "This migration will not do that for you: that repo syncs to other "
        "machines."
    )


def _copy_one_file(src: Path, dst: Path, check_only: bool) -> int:
    """Mirror bash `_copy_one_file`. Returns 0 (ok / no-op) or 1 (divergent,
    fail loud — caller must not migrate anything further)."""
    if not src.is_file():
        return 0

    if not dst.is_file():
        if check_only:
            print(
                f"[migrate] check failed: {dst} is absent (would copy from {src})",
                file=sys.stderr,
            )
            return 1
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)  # mode+timestamp-preserving, mirrors `cp -p`
        print(f"[migrate] copied {src} → {dst}")
        return 0

    if filecmp.cmp(src, dst, shallow=False):
        return 0

    print(
        "migrate-substrate-to-settings-home: DIVERGENT FILE — cannot safely migrate.",
        file=sys.stderr,
    )
    print(f"  Source : {src}", file=sys.stderr)
    print(f"  Dest   : {dst}", file=sys.stderr)
    print("  Both files exist and have different content.", file=sys.stderr)
    print("  Remediation: manually reconcile the two files, then re-run the migration.", file=sys.stderr)
    return 1


def _migrate_tree(
    src_dir: Path, dst_dir: Path, check_only: bool, performed: Optional[List[WriteSurfaceEntry]] = None
) -> int:
    """Mirror bash `_migrate_tree`: copy every regular file (dotfiles
    included, `find -type f` equivalent), then create empty destination
    subdirs that a files-only walk would silently skip.

    `performed`, when given, is appended to (never replaced) with one
    `WriteSurfaceEntry` per file actually copied or empty subdir actually
    created — i.e. only the writes this call genuinely performed, in the
    order performed, including any that landed before a later divergent
    file made this call return non-zero. `check_only` never appends (a
    check-only pass performs no writes to journal). This is the concrete
    resolution `migrate_substrate_to_settings_home` journals for this
    clause via `resolution_journal.record_resolution` — see that
    function's own docstring."""
    if not src_dir.is_dir():
        return 0

    for dirpath, dirnames, filenames in os.walk(src_dir):
        dirnames.sort()
        for fname in sorted(filenames):
            src_f = Path(dirpath) / fname
            rel = src_f.relative_to(src_dir)
            dst_f = dst_dir / rel
            rc = _copy_one_file(src_f, dst_f, check_only)
            if rc != 0:
                return rc
            if performed is not None and not check_only:
                performed.append(
                    WriteSurfaceEntry(
                        kind="file-path",
                        path=str(dst_f),
                        reason="_migrate_tree: per-file copy or empty-subdir creation, mirroring the legacy tree shape",
                    )
                )

    for dirpath, dirnames, filenames in os.walk(src_dir):
        for dname in sorted(dirnames):
            sub = Path(dirpath) / dname
            try:
                sub_is_empty = not any(sub.iterdir())
            except OSError:
                # Conservative default: an unreadable subdir is treated as
                # non-empty so this walk never mkdirs over it.
                sub_is_empty = False
            if not sub_is_empty:
                continue
            rel = sub.relative_to(src_dir)
            dst_sub = dst_dir / rel
            if dst_sub.is_dir():
                continue
            if check_only:
                print(
                    f"[migrate] check failed: empty subdir {dst_sub} is absent "
                    f"(would create from {sub})",
                    file=sys.stderr,
                )
                return 1
            dst_sub.mkdir(parents=True, exist_ok=True)
            print(f"[migrate] created empty subdir {dst_sub}")
            if performed is not None:
                performed.append(
                    WriteSurfaceEntry(
                        kind="file-path",
                        path=str(dst_sub),
                        reason="_migrate_tree: per-file copy or empty-subdir creation, mirroring the legacy tree shape",
                    )
                )

    return 0


def _install_compat_pointer(legacy_ml: Path, dst_ml: Path, check_only: bool) -> int:
    """Replace the legacy real `machine-local/` directory with a POSIX
    symlink (Linux/macOS) or Windows directory junction, pointing at
    `dst_ml`. Returns 0 on success/dry-run, 1 on a fail-loud platform error
    (cygpath absent, mklink /J failure)."""
    # Deferred import — see module docstring for why this can't be module-level.
    from coordinator_core.install import substrate as _substrate_mod

    platform = _substrate_mod._quiet_output(["uname", "-s"])
    is_windows = platform.startswith(("MINGW", "MSYS", "CYGWIN"))

    if check_only:
        kind = "junction" if is_windows else "symlink"
        # Reached only when the caller (`migrate_substrate_to_settings_home`)
        # has already established `legacy_ml` is a real directory, not yet a
        # pointer — so this branch is always a genuine stale/not-yet-migrated
        # state, never a false positive. Fail loud rather than return 0, per
        # the fail-loud-on-stale-or-absent contract for install-time legs.
        print(
            f"[migrate] check failed: {legacy_ml} is a real dir, not yet a {kind} → {dst_ml}",
            file=sys.stderr,
        )
        return 1

    if is_windows:
        if not shutil.which("cygpath"):
            print(
                "migrate-substrate-to-settings-home: FATAL — cygpath not found on Windows host.",
                file=sys.stderr,
            )
            print("  cygpath is required to convert POSIX paths to Windows paths for mklink /J.", file=sys.stderr)
            print(
                "  Remediation: ensure cygpath is on PATH (provided by MSYS2, Cygwin, or Git-for-Windows).",
                file=sys.stderr,
            )
            return 1
        win_legacy = _substrate_mod._cygpath_w(str(legacy_ml))
        win_dst = _substrate_mod._cygpath_w(str(dst_ml))
        # Review: coordinator:code-reviewer — this rmdir/rmtree is a genuine
        # removal of a real directory and its contents; gate it the same way
        # substrate.py's analogous delete legs do, above the mutating call.
        blocked = _substrate_mod._refuse_machine_mutation(
            str(legacy_ml), what=f"remove legacy machine-local directory {legacy_ml}", check_temp_path=False,
        )
        if blocked:
            print(f"[migrate] REFUSED: {blocked}", file=sys.stderr)
            return 1
        try:
            legacy_ml.rmdir()
        except OSError:
            shutil.rmtree(legacy_ml, ignore_errors=True)
        proc = _substrate_mod._run(["cmd", "/c", "mklink", "/J", win_legacy, win_dst], capture_output=True)
        if proc.returncode != 0:
            print("migrate-substrate-to-settings-home: FATAL — mklink /J failed.", file=sys.stderr)
            print(f"  Link path: {win_legacy}", file=sys.stderr)
            print(f"  Target   : {win_dst}", file=sys.stderr)
            print("  Remediation: ensure cmd.exe is reachable and the target is a local NTFS path.", file=sys.stderr)
            print("  Note: mklink /J does not require elevation or Developer Mode.", file=sys.stderr)
            return 1
        print(f"[migrate] installed compat junction: {legacy_ml} → {dst_ml}")
    else:
        # Review: coordinator:code-reviewer — same gate on the POSIX branch's
        # real-directory rmtree, above the mutating call.
        blocked = _substrate_mod._refuse_machine_mutation(
            str(legacy_ml), what=f"remove legacy machine-local directory {legacy_ml}", check_temp_path=False,
        )
        if blocked:
            print(f"[migrate] REFUSED: {blocked}", file=sys.stderr)
            return 1
        shutil.rmtree(legacy_ml, ignore_errors=True)
        legacy_ml.symlink_to(dst_ml)
        print(f"[migrate] installed compat symlink: {legacy_ml} → {dst_ml}")

    return 0


def migrate_substrate_to_settings_home(
    claude_base: Path,
    settings_home_path: Path,
    check_only: bool = False,
) -> int:
    """One-time idempotent migration of legacy `<claude_base>/{machine-local,
    settings-manifest.md}` into `<settings_home_path>/`, then compat-pointer
    the legacy `machine-local` path at `<claude_base>/machine-local`.

    ``claude_base`` and ``settings_home_path`` are explicit parameters
    derived from the call site's own locals (AC G5) — this function never
    reads `os.environ` itself. Returns 0 on success (including the
    already-migrated/nothing-to-migrate no-op case), 1 on a fail-loud
    per-file content divergence or platform pointer-install error.
    """
    legacy_ml = claude_base / LEGACY_MACHINE_LOCAL_DIRNAME
    legacy_manifest = claude_base / LEGACY_MANIFEST_FILENAME
    dst_ml = settings_home_path / LEGACY_MACHINE_LOCAL_DIRNAME
    dst_manifest = settings_home_path / LEGACY_MANIFEST_FILENAME

    # Review: code-reviewer (Finding 7) — renamed from `any_work`: this flag
    # tracks whether a legacy migration *candidate* was found, not whether
    # any bytes actually moved (a both-present-identical no-op still sets it).
    any_source_present = False

    if legacy_ml.is_dir() and not is_pointer(legacy_ml):
        # Structural check BEFORE the per-file walk, not after it. A tracked
        # legacy directory makes the terminal state unreachable whether or not
        # any single file happens to diverge, and the divergent-file report
        # that fired here on 2026-08-22 named a symptom the operator could
        # reconcile by hand forever without the migration ever converging.
        tracked = _tracked_file_count(claude_base, LEGACY_MACHINE_LOCAL_DIRNAME)
        if tracked:
            print(_unconvergeable_message(claude_base, legacy_ml, tracked), file=sys.stderr)
            return 1

        performed: List[WriteSurfaceEntry] = []
        rc = _migrate_tree(legacy_ml, dst_ml, check_only, performed)
        # Journal what `_migrate_tree` actually performed, regardless of
        # `rc` — a divergent-file abort partway through still leaves
        # earlier copies/mkdirs genuinely on disk (see `_migrate_tree`'s
        # own docstring), and those are real facts to record. `check_only`
        # never appends to `performed`, so a check-only pass journals
        # nothing here — it performed no writes to journal.
        if not check_only:
            # Deferred import — this module is itself imported by
            # `substrate.py` at module load time, and `resolution_journal`
            # back-imports `uninstall_legs`, which back-imports `substrate`
            # — a module-level import here would close that cycle at load
            # time (same reasoning as this module's own deferred
            # `substrate` back-import; see module docstring).
            from coordinator_core.install import resolution_journal

            resolution_journal.record_resolution(
                "substrate-migrate", _MIGRATE_TREE_CLAUSE_INDEX, performed
            )
        if rc != 0:
            return rc
        any_source_present = True
    elif not check_only:
        # Discovery already determined there is nothing to migrate (no
        # legacy dir, or already replaced by a compat pointer) — a genuine
        # "resolved to nothing" fact, not "we never got there".
        from coordinator_core.install import resolution_journal

        resolution_journal.record_resolution(
            "substrate-migrate", _MIGRATE_TREE_CLAUSE_INDEX, ()
        )

    # setup/ is NOT migrated here — see module docstring.

    if legacy_manifest.is_file():
        rc = _copy_one_file(legacy_manifest, dst_manifest, check_only)
        if rc != 0:
            return rc
        any_source_present = True

    # Review: code-reviewer (Finding 1, AC A5) — gate on "would dst_ml exist
    # after a real run" (legacy_ml present + not already a pointer), not on
    # dst_ml's current on-disk state: in dry-run, _migrate_tree never writes
    # dst_ml, so the dst_ml.is_dir() conjunct silently skipped the
    # would-replace report on the common fresh-tree dry run.
    if legacy_ml.is_dir() and not is_pointer(legacy_ml) and (check_only or dst_ml.is_dir()):
        rc = _install_compat_pointer(legacy_ml, dst_ml, check_only)
        if rc != 0:
            return rc

    if not any_source_present:
        print("[migrate] nothing to migrate (no legacy source dirs/files present, or already migrated)")

    return 0


WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="substrate-migrate",
    source_module="coordinator_core.install.substrate_migrate",
    clauses=(
        # Clause 1 — `_copy_one_file` copies the single legacy manifest
        # file into the settings home. A COPY, not a move: the legacy
        # source file is left in place, so this clause has no delete
        # counterpart.
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    path=f"<settings_home>/{LEGACY_MANIFEST_FILENAME}",
                    reason="_copy_one_file: copies legacy settings-manifest.md into settings-home",
                ),
            ),
        ),
        # Clause 2 — `_migrate_tree` walks the legacy machine-local
        # directory and copies (mode-preserving, `shutil.copy2`) every
        # regular file, plus creates any empty subdirs a files-only walk
        # would skip. SHAPED, not enumerable in source — the file set is
        # whatever exists under the legacy tree at migration time. A
        # COPY, not a move: legacy files are left in place by this
        # clause (see clause 4 for the subsequent removal of the legacy
        # directory itself, once it is replaced by the compat pointer).
        ShapedClause(
            discovered_by="_migrate_tree",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path=f"<settings_home>/{LEGACY_MACHINE_LOCAL_DIRNAME}/<relative-path>",
                reason="_migrate_tree: per-file copy or empty-subdir creation, mirroring the legacy tree shape",
            ),
        ),
        # Clause 3 — `_install_compat_pointer` replaces the legacy
        # machine-local directory with a compat pointer at the SAME
        # legacy path, so unmigrated direct-binding consumers keep
        # resolving through `<claude_base>/machine-local` unchanged. Two
        # entries, not one — a POSIX symlink (`Path.symlink_to`) and a
        # Windows directory junction (`mklink /J`) are different
        # artifacts created by different platform branches, and
        # collapsing them would lose which one a given machine got.
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    path=f"<claude_base>/{LEGACY_MACHINE_LOCAL_DIRNAME}",
                    reason=(
                        "_install_compat_pointer (POSIX branch): "
                        "legacy_ml.symlink_to(dst_ml)"
                    ),
                ),
                WriteSurfaceEntry(
                    kind="file-path",
                    path=f"<claude_base>/{LEGACY_MACHINE_LOCAL_DIRNAME}",
                    reason=(
                        "_install_compat_pointer (Windows branch): "
                        "cmd /c mklink /J <legacy> <settings-home target>"
                    ),
                ),
            ),
        ),
        # Clause 4 — DELETE: before either pointer variant is installed,
        # `_install_compat_pointer` removes the legacy real directory at
        # the same path (`legacy_ml.rmdir()` falling back to
        # `shutil.rmtree`, or an unconditional `shutil.rmtree` on the
        # Windows branch). This is a genuine removal of a real directory
        # and its contents, not "just a move" — the tree's bytes were
        # already copied out by clause 2, but the legacy directory itself
        # is deleted here, in place, ahead of the pointer write.
        StaticClause(
            effect="delete",
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    path=f"<claude_base>/{LEGACY_MACHINE_LOCAL_DIRNAME}",
                    effect="delete",
                    reason=(
                        "_install_compat_pointer: removes the legacy real "
                        "machine-local directory (rmdir/rmtree) immediately "
                        "before installing the compat symlink/junction at "
                        "the same path; gated by `_refuse_machine_mutation`"
                    ),
                ),
            ),
        ),
    ),
)
