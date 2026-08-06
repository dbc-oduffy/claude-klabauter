"""
coordinator_core.ops.bootstrap_repo — Git-as-revert bootstrap primitive for one
target repo.

Purpose: non-destructive, fully git-revertible pipeline that brings one target
directory into coordinator scaffolding compliance. The commit this op lands
(chore(coordinator): bootstrap) is the installation log; `git revert` undoes it
completely on a pre-existing repo.

Pipeline stages (in order):
  1. ensure-git    — offer `git init` if absent (declinable; never silent).
  2. assert-clean  — refuse to proceed on a dirty working tree without surfacing it.
  3. scaffold      — idempotent scaffold-canonical-structure.sh --root <target>.
  4. conflict-warn — three-way check-install-divergence.py classifier (percolate UX).
  5. commit        — discrete labelled `chore(coordinator): bootstrap` unit.

Contract (trust guarantees):
  AC3a: Pre-existing repo — bootstrap commit + `git revert` => working tree
        bit-identical to pre-bootstrap HEAD.
  AC3b: git-init'd repo — bootstrap (init + first commit) + teardown => zero
        coordinator-authored content remains; residual `.git/` is additive-
        coordinator-owned and removable via `rm -rf .git/`.

Exit codes (must match `main()`'s actual returns byte-for-byte — do not drift):
  0  Bootstrap complete (or dry-run completed without error).
  1  Usage error, missing prerequisite, or git not available.
  2  User declined git-init offer (repo is not a git repo; nothing done).
  3  Dirty working tree detected; bootstrap refused without user acknowledgement.
     (Under --non-interactive, exits 3 on a dirty tree — never silently proceeds.)
  4  Conflict-warn gate fired: consumer-modified or consumer-added files detected.
     (Printed with full three-way hunk detail; operator must re-run after review.)
  5  TRANSPORT failure (caller-side only, not returned by this
     module's own `main()`): CLAUDE_KLABAUTER_ROOT resolution failed or this module was
     not importable — the caller's own `except RuntimeError`/`except
     ImportError` branches exit 5 before `main()` is ever called, distinct
     from all four business codes above. A caller checking rc==1 for "usage
     error / git not available" must not conflate that with "the claude-klabauter link
     is down and nothing ran at all." Matches the dedicated-transport-code
     convention already used by sibling ports.
     Review: code-reviewer F1 — reusing rc=1 for transport failure violated
     the porter addendum's A3/A3b exit-code-collision rule.

Sibling-script resolution: this op is claude-klabauter-resident but depends on one
Example-doctrine-repo-resident sibling (`coordinator/bin/check-install-divergence.py`) that is
NOT part of this port. The caller computes its own coordinator root and sets
`COORDINATOR_ROOT` in the environment before invoking this module's
`main()` — mirroring the rung-1
env-override convention already used by `coordinator_core.ops.
learn_lessons_roots._resolve_doe_content_root` / `coordinator_core.ops.
coordinator_doe_root`. This module re-derives the same 4-rung fallback ladder
locally (env override -> `~/.claude/.doe-root` pointer -> machine-local
registry -> unconditional flat-layout fallback) rather than importing those
modules' private underscore-prefixed helpers, consistent with how those two
ports each carry their own local copy of the ladder.

Language-migration simplification (NOT a scope-drop): the bash oracle carried
a `resolve-python.sh` sourcing dance (with a `coordinator-trusted-root-guard.sh`
fallback branch) purely to find a python interpreter capable of running
`check-install-divergence.py` from a bash process. Since this op IS that
python process, `sys.executable` already satisfies that need; the dance is
dropped entirely rather than ported, because the requirement it existed to
satisfy is structurally met by the port itself.

Stage 3 (scaffold) update: scaffold-canonical-structure.sh was itself natively
ported to `coordinator_core.install.scaffold_structure.scaffold_canonical_structure`
by a sibling chunk (C4); this stage now calls that function in-process instead
of spawning bash, and no longer needs the sibling `.sh` on disk at all. Scaffold
failure of any kind (`ScaffoldError` or a raw filesystem exception) is caught
and logged as an advisory warning — never fatal, matching
`coordinator_core.install.maximalist`'s Step 7 treatment of the same call. The
bash oracle's `set -euo pipefail`-propagated raw-exit-code-abort on a failing
dry-run, and its double-printed failure output, were artifacts of shelling out
through a `| sed` pipe; neither applies to an in-process function call and
neither is reproduced.

Negative-spec (faithful oracle-bug repro — do NOT "fix" mid-port):
  - Final `git commit` is NOT wrapped in an advisory catch (unlike the
    scaffold stage) — a rejecting pre-commit hook's stderr streams through
    and the op exits with git's own raw exit code (typically 1), skipping the
    "bootstrap complete" trailer. This is deliberate (AC-HOOK-FAIL): bootstrap
    respects the target repo's hooks rather than bypassing them.
  - EOF-on-stdin at either interactive prompt reproduces the bash oracle's
    `_reply="${_reply:-DEFAULT}"` shape: EOF at the git-init prompt (default
    Y) is treated as ACCEPT; EOF at the dirty-tree prompt (default N) is
    treated as DECLINE. The two prompts have opposite EOF polarities in the
    oracle and that asymmetry is preserved here, not "safely" collapsed.

Spec backlink: docs/plans/2026-05-29-it-just-works-agentic-install-currency.md § Chunk 2
             + docs/plans/2026-07-16-bash-clean-slate-residual-migration.md (BIG_PORT wave)
Port of: bootstrap-repo.sh (example-doctrine-repo a1a568d2, 2026-07-22).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core.install.scaffold_structure import (
    ScaffoldError,
    locate_manifest,
    scaffold_canonical_structure,
)
from coordinator_core.ipc import register_op
from coordinator_core.doe_root_pointer import read_doe_root_pointer_file
from coordinator_core.win_portability import is_executable

_GIT_TIMEOUT_SECS = 30
_DIVERGENCE_TIMEOUT_SECS = 120
_COMMIT_TIMEOUT_SECS = 300  # a pre-commit hook may run linters/tests; generous but bounded
_MACHINE_LOCAL_TIMEOUT_SECS = 15

_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_HELP_TEXT = """
Purpose: non-destructive, fully git-revertible pipeline that brings one target
directory into coordinator scaffolding compliance. The commit this script lands
(chore(coordinator): bootstrap) is the installation log; `git revert` undoes it
completely on a pre-existing repo.

