"""
coordinator_core.ops.dirty_tree_gate — pre-terminate dirty-tree classifier.

Purpose: classify every dirty working-tree path as (a) session-authored
(staged), (b) known concurrent owner, or (c) unattributable. Returns 0 when
all dirty paths are (a) or (b). Returns 3, with case-(c) paths one per line
on stdout, when any unattributable file remains. EOL phantoms are filtered
before classification: ONE batched `git diff --no-renames` spawn (no
pathspec, full unified-diff body parsed by `_diff_changed_paths` — NOT
`--name-only`, see that function's docstring for why) yields the set of
tracked paths whose worktree content actually differs from the index; a
tracked-unstaged path (`X == ' '`) NOT in that set is a phantom (worktree ==
index — a Git-for-Windows stat-staleness artifact, or a
`diff.<driver>.textconv`-normalized no-op) and is benign, never (c). This
replaced a per-dirty-path `git diff --quiet -- <path>` spawn (unbounded in
dirty-file count) — see negative-spec for the path-form and fail-closed
contract of the batched call.

Classification rules applied in order; first match wins for each dirty path:
    EOL phantom : path absent from the batched `_diff_changed_paths` set
                  -> worktree == index (stat-stale) -> skip
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
    - The batched EOL-phantom call (`_diff_changed_paths`, `git diff
      --no-renames` with NO `--name-only`/`--raw`/`--stat`/`--numstat`) is
      deliberate: those listing flags all report a path whenever its raw
      git-object SHA differs, IGNORING `diff.<driver>.textconv`
      normalization — measured directly against this module's own
      `test_eol_phantom_not_flagged_exits_zero` fixture, which configures a
      textconv driver that renders two sides identical. Per-path `git diff
      --quiet` (what this replaced) treats an empty post-textconv patch body
      as "no difference"; only parsing the unified-diff BODY (`+++ b/<path>`
      / `--- a/<path>` -> `/dev/null` pairs, plus the `Binary files ...
      differ` line shape) reproduces that verdict in one batched call. Path
      form still agrees with `git status --porcelain`: neither the porcelain
      parser nor the diff body uses `-z`, and BOTH the status and diff
      subprocess calls pass `-c core.quotepath=false` (see
      `_diff_changed_paths` docstring) so a non-ASCII/space/quote/
      control-char path is never C-quoted by either side -- at git's
      default (`core.quotepath=true`) a diff header quotes the WHOLE
      `"b/<path>"` token, not just the path, which broke a naive
      `startswith("+++ b/")` match and silently swallowed a genuinely-dirty
      quoted path as an EOL phantom (fail-OPEN, this module's own named
      worst case) until this flag was added. `--no-renames` keeps the diff
      body's `a/`/`b/` pairs 1:1 with a single path (matching
      `parse_porcelain_paths`'s own rename-arrow collapse to the destination
      path) instead of letting rename detection split one dirty file into an
      old/new pair with no porcelain counterpart.
      Fail-closed on the batched call itself: if `git diff --no-renames`
      exits non-zero, `_diff_changed_paths` returns `None` and the phantom
      filter is disabled entirely (no path is treated as phantom) rather
      than defaulting to "diff empty -> every tracked-unstaged path is a
      phantom" — the same fail-closed direction as the old per-path `git
      diff --quiet` spawn, whose failure (returncode != 0) also left the
      path NOT skipped.
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
from coordinator_core.git.repo_root import show_toplevel
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


def _diff_changed_paths(repo_root: str) -> Optional[set]:
    """ONE batched `git diff --no-renames` spawn (full unified-diff BODY, not
    `--name-only`/`--raw`/`--stat`/`--numstat`) yielding the set of tracked
    paths whose worktree content actually differs from the index, replacing
    the old per-path `git diff --quiet -- <path>` spawn.

    Why the diff BODY and not a listing flag: `--name-only`/`--raw`/`--stat`/
    `--numstat` all report a path as changed whenever its raw git-object SHA
    differs, IGNORING any `diff.<driver>.textconv` normalization configured
    via `.gitattributes` -- measured directly (see this module's own tests
    plus the sidecar for this port). `git diff --quiet` (what the per-path
    call used) instead special-cases exactly that: when a textconv driver
    renders both sides identical, the generated patch body is EMPTY, and
    `--quiet`/`--exit-code` treat empty patch output as "no difference".
    Parsing the unified-diff body (`+++ b/<path>` / `--- a/<path>` -> `/dev/
    null` pairs, plus the `Binary files a/<path> and b/<path> differ` line
    shape) is the only batched call that reproduces `--quiet`'s per-path
    verdict -- a listing-only flag would silently reclassify a
    textconv-normalized EOL phantom as case-(c) unattributable, the module's
    own worst-failure case.

    Returns None (not an empty set) if the batched call itself fails --
    callers MUST treat None as "phantom filter disabled" (fail-closed, no
    path treated as phantom), matching the old per-path call's failure
    direction: a failing `git diff --quiet` invocation returned nonzero,
    which left that path NOT skipped.

    `-c core.quotepath=false`: at git's DEFAULT (`core.quotepath=true`), a
    non-ASCII/space/quote/control-char path is C-quoted -- and in a diff
    header the quotes wrap the WHOLE `b/<path>` token
    (`"b/m\303\244.txt"`), not just the path, so a naive `line.startswith
    ("+++ b/")` never matches and the path silently never enters `changed`.
    Back in `main()`'s loop that reads as `path not in diff_paths` -> True
    -> phantom -> skipped: a genuinely-dirty non-ASCII path swallowed as an
    EOL phantom, fail-OPEN, this module's own named worst case (a
    should-be-case-(c) file let through). `main()` passes the SAME flag to
    its `git status --porcelain` call so both sides parse in the identical
    unquoted form -- this function alone disabling quoting would just move
    the mismatch to the other side. Residual gap, accepted: a path
    containing a literal NEWLINE is C-quoted by git regardless of
    `core.quotepath` (that setting only governs the >=0x80-byte/space/
    quote/control-char behavior) -- this module's `parse_porcelain_paths`
    still has no unquoting step, so a newline-bearing path was never
    handled by either the old per-path call or this one.
    """
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "-C", repo_root, "diff", "--no-renames"],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    if result.returncode != 0:
        return None

    changed: set = set()
    pending_a: Optional[str] = None
    for line in result.stdout.splitlines():
        if line.startswith("--- a/"):
            pending_a = line[6:]
        elif line.startswith("--- /dev/null"):
            pending_a = None
        elif line.startswith("+++ b/"):
            changed.add(line[6:])
            pending_a = None
        elif line.startswith("+++ /dev/null"):
            if pending_a is not None:
                changed.add(pending_a)
            pending_a = None
        elif line.startswith("Binary files ") and line.endswith(" differ"):
            rest = line[len("Binary files ") : -len(" differ")]
            if " and b/" in rest:
                changed.add(rest.split(" and b/", 1)[1])
            elif " and /dev/null" in rest and rest.startswith("a/"):
                changed.add(rest.split(" and /dev/null", 1)[0][2:])
    return changed


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
        top = show_toplevel()
        if not top:
            print(f"{_PROG} ({terminator}): must be run inside a git repository", file=sys.stderr)
            return 2
        repo_root = top

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
    # `-c core.quotepath=false`: keeps this call's path form in agreement
    # with `_diff_changed_paths`'s own `core.quotepath=false` diff call (see
    # that function's docstring). Without it, a non-ASCII/space/quote/
    # control-char path is C-quoted by porcelain but NOT by this module's
    # `parse_porcelain_paths` (no unquoting step), and disabling quoting only
    # on the diff side (not here) would silently reintroduce the same
    # path-form mismatch this flag exists to close.
    status_result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "-C", repo_root, "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    status_out = status_result.stdout if status_result.returncode == 0 else ""

    # Batched EOL-phantom filter: ONE `git diff --no-renames` spawn for the
    # whole dirty set (see `_diff_changed_paths` docstring for why the diff
    # BODY, not `--name-only`, is what agrees with the old per-path
    # `git diff --quiet -- <path>` call). `diff_paths is None` means the
    # batched call itself failed — fail-closed: the phantom filter is
    # disabled (no path is treated as phantom) rather than guessed empty.
    diff_paths = _diff_changed_paths(repo_root)

    for xy, path in parse_porcelain_paths(status_out):
        x_char = xy[0:1]

        # (a) Staged: X status char is not ' ' or '?'.
        if x_char != " " and x_char != "?":
            continue

        # EOL phantom filter (tracked unstaged only): a path absent from the
        # batched diff set has worktree content equal to the index.
        if x_char == " " and diff_paths is not None and path not in diff_paths:
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
