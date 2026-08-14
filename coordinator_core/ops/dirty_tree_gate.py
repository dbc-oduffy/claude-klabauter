"""
coordinator_core.ops.dirty_tree_gate — pre-terminate dirty-tree classifier.

Purpose: classify every dirty working-tree path as (a) session-authored
(staged), (b) known concurrent owner, or (c) unattributable. Returns 0 when
all dirty paths are (a) or (b). Returns 3, with case-(c) paths one per line
on stdout, when any unattributable file remains. EOL phantoms are filtered
before classification: a path where `git diff --quiet -- <path>` exits 0
(worktree content equals index — a Git-for-Windows stat-staleness artifact)
is benign, never (c).

Classification rules applied in order; first match wins for each dirty path:
    EOL phantom : `git diff --quiet -- <path>` exits 0 -> worktree == index
                  (stat-stale) -> skip
    (a) Staged  : status XY where X != ' ' and X != '?' -> staged for this
                  session's commit -> skip
    (b) Scope   : path appears in scope: block of any state/handoffs/*.md
                  that carries claimed_by: (or legacy consumed_by:) -> skip
    (c) Residual: all others -> print to stdout, rc 3

The script does NOT auto-dispose case-(c) paths — disposition (commit /
stash / name-owner) stays EM judgment in the calling skill's prose.

Port of: dirty-tree-gate.sh (DoE 894d4bc6, 2026-07-22)
Spec backlink: docs/plans/2026-06-30-session-terminator-mechanism-unification.md C2

Negative-spec:
    - Does NOT auto-attribute case-(c) paths to a concurrent peer session
      (would require cross-machine claim visibility this repo does not have).
    - Does NOT modify the working tree — read-only classifier.
    - `_resolve_handoffs_dir` resolves the handoffs directory via the native
      `coordinator_core.state_root.coordinator_state_root()` seam (Rule 5,
      central=False), passing `repo_root` through as the `git_root` override
      rather than shelling out to the bash oracle (`coordinator-state-root.sh`)
      or mutating process cwd. PRESERVES the bash oracle's silent-degrade
      quirk even though the resolution mechanism changed: the oracle's own
      `source` call had no explicit failure check (the oracle script has no
      `set -e`); if the resolver errored, the bash oracle silently fell
      through to an empty HANDOFFS_DIR path (`"" + "/handoffs"`), the
      subsequent `[[ -d ... ]]` test was false, and KNOWN_SCOPE stayed empty
      (no crash, no warning). This port reproduces that exact silent-degrade
      behavior — a `coordinator_state_root()` failure here likewise yields an
      empty scope set, not a raised error.
    - `main()`'s `--root <repo>` flag (mirroring
      `refresh_roadmap_callout.main`'s own `--root`) lets a caller that
      already resolved its worktree root pass it through explicitly, skipping
      the cwd-dependent `git rev-parse --show-toplevel` subprocess call
      entirely. Added so a ceremony orchestrator invoking this module
      in-process no longer needs a process-global `os.chdir()` workaround.
    - Does NOT parse full YAML for the `scope:` block extraction — delegates
      to `coordinator_core.ops.extract_scope_paths._extract_scope_paths`
      (the same fixed-indentation `  - <path>` scanner `pickup_assemble`
      consumes for `preflight.completeness_items[]`), rather than carrying a
      second private copy of the awk-idiom scan.
    - `_build_known_scope`'s owner-claim probe resolves ledger-first via
      `coordinator_core.claim_state.resolve_claim_state`, DR-084 dual-tolerant
      (`claimed_by:` new, `consumed_by:` legacy) on the mirror fallback —
      this module is a read-only classifier over handoffs authored by other
      writers and never itself stamps a handoff status/field, so there is no
      write side here to migrate to NEW-only.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.ops.extract_scope_paths import (
    _extract_scope_paths as _shared_extract_scope_paths,
)
from coordinator_core.claim_state import resolve_claim_state
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.doe_root_pointer import read_doe_root_pointer_file
from coordinator_core.state_root import (
    CrossCuttingStateRoot,
    StateRootError,
    coordinator_state_root,
)
from coordinator_core.win_portability import no_console_creationflags

_PROG = "dirty-tree-gate"  # literal program-name prefix — mirrors oracle stderr text (no .sh suffix)


def _resolve_plugin_root() -> Tuple[Optional[str], Optional[str]]:
    """Resolve PLUGIN_ROOT exactly as the bash oracle's inline resolver does.

    Returns (plugin_root, error_message). error_message is None on success.
    """
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    if plugin_root:
        return plugin_root, None

    doe_root = read_doe_root_pointer_file()

    if not doe_root or not os.path.isdir(os.path.join(doe_root, "coordinator")):
        return None, "ERROR: .doe-root missing/invalid — re-run coordinator:install"

    return os.path.join(doe_root, "coordinator"), None


def _resolve_handoffs_dir(plugin_root: str, repo_root: str) -> str:
    """Resolve `$(coordinator_state_root)/handoffs` via the native
    `coordinator_core.state_root.coordinator_state_root()` seam (Rule 5,
    central=False).

    Review: code-reviewer — this used to chdir into `repo_root` for the
    duration of the call because `coordinator_state_root()`'s Rule-5 git-root
    resolution only read the process's CURRENT WORKING DIRECTORY, with no way
    to pass a root explicitly. A process-global `os.chdir()` is a latent
    hazard under any execution model where multiple in-process calls could
    interleave. `coordinator_state_root()` now accepts an explicit `git_root`
    override (Rule 5 only) so `repo_root` is threaded straight through
    in-process instead — no chdir, no restore, no window for a concurrent
    caller to observe the mutated cwd.

    See module negative-spec: a resolver failure degrades to an empty string
    (silent-degrade, matching the bash oracle's unchecked `source` behavior),
    NOT a raised error.
    """
    del plugin_root  # unused: the native resolver needs no plugin-root lib path
    try:
        state_root = coordinator_state_root(central=False, git_root=repo_root)
    except (StateRootError, CrossCuttingStateRoot, OSError):
        return os.path.join("", "handoffs")

    return os.path.join(state_root, "handoffs")


def parse_porcelain_paths(status_out: str) -> List[Tuple[str, str]]:
    """Parse `git status --porcelain` output into `(xy, path)` pairs.

    One entry per non-empty line: `xy` is the two-char status prefix (indexed
    at columns 0-1), `path` is the path field with the rename `" -> "` form
    already collapsed to its destination (the only path a working-tree read
    should ever attribute to). Shared by this module's own `main()` classifier
    and `coordinator_core.baton_assemble`'s dirty-tree attribution probe — the
    porcelain-parsing loop exists exactly ONCE, here; a second copy anywhere
    else is a bug, not a shortcut.
    """
    pairs: List[Tuple[str, str]] = []
    for line in status_out.splitlines():
        if not line:
            continue
        xy = line[0:2]
        path = line[3:]
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[-1]
        pairs.append((xy, path))
    return pairs


def _build_known_scope(handoffs_dir: str, repo_root: Optional[str] = None) -> set:
    """Union of `scope:` paths from every claimed `state/handoffs/*.md`.

    Purpose: case-(b) known-concurrent-owner membership set (see module
    docstring). A handoff is "claimed" when
    `coordinator_core.claim_state.resolve_claim_state` reports a live
    holder -- ledger-first, frontmatter mirror as fallback. Was previously a
    private `^(claimed_by|consumed_by):` regex over the mirror only.

    Spec backlink: pln-claim-state-make-the-ledger-th-6641e3
    § C3 / AC4. Before this fix, a live ledger claim with a branch-reverted
    mirror dropped its `scope:` paths from `known_scope` entirely,
    reclassifying the claim holder's own in-progress files as case-(c)
    unattributable.

    `common_dir` is resolved once per call, not once per handoff, per C1's
    hot-path cost note.
    """
    known_scope: set = set()
    if not os.path.isdir(handoffs_dir):
        return known_scope

    try:
        entries = sorted(os.listdir(handoffs_dir))
    except OSError:
        return known_scope

    root = Path(repo_root) if repo_root else Path(handoffs_dir).parent.parent
    try:
        common_dir = git_common_dir(root)
    except Exception:
        common_dir = None

    for name in entries:
        if not name.endswith(".md"):
            continue
        path = os.path.join(handoffs_dir, name)
        if not os.path.isfile(path):
            continue
        # Review: coordinator:code-reviewer C3 P3 — renamed from `claim_state`,
        # which shadowed the sibling module `coordinator_core.claim_state`
        # this function imports `resolve_claim_state` from.
        resolved_claim = resolve_claim_state(Path(path), common_dir=common_dir, repo_root=root)
        if resolved_claim.holder is None:
            continue
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            print(f"skip: _build_known_scope: text = Path(path).read_text(encoding=\"utf-8\") failed: {sys.exc_info()[1]}", file=sys.stderr)
            continue
        for sp in _shared_extract_scope_paths(text):
            if sp:
                known_scope.add(sp)

    return known_scope


def main(argv: List[str]) -> int:
    # --- Parse arguments ---
    terminator = ""
    root_arg = ""
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--terminator":
            if i + 1 >= len(argv) or not argv[i + 1]:
                print(f"{_PROG}: --terminator requires a non-empty value", file=sys.stderr)
                return 2
            terminator = argv[i + 1]
            i += 2
        elif arg == "--root":
            if i + 1 >= len(argv) or not argv[i + 1]:
                print(f"{_PROG}: --root requires a non-empty value", file=sys.stderr)
                return 2
            root_arg = argv[i + 1]
            i += 2
        else:
            print(f"{_PROG}: unknown argument: {arg}", file=sys.stderr)
            print(f"usage: {_PROG}.sh --terminator <token> [--root <repo>]", file=sys.stderr)
            return 2

    if not terminator:
        print(f"{_PROG}: --terminator <token> is required", file=sys.stderr)
        print(f"usage: {_PROG}.sh --terminator <token> [--root <repo>]", file=sys.stderr)
        return 2

    # --- Resolve plugin root ---
    plugin_root, err = _resolve_plugin_root()
    if plugin_root is None:
        print(err, file=sys.stderr)
        return 1

    # --- Resolve git repo root ---
    # Review: code-reviewer — an explicit --root (mirroring
    # refresh_roadmap_callout.main's `--root` flag) lets a caller that
    # already knows its worktree root skip the cwd-dependent git subprocess
    # entirely, so a ceremony orchestrator no longer needs a process-global
    # os.chdir() to make this resolution see the right tree.
    if root_arg:
        repo_root = root_arg
    else:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                **no_console_creationflags(),
            )
        except OSError:
            print(f"{_PROG} ({terminator}): must be run inside a git repository", file=sys.stderr)
            return 2
        if result.returncode != 0 or not result.stdout.strip():
            print(f"{_PROG} ({terminator}): must be run inside a git repository", file=sys.stderr)
            return 2
        repo_root = result.stdout.strip()

    # --- Build case-(b) known-scope path set ---
    handoffs_dir = _resolve_handoffs_dir(plugin_root, repo_root)
    known_scope = _build_known_scope(handoffs_dir, repo_root=repo_root)

    # --- Classify dirty paths ---
    unattributable: List[str] = []

    # `--untracked-files=all`, never the bare `--porcelain` default: git's
    # default collapses a wholly-new directory to a single `?? dir/` entry,
    # which this classifier then reports as ONE unattributable path instead of
    # the N files inside it — and a collapsed directory entry can never match
    # the case-(b) `known_scope` set (which holds handoff FILE paths), so a
    # live peer's brand-new handoff directory reads as unattributable no
    # matter how well-known its owner is.
    status_result = subprocess.run(
        ["git", "-C", repo_root, "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    status_out = status_result.stdout if status_result.returncode == 0 else ""

    for xy, path in parse_porcelain_paths(status_out):
        x_char = xy[0:1]

        # (a) Staged: X status char is not ' ' or '?'.
        if x_char != " " and x_char != "?":
            continue

        # EOL phantom filter (tracked unstaged only).
        if x_char == " ":
            diff_result = subprocess.run(
                ["git", "-C", repo_root, "diff", "--quiet", "--", path],
                capture_output=True,
                **no_console_creationflags(),
            )
            if diff_result.returncode == 0:
                continue

        # (b) Known concurrent owner.
        if path in known_scope:
            continue

        # (c) Unattributable.
        unattributable.append(path)

    # --- Report and exit ---
    if not unattributable:
        return 0

    print(
        f"{_PROG} ({terminator}): {len(unattributable)} unattributable file(s) — "
        "disposition required (commit / stash / name-owner):",
        file=sys.stderr,
    )
    for p in unattributable:
        print(p)
    print("", file=sys.stderr)
    print(
        "REFUSING to auto-stash or auto-adopt these paths — this gate cannot tell "
        "'orphaned WT change from a crashed session' apart from 'live peer session's "
        "in-flight file on a shared branch' (the two look identical to git status). "
        "On a concurrent-EM branch this is routine, not exceptional.",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    print("MANUAL FALLBACK — resolve by hand before re-running:", file=sys.stderr)
    print(
        "  1. Inspect each path above: is it yours (forgot to stage), a live peer's "
        "(git log -1 --format=%an -- <path>; is another EM session active on this "
        "branch right now?), or a genuine orphan (no active peer, stale/crashed "
        "session)?",
        file=sys.stderr,
    )
    print(
        "  2. Peer file (live concurrent-EM session)   -> leave it untouched. Do NOT "
        "stash, do NOT commit it. Complete via explicit-path commit of ONLY your own "
        "session's files (git add -- <your-paths> && git commit -m ... -- <your-paths>), "
        "skipping this gate's blanket pass for this run.",
        file=sys.stderr,
    )
    print(
        "  3. Your own file (forgot to stage)          -> git add -- <path>, then "
        "re-run this gate.",
        file=sys.stderr,
    )
    print(
        "  4. Genuine orphan (crashed/abandoned session) -> commit-with-provenance, "
        "or stash-with-provenance (git stash push -u -m '...' -- <path>), or name the "
        "owner explicitly — see skills/workstream-complete/SKILL.md Step 3.0.",
        file=sys.stderr,
    )
    return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