Pipeline stages (in order):
  1. ensure-git    — offer `git init` if absent (declinable; never silent).
  2. assert-clean  — refuse to proceed on a dirty working tree without surfacing it.
  3. scaffold      — idempotent scaffold-canonical-structure.sh --root <target>.
  4. conflict-warn — three-way check-install-divergence.py classifier (percolate UX).
  5. commit        — discrete labelled `chore(coordinator): bootstrap` unit.

Spec backlink: docs/plans/2026-05-29-it-just-works-agentic-install-currency.md § Chunk 2

Contract (trust guarantees):
  AC3a: Pre-existing repo — bootstrap commit + `git revert` ⇒ working tree
        bit-identical to pre-bootstrap HEAD.
  AC3b: git-init'd repo — bootstrap (init + first commit) + teardown ⇒ zero
        coordinator-authored content remains; residual `.git/` is additive-
        coordinator-owned and removable via `rm -rf .git/`.

Usage:
  bootstrap-repo.sh [--root <path>] [--non-interactive] [--dry-run] [--help]

Flags:
  --root <path>        Target repo root to bootstrap. Defaults to `git
                       rev-parse --show-toplevel` of the current working dir.
  --non-interactive    Skip all user prompts; decline git-init offer silently
                       (mirrors /setup --non-interactive behaviour for chained
                       callers that cannot present AskUserQuestion). Under this
                       flag the script exits 0 with a status row if the repo is
                       not yet a git repo (no init performed).
  --dry-run            Print the plan; create no files; make no commits.
  --help | -h          Show this help and exit.

