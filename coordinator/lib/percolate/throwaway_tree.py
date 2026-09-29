"""coordinator/lib/percolate/throwaway_tree.py — DR-445's assembly step:
materialize the dest repo's committed HEAD as a REAL git repo in a
throwaway directory OUTSIDE the dest repo, overlay every row's staging
output onto its working tree, apply declared deletions, and hand back the
resulting repo for gates to run against — never the dest's own
index/worktree.

Spec: docs/decisions/DR-445-publish-assembles-in-a-throwaway-and-moves-once.md

## Why a local clone, not `git archive` and not `git worktree`

Six end-of-run gates (`argv_parity` and others) need a live `git status`
and a HEAD in the tree they run against — a `.git`-less directory (this
module's first cut, `git archive` + `tarfile`) cannot host them at all.
`git worktree` is banned fleet-wide (`CLAUDE.md`) and is not used here
either: a worktree shares the dest's `.git` (its `HEAD`, its ref
namespace) rather than owning an independent one, which is exactly the
coupling-to-the-live-engine DR-445 exists to sever.

`git clone --local --no-checkout <dest> <tmp>` is the middle path:
`--local` hardlinks the OBJECT STORE (git's own default for a same-
filesystem local clone — no separate flag needed; `--no-hardlinks` would
force a copy instead and is deliberately NOT passed) cheaply, and
produces an entirely independent `.git` — its own `HEAD`, its own index,
its own ref namespace, no link back to the dest's. `--no-checkout` means the
clone's working tree starts EMPTY; `git -C <tmp> checkout -q HEAD`
(second spawn) populates it from the clone's own HEAD, which was set from
the dest's HEAD at clone time — never the dest's worktree or index. A
dirty dest worktree, if any, is invisible to this function by
construction: cloning reads only what the dest's own `.git` object store
and refs contain, never its worktree.

Two git spawns per `build_throwaway_tree` call (budget, per dispatch
brief): `clone` then `checkout`. No other git process runs — overlay and
deletion are pure filesystem operations on the clone's working tree, and
`git -C <tmp> status` afterward (run by a CALLER, not this module) shows
exactly those changes against the clone's own HEAD.

## Overlay contract

`overlays` is `[(staging_dir, dest_relative_path), ...]` — one entry per
row. Each overlay REPLACES the subtree at `<tmp>/<dest_relative_path>` to
match `staging_dir` exactly: files present in the HEAD materialization but
absent from `staging_dir` at that path are removed, not left stale. This is
why the overlay is `rmtree`-then-copy, not a merge — a row's staging output
is that row's authoritative content for its own subtree, and a file the row
deleted from its own tree must not survive as a HEAD leftover.

`deletions` are dest-relative paths (files or directories) removed from the
throwaway tree after every overlay lands — declared drops that no row's
staging output speaks for at all.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


class ThrowawayTreeError(RuntimeError):
    """Raised when `git clone` or `git checkout` fails. Never swallowed —
    a caller that cannot materialize HEAD must not proceed as though it
    had an assembled tree to gate."""


def _rmtree_clear_readonly_onerror(func, path, exc_info) -> None:
    """Matches `coordinator/bin/publish.py :: _rmtree_clear_readonly_
    onerror` byte-for-byte in behavior (not imported from there — that
    module is a peer chunk's concurrent-edit surface, not a shared
    library this module may reach into mid-rewrite). `onerror`, not
    `onexc`: this repo's floor is Python 3.11 (`pyproject.toml`), and
    `onexc` is 3.12+."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def _overlay_root(tree: Path, staging_dir: Path) -> None:
    """Mirror `publish.py :: _swap_publish_staging_into_dest_root` onto the
    throwaway: replace each top-level entry the root row staged (never
    `.git`), and drop top-level files the row removed. Trap: a root row's
    staging dir is a full copy of the dest, so a wholesale replace would
    delete `.git` and revert every sibling row."""
    staged = {p.name for p in staging_dir.iterdir()}
    for entry in staging_dir.iterdir():
        if entry.name == ".git":
            continue
        target = tree / entry.name
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target, onerror=_rmtree_clear_readonly_onerror)
        elif target.exists() or target.is_symlink():
            target.unlink()
        if entry.is_dir() and not entry.is_symlink():
            shutil.copytree(entry, target, symlinks=True)
        else:
            shutil.copy2(entry, target, follow_symlinks=False)
    for existing in tree.iterdir():
        if existing.name != ".git" and existing.is_file() and existing.name not in staged:
            existing.unlink()


