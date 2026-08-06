"""
coordinator_core.ops.workday_complete_step2_5_dirty_tree — Step 2.5 dirty-tree
auto-disposition.

Spec backlink: commands/workday-complete.md § Step 2.5
Spec backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md
PM ruling:     cross-repo/inbox/2026-06-16-workday-complete-dirty-tree-autonomy.md

Purpose: classifies every dirty path in the cwd's git repo (via `git status
--porcelain --untracked-files=all`) and acts on it, first-match-wins:

    EOL-PHANTOM    — content-equal to index (`git diff --quiet` exits 0);
                     silently skip. Rename destinations (`R*` status) and
                     untracked (`??`) paths never take this branch.
    SUBMODULE      — gitlink (mode 160000); silently skip (LEAVE-ALONE).
    LEAVE-ALONE    — `state/handoffs/` or `.git/`; silently skip (concurrent-
                     session territory — NOT `archive/handoffs/`, which is a
                     distinct, AUTO-COMMIT-eligible root).
    ORPHAN-TMP     — basename matches `*.tmp.[0-9]*.[0-9]*` (an Edit-tool
                     atomic-write crash artifact); listed on stderr, never
                     auto-deleted.
    AUTO-GITIGNORE — `logs/`, `*.log`, `*.pid`, `.DS_Store`, `Thumbs.db`;
                     pattern appended to `.gitignore` (dedup'd), tracked
                     copies `git rm --cached`, one commit.
    CLAIM          — (C6, 2026-08-05 in-process-writers-declare-their-
                     writes plan) a single ``compute_offer(sid)`` call's
                     four-bucket ownership readout decides BEFORE the
                     AUTO-COMMIT prefix rule below gets a chance, WITH ONE
                     CARVE-OUT (SOURCE-TREE — see below): a path in this
                     run's own ``ownership["mine"]`` commits BY CLAIM,
                     never by prefix; a path with a resolvable
                     ``ownership["peer"]`` entry is reported
                     (owner/liveness/claim_source) on stderr and NEVER
                     committed, needs-PM; every other path (unattributed,
                     or non-mine while ``ownership["degraded"]``) falls
                     through UNCHANGED to AUTO-COMMIT / SOURCE-TREE /
                     AMBIGUOUS below, exactly as if this branch did not
                     exist. Skipped in its entirety — every path
                     classifies exactly like the pre-C6 module — when this
                     run's session id is unresolvable or the ownership
                     read itself raised; see `_resolve_claim_context` and
                     "Session-id resolution (C6)" below.

                     SOURCE-TREE carve-out: a path that would classify
                     SOURCE-TREE (branch 7, `_classify_source_tree`) is
                     UNCONDITIONALLY excluded from this branch — mine,
                     peer, and unattributed/degraded alike — and falls
                     through untouched to SOURCE-TREE below, exactly as
                     pre-C6, even when it IS this run's own provable
                     claim. A claim answers WHO wrote a path, never
                     whether it is safe for an unattended ceremony to
                     commit unreviewed; SOURCE-TREE exists precisely to
                     force human review of source changes (`plugins/`,
                     `bin/`, `hooks/`, `skills/`, `agents/`, `commands/`,
                     `lib/`, `tests/`, `schemas/`, `setup/`, `snippets/`,
                     `docs/wiki/`, `docs/decisions/`, `docs/plans/`), and a
                     session's own claimed edit under one of those roots
                     still needs that review — auto-committing it would be
                     exactly the "do not widen what any ceremony commits"
                     anti-scope this plan carries. (2026-08-05, PM-caught:
                     an earlier version of this branch had no such
                     carve-out and read as unconditional precedence —
                     fixed same-day, before landing.)
    AUTO-COMMIT    — known-safe housekeeping roots (allow-list below);
                     explicit-pathspec commit.
    SOURCE-TREE    — `plugins/`, `bin/`, `hooks/`, etc.; listed on stderr,
                     needs-PM.
    AMBIGUOUS      — none of the above; listed on stderr, needs-PM.

A second pass re-walks the same `git status` output to classify rename
SOURCE paths (`old -> new` porcelain lines only classify `new` in the main
pass) against the AUTO-GITIGNORE / AUTO-COMMIT / SOURCE-TREE rules, folding
a source that lands in a different category than its destination into the
right bucket. A rename source is folded into `commit_paths` (the `git
commit` pathspec, so its staged deletion lands in the rename commit) but
NEVER into `commit_add_paths` (the `git add` pathspec) — it no longer
exists in the worktree, and `git add`ing a vanished path aborts with exit
128. This also covers the `RD` porcelain case (a rename whose destination
was itself deleted post-stage): the destination is excluded from `git add`
whenever its worktree status char is `D`, so the rename degrades to a
plain deletion-of-source commit instead of corrupting the index.

Session-id resolution (C6, 2026-08-05 in-process-writers-declare-their-
writes plan). This module was pure-stdlib and session-agnostic before C6
(no `coordinator_core` imports, `main(argv)` accepting only `--dry-run`);
CLAIM needs a session id to call `compute_offer(sid)` with, and this
module has none on hand. Two candidates were named at dispatch time:
(a) grow `main()` a session-id parameter/flag, or (b) call
`coordinator_core.session.core.resolve_session_id(repo_root)` in-process.
DECIDED: (b). `main()`'s own argv contract (`--dry-run` only) is
UNCHANGED — no new flag. This matters beyond this module's own CLI: the
Example-doctrine-repo-side trampoline's invocation of this module's import/`main()`, AND
this repo's own in-process caller
(`coordinator_core.workday_complete.brief._compute_dirty_tree_verdict`,
which calls `_step2_5_dirty_tree_main(["--dry-run"])` with no session
concept of its own today) both keep calling this module exactly as
before — (a) would have required updating both call sites' argv shape;
(b) requires updating neither.

`resolve_session_id`'s own docstring is the disposition for every one of
its 4 ambiguity tiers (env-var tiers 1-3, and tier-4's sentinel-file
concurrency guard) — this module does not re-implement that logic, only
consumes its single resolved-or-"" signal. "" (unresolvable, by ANY of
that function's own tier-4 ambiguity reasons: >=2 live sessions, exactly
1 live but the sentinel names someone else, 0 live but the sentinel names
a dead/racing session) is treated identically to a `compute_offer` call
that itself raised: both degrade this run to skipping the CLAIM branch
entirely, so every dirty path classifies by branches 6-8 alone — EXACTLY
today's pre-C6 behavior, never a wider commit (see `_resolve_claim_context`).

Exit codes (`main()`) — these are business codes only; this module never
raises or resolves CLAUDE_KLABAUTER_ROOT, so it has no transport-failure concept.
The example-doctrine-repo-side trampoline wraps this module's import/invocation and uses a
SEPARATE dedicated rc=3
for transport failure (CLAUDE_KLABAUTER_ROOT resolution / import), per code-review
Finding 2 (A3b) — it never reuses these business codes for that purpose:
    0 — all clear-wins handled (or nothing dirty); no source-tree/ambiguous
        paths remain.
    1 — hard error (not a git repo, `git status` failed, `.gitignore`
        unwriteable, a `git add`/`git rm --cached`/`git commit` failed).
    2 — clear-wins handled; source-tree or ambiguous paths remain (needs
        PM attention — printed as `NEEDS-PM` on stdout).

`--dry-run`: prints what would be done; makes no commits, no `.gitignore`
edits, no `git rm --cached`. Read-only git lookups (status/diff/ls-files)
still run so the dry-run preview reflects the real classification.

Stdout carries the per-group summary + the final `OK`/`NEEDS-PM` verdict
line (both prefixed `[step2.5] `); stderr carries per-path detail for
orphan-tmp / source-tree / ambiguous / dry-run notices.

Negative-spec (faithful bash-oracle reproduction, not silently "fixed"):
    - RATIFIED DIVERGENCE (C6, 2026-08-05 in-process-writers-declare-
      their-writes plan; PM ruling obtained before this chunk executed,
      per that chunk's own RATIFICATION GATE): the CLAIM branch above is
      a DELIBERATE departure from the faithful-bash-oracle-reproduction
      posture the rest of this negative-spec still holds to. The bash
      oracle had no session concept and never will; CLAIM's
      commit-by-claim/report-by-claim decision has no oracle counterpart
      to stay faithful to. This amends, and does not silently contradict,
      every OTHER bullet below — the AUTO-GITIGNORE/AUTO-COMMIT/
      SOURCE-TREE allow-lists remain the oracle's own fixed, non-config-
      driven lists; only CLAIM's precedence over AUTO-COMMIT and
      AMBIGUOUS is new. SOURCE-TREE is UNTOUCHED by this divergence — the
      CLAIM branch carries an unconditional carve-out for it (see the
      module docstring's CLAIM description, "SOURCE-TREE carve-out"
      paragraph): a claim proves authorship, never reviewability, and
      SOURCE-TREE's human-review gate applies to a claimed path exactly as
      it did before this chunk existed.
    - The AUTO-GITIGNORE allow-list and AUTO-COMMIT allow-list are the
      oracle's own fixed lists (see `_classify_gitignore`/`_classify_commit`
      below) — adding a new root/pattern requires a code change here, same
      as the oracle required a script edit. Not config-driven.
    - Only the three named `docs/plans/*-check.md` sidecar suffixes
      (`.plan-coverage-check.md`, `.prior-art-check.md`,
      `.external-pattern-check.md`) are AUTO-COMMIT-eligible; any other
      `docs/plans/*` path falls through to SOURCE-TREE.
    - The oracle merges `git status`'s stdout and stderr into one stream
      before line-splitting (`> tmp 2>&1`); on the success path `git
      status` emits no stderr in practice, so this port reads stdout only
      — behaviorally equivalent for every real invocation, and the
      genuine-failure path (non-zero exit) is still caught and reported
      identically (exit 1) before any line is parsed.
    - Rename-source classification (the second pass) applies ONLY the
      AUTO-GITIGNORE / AUTO-COMMIT / SOURCE-TREE rules — it does NOT
      re-run EOL-PHANTOM / SUBMODULE / LEAVE-ALONE / ORPHAN-TMP checks on
      the source path, matching the oracle's own second loop exactly. A
      rename source that matches none of the three rules is left
      unclassified (neither committed nor reported) — this is a
      pre-existing oracle gap (a rename source with no destination-side
      collision that also isn't gitignore/commit/source-tree-eligible
      simply stays staged, uncommitted, unreported), reproduced as-is.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
import sys
from typing import Dict, FrozenSet, List, Optional, Tuple

from coordinator_core.session.declared_writes import declare_write

_PROG = "step2.5"
_GIT_TIMEOUT_SECS = 30
_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------------------------------------------------------------------------
# Unit 3 message text (AUTO-GITIGNORE / AUTO-COMMIT act blocks use these).
# ---------------------------------------------------------------------------
_GITIGNORE_COMMIT_MSG = "chore(gitignore): exclude orphaned transients at workday-complete"

# AUTO-GITIGNORE basename/prefix allow-list — pattern written to .gitignore.
_GITIGNORE_BASENAME_EXACT = {
    ".DS_Store": ".DS_Store",
    "Thumbs.db": "Thumbs.db",
}
_GITIGNORE_BASENAME_GLOBS = [
    ("*.log", "*.log"),
    ("*.pid", "*.pid"),
]

# CLAIM (C6) commit-roots-seen label for a path committed BY CLAIM rather
# than by any prefix below -- distinguishes a claim-driven commit-message
# root entry from a genuine `_COMMIT_PREFIXES` match without inventing a
# fake filesystem prefix.
_CLAIM_COMMIT_ROOT_LABEL = "(session claim)"

# AUTO-COMMIT prefix allow-list (path startswith root -> commit_root == root).
_COMMIT_PREFIXES = [
    "cross-repo/inbox/",
    "cross-repo/archive/",
    "state/review-trail/",
    "state/memos/",
    "state/lessons-outbox/",
    "state/improvement-queue/",
    "state/debt-backlog/",
    "state/bug-backlog/",
    "state/lesson-triage-recheck-due-",
    "tasks/learn-lessons-",
    "tasks/audits/",
    "tasks/daily-review-scratch/",
    "archive/",
]

# AUTO-COMMIT docs/plans/ sidecar suffix allow-list (glob-matched full path).
_COMMIT_PLAN_SIDECAR_GLOBS = [
    "docs/plans/*.plan-coverage-check.md",
    "docs/plans/*.prior-art-check.md",
    "docs/plans/*.external-pattern-check.md",
]
_COMMIT_PLAN_SIDECAR_ROOT = "docs/plans/*-check.md"

# SOURCE-TREE root allow-list (path startswith root -> source-tree).
_SOURCE_TREE_PREFIXES = [
    "plugins/",
    "bin/",
    "hooks/",
    "lib/",
    "tests/",
    "schemas/",
    "docs/wiki/",
    "docs/decisions/",
    "setup/",
    "skills/",
    "agents/",
    "snippets/",
    "commands/",
    "docs/plans/",
]


# ---------------------------------------------------------------------------
# Unit 1 — subprocess helper, gitignore file helpers.
# ---------------------------------------------------------------------------


def _run_git(args: List[str], cwd: str) -> subprocess.CompletedProcess:
    """Run a git subcommand with a bounded timeout, no stdin, no console flash."""
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=_GIT_TIMEOUT_SECS,
        creationflags=_CREATIONFLAGS,
    )


def _pattern_in_gitignore(pattern: str, gitignore_path: str) -> bool:
    """Fixed-string, whole-line match — mirrors `grep -qxF`."""
    if not os.path.isfile(gitignore_path):
        return False
    try:
        with open(gitignore_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        print(f"skip: _pattern_in_gitignore: with open(gitignore_path, \"r\", encoding=\"utf-8\", errors=\"replace\") as  failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    return pattern in lines


def _append_to_gitignore(pattern: str, gitignore_path: str, dry_run: bool) -> bool:
    """Returns True on success (or dry-run no-op); False on write failure."""
    if dry_run:
        print(f"[{_PROG}] DRY-RUN: would append '{pattern}' to {gitignore_path}", file=sys.stderr)
        return True
    try:
        with open(gitignore_path, "a", encoding="utf-8") as fh:
            fh.write(f"\n{pattern}\n")
        declare_write(gitignore_path)
        return True
    except OSError:
        print(f"[{_PROG}] ERROR: cannot write to {gitignore_path}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Unit 2 — classification predicates (shared by the main pass and the
# rename-source second pass).
# ---------------------------------------------------------------------------


def _classify_gitignore(path: str, bname: str) -> Optional[str]:
    """Returns the .gitignore pattern to write, or None if not a gitignore hit."""
    if path.startswith("logs/"):
        return "logs/"
    if bname in _GITIGNORE_BASENAME_EXACT:
        return _GITIGNORE_BASENAME_EXACT[bname]
    for glob, pattern in _GITIGNORE_BASENAME_GLOBS:
        if fnmatch.fnmatchcase(bname, glob):
            return pattern
    return None


def _classify_commit(path: str) -> Optional[str]:
    """Returns the matched commit_root, or None if not an AUTO-COMMIT hit."""
    for prefix in _COMMIT_PREFIXES:
        if path.startswith(prefix):
            return prefix
    for glob in _COMMIT_PLAN_SIDECAR_GLOBS:
        if fnmatch.fnmatchcase(path, glob):
            return _COMMIT_PLAN_SIDECAR_ROOT
    return None


def _classify_source_tree(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _SOURCE_TREE_PREFIXES)


def _classify_orphan_tmp(bname: str) -> bool:
    return fnmatch.fnmatchcase(bname, "*.tmp.[0-9]*.[0-9]*")


def _split_rename(rest: str) -> Tuple[Optional[str], str]:
    """Porcelain `XY old -> new`: returns (rename_src or None, path).

    `path` is the text after the LAST ` -> ` (destination may itself
    contain the separator in a pathological name); `rename_src` is the
    text before the FIRST ` -> ` — mirrors the oracle's
    `${rest%% -> *}` / `${rest##* -> }` parameter expansions.

    shell-doc-ok: quotes the oracle's real parameter expansions, whose subject is
    git porcelain's own literal " -> " rename separator.
    """
    if " -> " not in rest:
        return None, rest
    first = rest.find(" -> ")
    last = rest.rfind(" -> ")
    return rest[:first], rest[last + 4 :]


def _record_root(commit_roots_seen: List[str], root: str) -> None:
    if root not in commit_roots_seen:
        commit_roots_seen.append(root)


class _Counters:
    def __init__(self) -> None:
        self.eol = 0
        self.sub = 0
        self.leave = 0
        self.orphan = 0
        self.gitignore = 0
        self.commit = 0
        self.claim_mine = 0  # C6 — committed BY CLAIM, never by prefix
        self.claim_peer = 0  # C6 — reported peer claim, never committed
        self.source = 0
        self.ambiguous = 0


class _Accumulators:
    def __init__(self) -> None:
        self.gitignore_paths: List[str] = []
        self.gitignore_patterns: List[str] = []
        self.commit_paths: List[str] = []
        self.commit_add_paths: List[str] = []
        self.commit_roots_seen: List[str] = []


def _resolve_claim_context(
    repo_root: str,
) -> Optional[Tuple[FrozenSet[str], Dict[str, dict]]]:
    """C6: resolve THIS run's session id and its four-bucket ownership
    readout ONCE, before the main classification loop starts (never
    per-path) -- see module docstring's "Session-id resolution (C6)"
    section for why this calls `resolve_session_id` rather than `main()`
    growing a parameter.

    Returns ``None`` -- the canonical "no claim awareness this run" signal
    -- for every tier this chunk must dispose of explicitly:
      - `resolve_session_id` returned `""` (unresolvable, by ANY of that
        function's own 4-tier ambiguity reasons -- see its docstring; this
        function does not re-derive which tier fired, only consumes the
        single resolved-or-unresolvable signal).
      - `compute_offer` itself raised (a missing/broken `coordinator_core`
        dependency, an unreadable session dir, etc.).
    Either failure degrades this run to EXACTLY pre-C6 behavior: the CLAIM
    branch is skipped in `_classify_main_pass` and every dirty path
    classifies by branches 6-8 alone, same as before this chunk existed --
    fails closed to "no claim awareness", never to a wider commit (AC8).

    On success, returns ``(mine_paths, peer_map)``:
      - ``mine_paths`` -- `frozenset` of `ownership["mine"]`.
      - ``peer_map`` -- `path -> PeerOwnedPath` dict built from
        `ownership["peer"]`. Empty outright when `ownership["degraded"]`
        is `True` -- `_compute_ownership` (`safe_commit_offer.py`) already
        implements exactly that fold (AC7), so this function does not
        re-check `degraded` itself: an empty `peer_map` here means every
        non-mine path this run saw falls through unchanged to branches
        6-8, matching "degraded -> everything non-mine falls back to
        AMBIGUOUS" verbatim.
    """
    try:
        from coordinator_core.ops.session.safe_commit_offer import compute_offer
        from coordinator_core.session import core as _session_core
    except Exception:
        return None

    try:
        sid = _session_core.resolve_session_id(repo_root)
    except Exception:
        return None
    if not sid:
        return None

    try:
        offer = compute_offer(sid, repo_root)
    except Exception:
        return None

    ownership = offer.get("ownership") or {}
    mine_paths: FrozenSet[str] = frozenset(ownership.get("mine") or [])
    peer_map: Dict[str, dict] = {
        entry["path"]: entry for entry in (ownership.get("peer") or [])
    }
    return mine_paths, peer_map


def _classify_main_pass(
    status_lines: List[str],
    repo_root: str,
    counters: _Counters,
    acc: _Accumulators,
    claim_ctx: Optional[Tuple[FrozenSet[str], Dict[str, dict]]] = None,
) -> bool:
    """Runs the main classification loop. Returns needs_pm."""
    needs_pm = False

    for status_line in status_lines:
        if not status_line:
            continue

        xy = status_line[0:2]
        rest = status_line[3:]
        _rename_src, path = _split_rename(rest)

        # 1. EOL-PHANTOM (skip for untracked and rename-destination lines).
        if xy != "??" and not xy.startswith("R"):
            diff_res = _run_git(["diff", "--quiet", "--", path], cwd=repo_root)
            if diff_res.returncode == 0:
                counters.eol += 1
                continue

        # 2. SUBMODULE (skip for untracked).
        if xy != "??":
            stage_res = _run_git(["ls-files", "--stage", "--", path], cwd=repo_root)
            if any(line.startswith("160000") for line in stage_res.stdout.splitlines()):
                counters.sub += 1
                continue

        # 3. LEAVE-ALONE.
        if path.startswith("state/handoffs/") or path.startswith(".git/"):
            counters.leave += 1
            continue

        # 4. ORPHAN-TMP.
        bname = path.rsplit("/", 1)[-1]
        if _classify_orphan_tmp(bname):
            print(f"[{_PROG}] orphan-tmp: {path}", file=sys.stderr)
            print(
                f"[{_PROG}] WARN: inspect {path} against its target before deleting — "
                "not auto-disposed (Edit-tool atomic-write crash artifact)",
                file=sys.stderr,
            )
            counters.orphan += 1
            continue

        # 5. AUTO-GITIGNORE.
        gi_pattern = _classify_gitignore(path, bname)
        if gi_pattern is not None:
            acc.gitignore_paths.append(path)
            acc.gitignore_patterns.append(gi_pattern)
            counters.gitignore += 1
            continue

        # 5.5 CLAIM (C6) — a claim decides before any prefix does, EXCEPT
        # SOURCE-TREE (branch 7): a claim proves WHO wrote a path, never
        # that it is safe for an unattended ceremony to commit unreviewed.
        # SOURCE-TREE exists precisely to force human review of source
        # changes, and this carve-out is unconditional -- a source-tree
        # path provably claimed by the closing session still falls through
        # UNTOUCHED to branch 7 below, exactly as pre-C6 (see the module
        # docstring's CLAIM description and its "SOURCE-TREE carve-out"
        # paragraph for the full rationale). Skipped entirely when
        # `claim_ctx` is None (session unresolvable, or the ownership read
        # failed -- see `_resolve_claim_context`): every path then
        # classifies by branches 6-8 exactly as before C6.
        if claim_ctx is not None and not _classify_source_tree(path):
            mine_paths, peer_map = claim_ctx
            if path in mine_paths:
                acc.commit_paths.append(path)
                if xy[1:2] != "D":
                    acc.commit_add_paths.append(path)
                _record_root(acc.commit_roots_seen, _CLAIM_COMMIT_ROOT_LABEL)
                counters.claim_mine += 1
                continue
            peer_fact = peer_map.get(path)
            if peer_fact is not None:
                print(
                    f"[{_PROG}] peer-claim: {path} owner={peer_fact['owner']} "
                    f"liveness={peer_fact['liveness']} "
                    f"claim_source={peer_fact['claim_source']}",
                    file=sys.stderr,
                )
                counters.claim_peer += 1
                needs_pm = True
                continue
            # Unattributed (or non-mine while ownership["degraded"] --
            # peer_map is already empty in that case): falls through
            # unchanged to AUTO-COMMIT / SOURCE-TREE / AMBIGUOUS below --
            # "stays AMBIGUOUS, rc unchanged" per the plan's own bullet.

        # 6. AUTO-COMMIT.
        commit_root = _classify_commit(path)
        if commit_root is not None:
            acc.commit_paths.append(path)
            if xy[1:2] != "D":
                acc.commit_add_paths.append(path)
            _record_root(acc.commit_roots_seen, commit_root)
            counters.commit += 1
            continue

        # 7. SOURCE-TREE.
        if _classify_source_tree(path):
            print(f"[{_PROG}] source-tree: {path}", file=sys.stderr)
            counters.source += 1
            needs_pm = True
            continue

        # 8. AMBIGUOUS.
        print(f"[{_PROG}] ambiguous: {path}", file=sys.stderr)
        counters.ambiguous += 1
        needs_pm = True

    return needs_pm


def _classify_rename_source_pass(
    status_lines: List[str],
    counters: _Counters,
    acc: _Accumulators,
) -> bool:
    """F8: re-classify rename SOURCE paths against gitignore/commit/source-tree
    only (no EOL/submodule/leave-alone/orphan-tmp on the source). Returns
    whether this pass set needs_pm."""
    needs_pm = False

    for status_line in status_lines:
        if not status_line:
            continue
        rest = status_line[3:]
        if " -> " not in rest:
            continue
        first = rest.find(" -> ")
        src = rest[:first]

        if _classify_source_tree(src):
            print(f"[{_PROG}] source-tree (rename-src): {src}", file=sys.stderr)
            counters.source += 1
            needs_pm = True
            continue

        commit_root = _classify_commit(src)
        if commit_root is not None:
            # Vanished (renamed-away) source: fold into commit_paths only —
            # never commit_add_paths, never a new root, never double-counted
            # (the destination side already recorded root + count).
            acc.commit_paths.append(src)
            continue

        src_bname = src.rsplit("/", 1)[-1]
        gi_pattern = _classify_gitignore(src, src_bname)
        if gi_pattern is not None:
            acc.commit_paths.append(src)
            counters.gitignore += 1
            continue

    return needs_pm


# ---------------------------------------------------------------------------
# Unit 3 — act blocks (AUTO-GITIGNORE, AUTO-COMMIT), summary, verdict.
# ---------------------------------------------------------------------------


def _act_gitignore(repo_root: str, dry_run: bool, counters: _Counters, acc: _Accumulators) -> Optional[int]:
    """Returns an exit code on hard failure, else None."""
    if not acc.gitignore_paths:
        return None

    gi_file = os.path.join(repo_root, ".gitignore")
    patterns_committed: List[str] = []
    gi_committed_paths: List[str] = []

    for path, pattern in zip(acc.gitignore_paths, acc.gitignore_patterns):
        if not _pattern_in_gitignore(pattern, gi_file):
            if not _append_to_gitignore(pattern, gi_file, dry_run):
                return 1

        if pattern not in patterns_committed:
            patterns_committed.append(pattern)

        tracked_res = _run_git(["ls-files", "--error-unmatch", "--", path], cwd=repo_root)
        if tracked_res.returncode == 0:
            if dry_run:
                print(f"[{_PROG}] DRY-RUN: would git rm --cached -- {path}", file=sys.stderr)
            else:
                rm_res = _run_git(["rm", "--cached", "--", path], cwd=repo_root)
                if rm_res.returncode != 0:
                    print(f"[{_PROG}] ERROR: git rm --cached failed for {path}", file=sys.stderr)
                    return 1
            gi_committed_paths.append(path)

    pattern_list = ", ".join(patterns_committed)

    if dry_run:
        print(
            f"[{_PROG}] DRY-RUN: would commit .gitignore + {len(gi_committed_paths)} paths: "
            f"{_GITIGNORE_COMMIT_MSG}",
            file=sys.stderr,
        )
        return None

    add_res = _run_git(["add", "--", gi_file], cwd=repo_root)
    if add_res.returncode != 0:
        print(f"[{_PROG}] ERROR: git add .gitignore failed", file=sys.stderr)
        return 1

    # No explicit pathspec on the commit — the staged index already holds
    # both the .gitignore write and the `git rm --cached` deletions; passing
    # the deleted paths as pathspecs on Windows git re-stages the worktree
    # copies and undoes the rm --cached.
    commit_res = _run_git(["commit", "-m", _GITIGNORE_COMMIT_MSG], cwd=repo_root)
    if commit_res.returncode != 0:
        print(f"[{_PROG}] ERROR: gitignore commit failed", file=sys.stderr)
        return 1

    return None


def _act_commit(repo_root: str, dry_run: bool, acc: _Accumulators) -> Optional[int]:
    """Returns an exit code on hard failure, else None."""
    if not acc.commit_paths:
        return None

    roots_str = ", ".join(acc.commit_roots_seen)
    n_paths = len(acc.commit_paths)
    commit_msg = f"chore: adopt orphaned WT housekeeping at workday-complete ({n_paths} paths under {roots_str})"

    if dry_run:
        print(f"[{_PROG}] DRY-RUN: would commit {n_paths} paths under {roots_str}", file=sys.stderr)
        for p in acc.commit_paths:
            print(f"[{_PROG}] DRY-RUN:   {p}", file=sys.stderr)
        return None

    if acc.commit_add_paths:
        add_res = _run_git(["add", "--"] + acc.commit_add_paths, cwd=repo_root)
        if add_res.returncode != 0:
            print(f"[{_PROG}] ERROR: git add for auto-commit paths failed", file=sys.stderr)
            return 1

    staged_res = _run_git(["diff", "--cached", "--quiet"], cwd=repo_root)
    if staged_res.returncode == 0:
        # Nothing staged — already committed (idempotent second run) or
        # content-identical; no-op, matching the oracle.
        return None

    commit_res = _run_git(["commit", "-m", commit_msg, "--"] + acc.commit_paths, cwd=repo_root)
    if commit_res.returncode != 0:
        print(f"[{_PROG}] ERROR: auto-commit failed", file=sys.stderr)
        return 1

    return None


def _print_summary(counters: _Counters, acc: _Accumulators) -> None:
    if counters.eol > 0:
        print(f"[{_PROG}] EOL-phantom skipped: {counters.eol}")
    if counters.sub > 0:
        print(f"[{_PROG}] submodule skipped: {counters.sub}")
    if counters.leave > 0:
        print(f"[{_PROG}] leave-alone skipped: {counters.leave}")
    if counters.orphan > 0:
        print(f"[{_PROG}] orphan-tmp listed (no action): {counters.orphan}")

    if counters.gitignore > 0:
        # Dedup preserving first-seen order (mirrors the oracle's
        # patterns_committed reuse rather than re-deriving from a joined string).
        seen: List[str] = []
        for pat in acc.gitignore_patterns:
            if pat not in seen:
                seen.append(pat)
        gi_pat_list = ", ".join(seen)
        print(f"[{_PROG}] gitignore + commit: {counters.gitignore} paths, patterns: {gi_pat_list}")

    if counters.commit > 0:
        roots_str2 = ", ".join(acc.commit_roots_seen)
        print(f"[{_PROG}] auto-commit: {counters.commit} paths under {roots_str2}")

    if counters.claim_mine > 0:
        print(f"[{_PROG}] claim-commit (by claim, not prefix): {counters.claim_mine}")
    if counters.claim_peer > 0:
        print(f"[{_PROG}] peer-claim (needs PM, not committed): {counters.claim_peer}")

    if counters.source > 0:
        print(f"[{_PROG}] source-tree (needs PM): {counters.source}")
    if counters.ambiguous > 0:
        print(f"[{_PROG}] ambiguous (needs PM): {counters.ambiguous}")


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def main(argv: List[str]) -> int:
    # --- Unit 1: parse args ---
    dry_run = False
    for arg in argv:
        if arg == "--dry-run":
            dry_run = True
        else:
            print(f"ERROR: unknown argument: {arg}", file=sys.stderr)
            return 1

    # --- Unit 1: locate repo root ---
    try:
        toplevel_res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_SECS,
            creationflags=_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"[{_PROG}] ERROR: not inside a git repo", file=sys.stderr)
        return 1
    if toplevel_res.returncode != 0 or not toplevel_res.stdout.strip():
        print(f"[{_PROG}] ERROR: not inside a git repo", file=sys.stderr)
        return 1
    repo_root = toplevel_res.stdout.strip()

    # --- Unit 1: counters/accumulators ---
    counters = _Counters()
    acc = _Accumulators()

    # --- Unit 2: gather git status once, classify twice (main + rename-source) ---
    try:
        status_res = _run_git(["status", "--porcelain", "--untracked-files=all"], cwd=repo_root)
    except (OSError, subprocess.TimeoutExpired):
        print(f"[{_PROG}] ERROR: git status failed", file=sys.stderr)
        return 1
    if status_res.returncode != 0:
        print(f"[{_PROG}] ERROR: git status failed", file=sys.stderr)
        return 1
    status_lines = status_res.stdout.splitlines()

    # --- Unit 2: C6 — resolve this run's claim context ONCE, not per path.
    claim_ctx = _resolve_claim_context(repo_root)

    needs_pm = _classify_main_pass(status_lines, repo_root, counters, acc, claim_ctx)
    needs_pm = _classify_rename_source_pass(status_lines, counters, acc) or needs_pm

    # --- Unit 3: act blocks ---
    rc = _act_gitignore(repo_root, dry_run, counters, acc)
    if rc is not None:
        return rc
    rc = _act_commit(repo_root, dry_run, acc)
    if rc is not None:
        return rc

    # --- Unit 3: summary + verdict ---
    _print_summary(counters, acc)

    if needs_pm:
        print(f"[{_PROG}] NEEDS-PM")
        return 2

    print(f"[{_PROG}] OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