Exit codes:
  0  Bootstrap complete (or dry-run completed without error).
  1  Usage error, missing prerequisite, or git not available.
  2  User declined git-init offer (repo is not a git repo; nothing done).
  3  Dirty working tree detected; bootstrap refused without user acknowledgement.
     (Under --non-interactive, exits 3 on a dirty tree — never silently proceeds.)
  4  Conflict-warn gate fired: consumer-modified or consumer-added files detected.
     (Printed with full three-way hunk detail; operator must re-run after review.)
""".strip(
    "\n"
)


# ---------------------------------------------------------------------------
# example-doctrine-repo sibling-root resolution (rung ladder mirrored from
# coordinator_core.ops.learn_lessons_roots._resolve_doe_content_root)
# ---------------------------------------------------------------------------


def _claude_home() -> str:
    """Mirror `CLAUDE_HOME="${CLAUDE_HOME:-$HOME}/.claude"` — the env var, when
    set, overrides $HOME (not the full .claude path). Matches the identically-
    named helper in coordinator_core.ops.learn_lessons_roots (copied, not
    imported, per that module's own local-copy convention)."""
    base = os.environ.get("CLAUDE_HOME") or os.path.expanduser("~")
    return os.path.join(base, ".claude")


def _content_root_rungs_2_to_4(claude_home: str) -> str:
    """Rungs 2-4 of the example-doctrine-repo coordinator content root ladder, factored out so
    `_resolve_scaffold_manifest_root` can re-run them without rung 1 (see
    that function's docstring for why rung 1 alone is unsound for the
    manifest lookup)."""
    doe_root = read_doe_root_pointer_file(os.path.expanduser("~"))
    if doe_root:
        candidate = os.path.join(doe_root, "coordinator")
        if os.path.isdir(candidate):
            return candidate

    machine_local = os.path.join(claude_home, "bin", "machine-local")
    if os.path.isfile(machine_local) and is_executable(machine_local):
        try:
            proc = subprocess.run(
                [machine_local, "get", "plugin.mirrors.coordinator-claude.live_path"],
                capture_output=True,
                text=True,
                timeout=_MACHINE_LOCAL_TIMEOUT_SECS,
                stdin=subprocess.DEVNULL,
                creationflags=_CREATIONFLAGS,
            )
            candidate = (proc.stdout or "").strip()
            if proc.returncode == 0 and candidate and os.path.isdir(candidate):
                return candidate
        except (OSError, subprocess.TimeoutExpired):
            print(f"skip: _resolve_doe_content_root: proc = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass

    return os.path.join(claude_home, "plugins", "coordinator-claude", "coordinator")


def _resolve_doe_content_root(claude_home: str) -> str:
    """Resolve the example-doctrine-repo coordinator content root (coordinator/bin, coordinator/lib).

    Rungs (best-effort, non-fatal):
      1. COORDINATOR_ROOT / CLAUDE_PLUGIN_ROOT env override (set by the example-doctrine-repo-side
         trampoline before importing this module).
      2-4. See `_content_root_rungs_2_to_4` for the rest of the ladder (`.doe-root`
         pointer file, machine-local registry, unconditional plugins-dir fallback).
    """
    override = os.environ.get("COORDINATOR_ROOT") or os.environ.get("CLAUDE_PLUGIN_ROOT")
    if override:
        return override

    return _content_root_rungs_2_to_4(claude_home)


def _resolve_scaffold_manifest_root(claude_home: str, coordinator_root: str) -> str:
    """Resolve the root `scaffold_canonical_structure` should search for
    `canonical-structure.yaml` -- separately from `coordinator_root`
    (`bin/check-install-divergence.py`'s home).

    The two used to be the same directory by construction: both were
    example-doctrine-repo-resident, reached through the identical rung-1 `COORDINATOR_ROOT`
    override. Example-doctrine-repo `b644d5a9b` ("migrate the executable surface + its tests
    to claude-klabauter") moved `check-install-divergence.py` here for good,
    and the trampoline (`coordinator/lib/bootstrap-repo.py`) now
    unconditionally points `COORDINATOR_ROOT` at claude-klabauter's OWN `coordinator/`
    tree so that move resolves -- but `canonical-structure.yaml` stays
    example-doctrine-repo-owned and deliberately unvendored (`scaffold_structure.py` AC D5),
    so that same override now points the manifest lookup at a directory that
    was never meant to carry it. No single directory satisfies both anymore.

    `coordinator_root` is tried first -- every
    `coordinator_core/ops/test_bootstrap_repo.py` fixture (and any other
    caller that still hands this op one merged root) carries the manifest
    there, and that fast path must keep resolving unchanged. Only on a miss
    does this fall through to rungs 2-4 of the SAME ladder
    `_resolve_doe_content_root` uses -- skipping rung 1, since that produced
    the `coordinator_root` value that just failed the presence check."""
    try:
        locate_manifest(Path(coordinator_root))
    except ScaffoldError:
        pass
    else:
        return coordinator_root

    return _content_root_rungs_2_to_4(claude_home)


# ---------------------------------------------------------------------------
# Small git/subprocess helpers — every call carries a bounded timeout and a
# DEVNULL stdin guard (nothing here should ever be able to block on stdin).
# ---------------------------------------------------------------------------


def _git(
    args: List[str],
    root: Optional[str] = None,
    timeout: int = _GIT_TIMEOUT_SECS,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    cmd = ["git"]
    if root is not None:
        cmd += ["-C", root]
    cmd += args
    return subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        creationflags=_CREATIONFLAGS,
    )


def _prompt(text: str, default_yes: bool) -> bool:
    """Read a Y/n-shaped reply from stdin; EOF degrades to the oracle's default.

    Mirrors bash `read -r -p "$text" reply; reply="${reply:-DEFAULT}"` followed
    by a `case` match against `[Yy]|[Yy][Ee][Ss]` (accept) vs anything else
    (decline) -- reproduced here as a case-insensitive exact match against
    "y"/"yes", not a startswith/prefix check.
    """
    try:
        reply = input(text)
    except EOFError:
        reply = ""
    reply = reply.strip()
    if not reply:
        reply = "Y" if default_yes else "N"
    return reply.lower() in ("y", "yes")


def _print(*args, **kwargs) -> None:
    print(*args, **kwargs)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: List[str]) -> int:
    # ---- arg parsing -------------------------------------------------
    root_path = ""
    non_interactive = False
    dry_run = False

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--help", "-h"):
            _print(_HELP_TEXT)
            return 0
        elif arg == "--root":
            if i + 1 >= len(argv):
                _print("bootstrap-repo.sh: --root requires an argument", file=sys.stderr)
                return 1
            root_path = argv[i + 1]
            i += 2
            continue
        elif arg == "--non-interactive":
            non_interactive = True
            i += 1
            continue
        elif arg == "--dry-run":
            dry_run = True
            i += 1
            continue
        elif arg.startswith("-"):
            _print(f"bootstrap-repo.sh: unknown flag: {arg}", file=sys.stderr)
            return 1
        else:
            _print(f"bootstrap-repo.sh: unexpected argument: {arg}", file=sys.stderr)
            return 1
        i += 1

    # ---- prerequisite checks ------------------------------------------
    if _which_git() is None:
        _print("bootstrap-repo.sh: git is not available on PATH", file=sys.stderr)
        return 1

    claude_home = _claude_home()
    coordinator_root = _resolve_doe_content_root(claude_home)
    manifest_root = _resolve_scaffold_manifest_root(claude_home, coordinator_root)
    bin_dir = os.path.join(coordinator_root, "bin")
    divergence_py = os.path.join(bin_dir, "check-install-divergence.py")

    if not os.path.isfile(divergence_py):
        _print(
            f"bootstrap-repo.sh: check-install-divergence.py not found at: {divergence_py}",
            file=sys.stderr,
        )
        return 1

    # ---- resolve target root -------------------------------------------
    if not root_path:
        try:
            proc = _git(["rev-parse", "--show-toplevel"])
        except (OSError, subprocess.TimeoutExpired):
            proc = None
        if proc is None or proc.returncode != 0:
            _print(
                "bootstrap-repo.sh: --root not provided and not inside a git repo",
                file=sys.stderr,
            )
            return 1
        root_path = (proc.stdout or "").strip()

    # Normalise path separators on Windows (Git-Bash/MSYS)
    root_path = root_path.replace("\\", "/")

    if not os.path.isdir(root_path):
        _print(f"bootstrap-repo.sh: target root does not exist: {root_path}", file=sys.stderr)
        return 1

    # ---- dry-run header --------------------------------------------------
    if dry_run:
        _print(f"[bootstrap-repo dry-run] target: {root_path}")
        _print(f"[bootstrap-repo dry-run] non-interactive: {1 if non_interactive else 0}")

    # ---- Stage 1 — ensure-git --------------------------------------------
    is_git_repo = _is_git_repo(root_path)

    if not is_git_repo:
        if dry_run:
            _print("[bootstrap-repo dry-run] stage 1 (ensure-git): NOT a git repo")
            _print(f"[bootstrap-repo dry-run]   would offer: git init {root_path}")
            _print("[bootstrap-repo dry-run]   (--non-interactive would decline and exit 2)")
            # For dry-run purposes, treat as if we would init and continue
        elif non_interactive:
            _print(f"bootstrap-repo: {root_path} is not a git repository.", file=sys.stderr)
            _print(
                "  Under --non-interactive, git-init is not offered. Skipping bootstrap.",
                file=sys.stderr,
            )
            _print("  Re-run interactively to be offered git-init.", file=sys.stderr)
            return 2
        else:
            _print("")
            _print(f"bootstrap-repo: {root_path} is not a git repository.")
            _print("")
            _print(
                "  Coordinator scaffolding uses git as the revert substrate — initializing"
            )
            _print(
                "  a git repo is the precondition that makes the bootstrap fully reversible."
            )
            _print(
                "  The .git/ directory is additive and non-destructive; it can be removed"
            )
            _print("  via 'rm -rf .git/' if you later decide you don't want it.")
            _print("")
            _print("  [Recommended] Initialize git repo and continue bootstrap")
            _print("  [Skip]        Decline — no changes will be made")
            _print("")
            accepted = _prompt(f"  Initialize git repo at {root_path}? [Y/n] ", default_yes=True)
            if accepted:
                _git(["init", "--quiet"], root=root_path, capture=False)
                _print(f"  git init: done ({root_path}/.git/ created)")
                is_git_repo = True
            else:
                _print("  Declined. No changes made.")
                return 2
    else:
        if dry_run:
            _print("[bootstrap-repo dry-run] stage 1 (ensure-git): already a git repo")

    # ---- Stage 2 — assert clean baseline ----------------------------------
    if not dry_run:
        dirty = _git_status_porcelain(root_path)
        # BEHAVIOUR CHANGE (2026-07-22, break-class fix): an unverifiable
        # working tree is UNKNOWN, not clean — abort rather than mutate a
        # repo whose 'git status' we could not confirm is clean.
        if dirty is None:
            _print("")
            _print(f"bootstrap-repo: unable to verify {root_path} has a clean working tree (git status check failed).")
            _print("")
            _print(
                "  Bootstrap requires a verified clean baseline so that 'git revert' fully"
            )
            _print("  restores the pre-bootstrap state. Refusing to proceed without that guarantee.")
            return 3
        if dirty:
            _print("")
            _print(f"bootstrap-repo: {root_path} has uncommitted changes (dirty working tree).")
            _print("")
            _print(dirty)
            _print("")
            _print(
                "  Bootstrap requires a clean baseline so that 'git revert' fully restores"
            )
            _print("  the pre-bootstrap state. Please commit or stash your changes first.")
            _print("")
            if non_interactive:
                _print("  Under --non-interactive: refusing to proceed.", file=sys.stderr)
                return 3
            else:
                _print("  [Abort] — stop here and let you clean up (recommended)")
                _print("  [Force] — proceed anyway (git revert guarantee does NOT hold)")
                _print("")
                proceed = _prompt("  Proceed despite dirty tree? [y/N] ", default_yes=False)
                if proceed:
                    _print("  Proceeding (git revert guarantee voided).")
                else:
                    _print("  Aborted. No changes made.")
                    return 3

    if dry_run:
        dirty_dry = _git_status_porcelain(root_path)
        if dirty_dry is None:
            _print(
                "[bootstrap-repo dry-run] stage 2 (assert-clean): UNABLE TO VERIFY (git status check failed) — bootstrap would be refused"
            )
        elif dirty_dry:
            _print(
                "[bootstrap-repo dry-run] stage 2 (assert-clean): DIRTY TREE — bootstrap would be refused"
            )
        else:
            _print("[bootstrap-repo dry-run] stage 2 (assert-clean): working tree is clean")

    # ---- Stage 3 — scaffold ------------------------------------------------
    # Native call (C4 ported scaffold-canonical-structure.sh to
    # coordinator_core.install.scaffold_structure; this stage no longer
    # spawns bash). Advisory per setup.md Phase 3 -- any failure is caught
    # and logged, never halts the bootstrap chain, mirroring maximalist.py's
    # Step 7 `except Exception` treatment of the same call.
    if dry_run:
        _print(f"[bootstrap-repo dry-run] stage 3 (scaffold): would scaffold {root_path}")
        try:
            _scaffold_result = scaffold_canonical_structure(root_path, manifest_root, dry_run=True)
        except Exception as exc:
            _print(f"  WARN: scaffold dry-run failed -- continuing (advisory, not fatal): {exc}")
        else:
            for line in _scaffold_result.lines:
                _print(f"  {line}")
    else:
        try:
            _scaffold_result = scaffold_canonical_structure(root_path, manifest_root, dry_run=False)
        except Exception as exc:
            _print(
                f"bootstrap-repo: scaffold-canonical-structure failed: {exc}",
                file=sys.stderr,
            )
            # Scaffold failure is advisory per setup.md Phase 3 — do NOT halt the chain.
            _print(
                "  Warning: scaffold failed; continuing bootstrap (scaffold is advisory)."
            )
        else:
            for line in _scaffold_result.lines:
                _print(line)

    # ---- Stage 4 — conflict-warn --------------------------------------------
    has_baseline = os.path.isfile(os.path.join(root_path, "version.txt"))

    if dry_run:
        if has_baseline:
            _print(
                "[bootstrap-repo dry-run] stage 4 (conflict-warn): version.txt found — would run check-install-divergence.py"
            )
            _print(f"  --source {coordinator_root} --live {root_path}")
        else:
            _print(
                "[bootstrap-repo dry-run] stage 4 (conflict-warn): no version.txt — first-time bootstrap, skipping classifier"
            )
    elif has_baseline:
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    divergence_py,
                    "--source",
                    coordinator_root,
                    "--live",
                    root_path,
                    "--format",
                    "text",
                ],
                timeout=_DIVERGENCE_TIMEOUT_SECS,
                stdin=subprocess.DEVNULL,
                creationflags=_CREATIONFLAGS,
            )
            conflict_exit = proc.returncode
        except subprocess.TimeoutExpired:
            _print(
                "bootstrap-repo: check-install-divergence.py timed out",
                file=sys.stderr,
            )
            return 1

        # Exit codes per check-install-divergence.py contract:
        #   0 — unchanged + forward-safe only; safe to proceed
        #   2 — no/malformed baseline; two-way clean; safe to proceed
        #   3 — consumer-modified or consumer-added files detected
        if conflict_exit == 3:
            _print("")
            _print(
                "bootstrap-repo: conflict-warn gate FIRED — consumer-modified or consumer-added files detected."
            )
            _print(
                "  The files listed above will be overwritten by the bootstrap scaffold. Check them first."
            )
            _print(
                "  To proceed anyway, remove or reconcile the conflicting files, then re-run bootstrap."
            )
            _print("")
            return 4
        elif conflict_exit not in (0, 2):
            _print(
                f"bootstrap-repo: check-install-divergence.py exited with unexpected code: {conflict_exit}",
                file=sys.stderr,
            )
            return 1
    else:
        _print(
            "[bootstrap-repo] stage 4 (conflict-warn): first-time bootstrap (no version.txt) — skipping classifier"
        )

    # ---- Stage 5 — commit ----------------------------------------------------
    if dry_run:
        _print("[bootstrap-repo dry-run] stage 5 (commit): would commit staged scaffold files")
        _print("  message: chore(coordinator): bootstrap")
        _print("[bootstrap-repo dry-run] done — no changes made")
        return 0

    untracked = _git_lines(["ls-files", "--others", "--exclude-standard"], root_path)
    modified = _git_lines(["diff", "--name-only"], root_path)

    for f in untracked:
        try:
            proc = _git(["add", "--", f], root=root_path)
            if proc.returncode != 0:
                _print(
                    f"bootstrap-repo: WARNING — failed to stage untracked file: {f}",
                    file=sys.stderr,
                )
        except subprocess.TimeoutExpired:
            _print(
                f"bootstrap-repo: WARNING — timed out staging untracked file: {f}",
                file=sys.stderr,
            )

    for f in modified:
        try:
            proc = _git(["add", "--", f], root=root_path)
            if proc.returncode != 0:
                _print(
                    f"bootstrap-repo: WARNING — failed to stage modified file: {f}",
                    file=sys.stderr,
                )
        except subprocess.TimeoutExpired:
            _print(
                f"bootstrap-repo: WARNING — timed out staging modified file: {f}",
                file=sys.stderr,
            )

    staged_files = _git_lines(["diff", "--cached", "--name-only"], root_path)

    if not staged_files:
        _print("bootstrap-repo: nothing to commit (scaffold was already up to date).")
        _print("  status: bootstrap already current — no commit made.")
        return 0

    git_user_name = _git_config(root_path, "user.name") or "Coordinator"
    git_user_email = _git_config(root_path, "user.email") or "coordinator@coordinator"

    env = dict(os.environ)
    env["GIT_AUTHOR_NAME"] = git_user_name
    env["GIT_AUTHOR_EMAIL"] = git_user_email
    env["GIT_COMMITTER_NAME"] = git_user_name
    env["GIT_COMMITTER_EMAIL"] = git_user_email

    try:
        proc = subprocess.run(
            ["git", "-C", root_path, "commit", "-m", "chore(coordinator): bootstrap"],
            env=env,
            timeout=_COMMIT_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            creationflags=_CREATIONFLAGS,
        )
    except subprocess.TimeoutExpired:
        _print("bootstrap-repo: git commit timed out", file=sys.stderr)
        return 1

    if proc.returncode != 0:
        # Faithful oracle repro: the commit is NOT advisory-wrapped like scaffold —
        # a rejecting pre-commit hook propagates straight through, skipping the
        # "bootstrap complete" trailer below (AC-HOOK-FAIL).
        return proc.returncode

    _print("")
    _print("bootstrap-repo: bootstrap complete.")
    _print("  Commit: chore(coordinator): bootstrap")
    _print(f"  Revert: git -C '{root_path}' revert HEAD   (restores pre-bootstrap state)")
    return 0