def build_throwaway_tree(
    dest_repo_root: Path,
    overlays: "list[tuple[Path, Path]]",
    deletions: "list[str]",
) -> Path:
    """Materialize `dest_repo_root`'s committed HEAD as a real, independent
    git repo into a fresh temp directory OUTSIDE `dest_repo_root`
    (`tempfile.mkdtemp`, default temp root — never a subdirectory of the
    dest, so a caller enumerating the dest's own tree never sees it), then
    overlay each `(staging_dir, dest_relative_path)` pair in `overlays`
    onto `<tmp>/<dest_relative_path>` (replacing that subtree wholesale —
    see module docstring "Overlay contract"), then remove every
    dest-relative path in `deletions`. Returns the temp directory (the
    clone's root, where its `.git` lives).

    `dest_repo_root`'s index and worktree are never touched — this reads
    committed HEAD content only, via `git clone --local --no-checkout`
    (spawn 1) followed by `git checkout -q HEAD` inside the clone (spawn
    2). Never `git worktree` (banned fleet-wide):
    the clone owns an independent `.git` — its own HEAD, index, and ref
    namespace — rather than sharing the dest's. A dirty dest worktree's
    uncommitted changes are invisible to this function by construction:
    cloning reads only the dest's object store and refs.

    The returned tree carries a real `.git` with HEAD equal to the dest's
    HEAD at clone time — `git -C <tmp> status` after overlays/deletions
    land shows exactly this round's changes, which is what the six
    end-of-run gates needing a live git status/HEAD (`argv_parity` and
    others) require. This function never commits anything in the clone.

    Raises `ThrowawayTreeError` on a non-zero `git clone` or `git
    checkout` exit; the partially-built temp dir is reclaimed before the
    raise so a failed build never orphans one.
    """
    dest_repo_root = Path(dest_repo_root)
    tmp_dir = Path(tempfile.mkdtemp(prefix="claude-klabauter-throwaway-tree-"))
    try:
        clone_cmd = [
            "git",
            "clone",
            "--local",
            "--no-checkout",
            str(dest_repo_root),
            str(tmp_dir),
        ]
        result = subprocess.run(
            clone_cmd,
            capture_output=True,
            text=True,
            check=False,
            **_NO_CONSOLE,
        )
        if result.returncode != 0:
            raise ThrowawayTreeError(
                f"git clone --local --no-checkout failed for {dest_repo_root} "
                f"(exit {result.returncode}): {result.stderr.strip()}"
            )

        checkout_cmd = ["git", "-C", str(tmp_dir), "checkout", "-q", "HEAD"]
        result = subprocess.run(
            checkout_cmd,
            capture_output=True,
            text=True,
            check=False,
            **_NO_CONSOLE,
        )
        if result.returncode != 0:
            raise ThrowawayTreeError(
                f"git checkout -q HEAD failed in throwaway clone of {dest_repo_root} "
                f"(exit {result.returncode}): {result.stderr.strip()}"
            )
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    # Root rows first, subdir rows after — the same net effect the real swap
    # produces, where a subdir row's content wins over the root row's stale
    # full-copy of that subdir.
    ordered = sorted(overlays, key=lambda o: str(o[1]) not in ("", "."))
    for staging_dir, dest_relative_path in ordered:
        target = tmp_dir / dest_relative_path
        if str(dest_relative_path) in ("", "."):
            _overlay_root(tmp_dir, Path(staging_dir))
            continue
        if target.exists():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target, onerror=_rmtree_clear_readonly_onerror)
            else:
                target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staging_dir, target, symlinks=True, dirs_exist_ok=True)

    for deletion in deletions:
        victim = tmp_dir / deletion
        if not victim.exists() and not victim.is_symlink():
            continue
        if victim.is_dir() and not victim.is_symlink():
            shutil.rmtree(victim, onerror=_rmtree_clear_readonly_onerror)
        else:
            victim.unlink()

    return tmp_dir


def discard_throwaway_tree(path: Path) -> None:
    """Reclaims a tree `build_throwaway_tree` returned. Windows read-only
    safe (`_rmtree_clear_readonly_onerror`) and idempotent — a no-op if
    `path` is already gone, so a caller on a double-discard path (e.g. an
    exception handler racing a normal completion path) never raises."""
    path = Path(path)
    if path.exists():
        shutil.rmtree(path, onerror=_rmtree_clear_readonly_onerror)