# ---------------------------------------------------------------------------
# small internal helpers
# ---------------------------------------------------------------------------


def _which_git() -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            creationflags=_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _which_git: proc = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    return "git" if proc.returncode == 0 else None


def _is_git_repo(root: str) -> bool:
    try:
        proc = _git(["rev-parse", "--show-toplevel"], root=root)
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _is_git_repo: proc = _git([\"rev-parse\", \"--show-toplevel\"], root=root) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    return proc.returncode == 0


def _git_status_porcelain(root: str) -> Optional[str]:
    """Returns the porcelain status output, "" for a verified-clean tree, or
    None when the check itself could not be completed (git error/timeout).
    None is a distinct sentinel from "" — callers MUST treat "unable to
    verify" as UNKNOWN, never as clean.

    BEHAVIOUR CHANGE (2026-07-22, break-class fix): previously returned ""
    for both a clean tree AND a git-status failure/timeout, so the stage-2
    "assert clean baseline" gate treated an unverifiable tree as clean and
    proceeded to mutate the repo — voiding the git-revert guarantee the gate
    exists to provide.
    """
    try:
        proc = _git(["status", "--porcelain"], root=root)
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _git_status_porcelain: proc = _git([\"status\", \"--porcelain\"], root=root) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").rstrip("\n")


def _git_lines(args: List[str], root: str) -> List[str]:
    try:
        proc = _git(args, root=root)
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _git_lines: proc = _git(args, root=root) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return []
    if proc.returncode != 0:
        return []
    return [line for line in (proc.stdout or "").splitlines() if line]


def _git_config(root: str, key: str) -> Optional[str]:
    try:
        proc = _git(["config", key], root=root)
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _git_config: proc = _git([\"config\", key], root=root) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        return None
    val = (proc.stdout or "").strip()
    return val or None


# ---------------------------------------------------------------------------
# op registration — validate-target-root-is-git-repo (repo-setup fail-loud
# guard). Small reusable validator near this module's other fail-loud guards
# (stage 1 "ensure-git" above performs the same _is_git_repo check inline,
# with an interactive git-init offer attached); this is the read-only,
# no-prompt sibling repo-setup calls standalone before deciding whether to
# hand a target root to the full bootstrap pipeline.
# ---------------------------------------------------------------------------


def _validate_target_root_is_git_repo(target_root: str) -> dict:
    """Read-only check: does target_root exist and is it a git repository?

    Returns {"valid": bool, "reason": str | None} — reason is None when valid,
    otherwise a short human-readable cause. Performs no mutation and reuses the
    existing `_is_git_repo()` helper (which already carries the bounded-timeout,
    DEVNULL-stdin `_git()` call) rather than introducing a second git-repo check.
    """
    root = Path(target_root)
    if not root.is_dir():
        return {"valid": False, "reason": f"target root does not exist: {target_root}"}
    if not _is_git_repo(str(root)):
        return {"valid": False, "reason": f"target root is not a git repository: {target_root}"}
    return {"valid": True, "reason": None}


@register_op("repo_setup.validate_target_root")
async def _validate_target_root_op(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'repo_setup.validate_target_root' handler.

    Params:
        target_root (str, required) — the repo being onboarded during repo-setup
        (an explicit caller-supplied path, not necessarily the invoking cwd/worktree;
        this op never needs `_origin_worktree` injected — scope is "none").

    Returns: {"valid": bool, "reason": str | None}.

    Idempotency (AC7): purely read-only (dir-exists + is-a-git-repo check, no
    writes anywhere); a second invocation with identical params returns the same
    result and is a safe no-op by construction — no explicit idempotency
    mechanism is needed because there is no state to make idempotent.
    """
    target_root = params.get("target_root")
    if not target_root:
        raise ValueError("repo_setup.validate_target_root requires params.target_root")
    return _validate_target_root_is_git_repo(target_root)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
