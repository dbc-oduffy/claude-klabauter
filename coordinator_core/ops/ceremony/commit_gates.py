"""
coordinator_core.ops.ceremony.commit_gates -- native ports of the deleted
`check-workstream-complete-deletion-blocks.sh` (164 LOC) and `dirty-tree-gate.sh`
(187 LOC), the C3 chunk of the `wsc_tail` rebuild
(docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md).

Two gates, both pure-Python classification over `git` state (routed through
`git_native`'s single Windows-safe subprocess choke point -- AC3):

  deletion_block_gate() -- validates the composed commit message's
      "Deleted (Step 2.67):" / "Kept (Step 2.67):" blocks (see
      `commit_message.compose_message`, C2) against staged reality:
        Assertion-1 -- every Deleted-claimed path is staged for deletion.
        Assertion-2 -- every Kept-claimed path exists at HEAD or in the
                        staged set.
        Assertion-3 (F3, the inverse check) -- staged deletions present but
                        no Step 2.67 block at all is a fail (the EM forgot
                        to account for them).
      Scoped to `gate_paths` (C2's dual-path-set output) -- NOT the whole
      index -- so a concurrent sibling session's own staged deletion outside
      `gate_paths` can never trip Assertion-3 through this caller (this is
      the exact false-positive the 2026-07-07 example-cockpit-repo incident
      reported; the deleted bash gate's own header documents the same fix).
      Skip-gate-when-empty: an empty `gate_paths` AND no Step 2.67 block in
      the message is a legitimate no-deletions session -- skipped entirely,
      not scored as an ambiguous pass.

  dirty_tree_gate() -- three-way classification of every `git status
      --porcelain` entry: (a) staged (this session's own pending commit),
      (b) known-concurrent-owner (path falls inside the `scope:` block of a
      `state/handoffs/*.md` that carries `consumed_by:` -- another live
      session owns it), (c) unattributable. An EOL-phantom filter runs
      before classification: a tracked-unstaged path absent from a single
      batched `git diff --name-only -- <all candidate paths>` (worktree
      content already equals the index -- a Git-for-Windows stat-staleness
      artifact) is benign, never (c) -- one `git diff` call classifies every
      tracked-unstaged porcelain line at once instead of one call per line
      (docs/plans/2026-08-07-n-plus-one-git-spawn-class-and-amplification-
      gate.md § C1; see `_diff_name_only_worktree()` below for why this is
      the correct inversion of a per-path `git diff --quiet`). Case
      (c) paths are reported, NEVER auto-stashed or auto-adopted -- see the
      module-level negative-spec below.
      Optionally scoped to `gate_paths` (2026-07-22, mirroring
      `deletion_block_gate`'s existing scoping): when `gate_paths` is
      supplied (including an EMPTY sequence), a path that would otherwise
      classify as (c) is only reported if it also falls inside
      `gate_paths` -- a dirty path OUTSIDE the caller's own pathspec is
      none of that caller's business and is silently excluded from
      `unattributable`, not merely down-ranked. `gate_paths=None` (the
      default) preserves the original unfiltered behaviour -- this is
      DELIBERATELY NOT the same as an empty sequence; see the parameter
      doc on `dirty_tree_gate()` for why empty-means-unfiltered was
      rejected as the sentinel.

Both gates report (never raise) -- callers get a typed outcome with a
diagnostics list, mirroring the bash originals' "print diagnostics, set exit
code" shape rather than a Python exception flow (the caller, `commit_pipeline`
C4, decides how to surface a failed gate to the op's result envelope).

Spec backlink: pln-rebuild-the-wsc-commit-ceremon-f7c2a0 § C3 (AC10).
Provenance: ported from `DoE:coordinator/bin/check-workstream-complete-deletion-blocks.sh`
  and `DoE:coordinator/bin/dirty-tree-gate.sh` (both still present on disk at
  `/Users/example-operator/X/DoE-claude/coordinator/bin/` at port time -- not yet deleted
  by the kill list at the time this chunk was authored).

Negative-spec (hard-won, preserved from the bash originals):
  - deletion_block_gate() does NOT re-derive gate_paths -- the caller (C2's
    `compute_gate_paths`) is the sole source; this module only reads it.
  - dirty_tree_gate() does NOT auto-stash, auto-commit, or auto-adopt any
    case-(c) unattributable path -- disposition stays a human/EM judgment
    call in the calling skill's prose (the bash original's load-bearing
    refusal; see its own header for the 2026-07-07 incident that motivated
    it). This module only classifies and reports.
  - dirty_tree_gate()'s `gate_paths` scoping does NOT re-derive the scope
    set -- exactly like deletion_block_gate(), the caller (C2's
    `compute_gate_paths`) is the sole source; this module only reads it.
    Motivated by a 2026-07-22 incident: a sibling repo's
    `/workstream-complete` ran `ceremony.wsc_tail` on a shared work branch
    with concurrent peer sessions, and the (then-unscoped) gate reported
    ~33 unattributable paths, every one belonging to a peer session and
    NONE inside the caller's own `stage_paths` -- on a shared branch this
    is the normal operating condition, so an unscoped gate makes the
    commit gesture unusable concurrently.
  - The sentinel is `None` = unfiltered, NOT empty-sequence = unfiltered
    (corrected 2026-07-22, same day as the fix above -- the first cut of
    this fix used empty-means-unfiltered and left the ORIGINAL incident
    reachable: `commit_pipeline.run_commit_pipeline` computes
    `gate_paths = compute_gate_paths(stage.staged_paths, deleted_paths)`,
    which is `[]`, not `None`, whenever a caller passes empty
    `stage_paths`/`deleted_paths` -- exactly the shape a `/workstream-
    complete` invocation with no local changes of its own takes on a
    shared branch. Under empty-means-unfiltered that `[]` would have run
    the gate UNFILTERED over the whole tree and reproduced the incident
    this fix exists to close. `run_commit_pipeline` now short-circuits to
    a benign no-op before either gate runs whenever `commit_paths` is
    empty (see its own docstring), so in practice it never calls
    `dirty_tree_gate` with an empty-but-non-None `gate_paths` -- but the
    function's own contract must not silently rely on that caller
    discipline to stay safe).
  - `dirty_tree_gate`'s scoped case-(c) set is DEGENERATE BY CONSTRUCTION
    at `commit_pipeline`'s call site, and this module does not overstate
    it: `explicit_stage` only ever stages a path that exists on disk (a
    `git add` failure short-circuits the pipeline before either gate
    runs), so every path in `stage.staged_paths` carries a non-blank
    porcelain X status char and is caught by dirty_tree_gate's OWN case-(a)
    filter before scoping is even consulted -- no staged path can reach
    case (c). The only `gate_paths` member that CAN reach case (c) is a
    `deleted_paths` entry removed from the worktree but never staged for
    deletion -- and `deletion_block_gate`'s Assertion-1, running on the
    immediately preceding line at the same call site, already blocks that
    exact condition with a more specific diagnostic ("NOT staged for
    deletion"). `run_commit_pipeline` keeps `dirty_tree_gate` scoped-and-
    BLOCKING anyway, but honestly: as FAIL-CLOSED DEFENCE-IN-DEPTH against
    a future widening of `compute_gate_paths` or a future caller
    constructing `gate_paths` by hand -- not because the scoped classifier
    carries independent signal at today's single call site. A docstring
    that claimed otherwise would be a lie a future reader trusts.
  - `ceremony.wsc_commit`'s advisory-only posture on the classifier
    `ops.dirty_tree_gate` invokes (commit 58b0f7e5, 2026-07-06) and this
    module's scoped-blocking posture are the SAME conclusion reached
    independently, not a deliberate divergence: both exist because a
    whole-tree unattributable-path gate is redundant once the caller
    already commits an explicit pathspec -- `wsc_commit` expresses that by
    never blocking on the gate's result, this module expresses it by
    narrowing what the gate is even allowed to report on. See
    `ceremony/wsc_commit.py:2037`'s `_run_dirty_tree_gate` docstring for
    the cross-pointer back to here.
  - `dirty_tree_gate`'s scoped (`gate_paths` not `None`) code path does NOT
    spawn `git diff-files`/`git ls-files` to enumerate the dirty tree and
    then filter it down -- DR-227 (docs/decisions/DR-227-whole-tree-dirty-
    classifier-redundant-under-explicit-path-scoping.md, 2026-08-26
    follow-up) proves the scoped case-(c) set is empty by construction for
    the staged half of `gate_paths`, and its one reachable member (an
    unstaged deletion) is answerable with `read_index()` + `os.path.exists()`
    at zero spawns. Do NOT re-add a `_dirty_candidate_paths` call, and do NOT
    add a stat/content-based worktree comparison to cover the diverged-bytes
    axis in the scoped path -- that axis is unreachable through the real
    caller and a naive stat/byte comparison reproduces the exact
    autocrlf false-positive class the reverted `da156a723` attempt shipped
    (measured: 326/400 clean tracked files MISMATCH on this repo's
    `core.autocrlf=true`). The unscoped (`gate_paths=None`) path is
    unaffected and still uses `_dirty_candidate_paths`.
  - Neither gate shells out to bash, node, or awk -- all parsing is native
    Python string/regex work; every git read routes through
    `git_native._git` (AC2/AC3), never a bare `subprocess.run`.
  - The em-dash split in Kept-block parsing uses a literal Python string
    (U+2014, three UTF-8 bytes 0xE2 0x80 0x94) -- Python has no BSD-awk vs
    gawk octal-vs-hex portability landmine to reproduce; noted here, not
    carried forward.
  - `tracked_at_head` (the Kept-claim existence check) is intentionally left
    UNSCOPED to `gate_paths` -- it is a HEAD snapshot, not staged state, and
    the bash original scopes only the staged reads. It is, however, scoped
    to `parsed.kept_claimed` itself: since C3 (2026-08-21) both legs of
    Assertion-2 route through `coordinator_core.git.git_state`
    (`read_index` for the staged set, `head_blobs(cwd, parsed.kept_claimed)`
    for HEAD membership -- a targeted lookup over the handful of
    Kept-claimed paths, never `git`'s own unscopeable `ls-tree -r HEAD`
    walk over the whole tree) with no `git` spawn at all. Both reads are
    still issued only when the parsed message actually carries a
    Kept-claim, since no other assertion consumes either set -- a spawn/
    read-count reduction, never a widening.
"""

from __future__ import annotations

import ast
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple, Union

from coordinator_core.claim_state import resolve_claim_state
from coordinator_core.git.git_state import IndexParseError, head_blobs, read_index
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.ops.ceremony.git_native import (
    GitResult,
    _chunk_paths,
    _git,
    diff_cached_name_status,
)
from coordinator_core.ops.handoff_carry_gate import CarryGateError, evaluate_gate, read_carried_items

#: U+2014 EM DASH, surrounded by single spaces -- matches
#: `commit_message.EM_DASH_SEPARATOR`; duplicated here (not imported) since
#: this module's own negative-spec is "no cross-module coupling beyond git
#: reads" for the block-parsing logic -- both modules independently encode
#: the same bash-original literal.
_EM_DASH_SEPARATOR = " — "

_DELETED_HEADER_RE = re.compile(r"^Deleted \(Step 2\.67\):\s*$")
_KEPT_HEADER_RE = re.compile(r"^Kept \(Step 2\.67\):\s*$")
_ANY_BLOCK_HEADER_RE = re.compile(r"^[A-Z][a-z]+ \(Step 2\.67\):\s*$")
_FOOTER_RE = re.compile(r"^--- end Step 2\.67 blocks ---$")
_HAS_BLOCK_RE = re.compile(r"^(Deleted|Kept) \(Step 2\.67\):", re.MULTILINE)


@dataclass(frozen=True)
class ParsedBlocks:
    """Result of parsing a composed commit message's Step-2.67 blocks.

    Fields:
        deleted_claimed -- bare paths claimed in the "Deleted (Step 2.67):"
            block, one per non-blank/non-indented line.
        kept_claimed -- paths successfully split from "<path> EM_DASH <reason>"
            lines in the "Kept (Step 2.67):" block.
        kept_malformed -- raw Kept-block lines that had no em-dash separator
            (unparseable -- always a diagnostic/mismatch, never silently
            dropped).
    """

    deleted_claimed: List[str] = field(default_factory=list)
    kept_claimed: List[str] = field(default_factory=list)
    kept_malformed: List[str] = field(default_factory=list)


def has_step267_block(msg_text: str) -> bool:
    """True iff the message contains a "Deleted (Step 2.67):" or "Kept (Step 2.67):" header.

    Purpose: the F3 skip-gate-when-empty predicate and the Assertion-3
    inverse-check predicate both key on this.
    """
    return bool(_HAS_BLOCK_RE.search(msg_text))


def parse_step267_blocks(msg_text: str) -> ParsedBlocks:
    """Parse the Deleted/Kept Step-2.67 blocks out of a composed commit message.

    Purpose: byte-reproduces the bash original's two-pass awk state machine
    (one pass per block type there; a single pass here since Python has no
    equivalent per-block subshell-capture friction) as a single line-by-line
    scan. Block-end is the next "Word (Step 2.67):" header OR the literal
    footer line -- blank lines INSIDE a block do NOT terminate it (paragraph
    grouping permitted, matching the awk `/^[^[:space:]]/` guard, which only
    ever matched non-blank lines to begin with).
    """
    deleted_claimed: List[str] = []
    kept_claimed: List[str] = []
    kept_malformed: List[str] = []

    in_del = False
    in_kept = False
    for line in msg_text.splitlines():
        if _DELETED_HEADER_RE.match(line):
            in_del, in_kept = True, False
            continue
        if _KEPT_HEADER_RE.match(line):
            in_del, in_kept = False, True
            continue
        if _ANY_BLOCK_HEADER_RE.match(line):
            in_del, in_kept = False, False
            continue
        if _FOOTER_RE.match(line):
            in_del, in_kept = False, False
            continue

        # Non-blank, non-leading-whitespace lines only (mirrors awk's
        # `/^[^[:space:]]/` -- blank lines never match, so they never
        # terminate the block and are never claimed as content).
        if not line or line[0].isspace():
            continue

        if in_del:
            deleted_claimed.append(line)
        elif in_kept:
            idx = line.find(_EM_DASH_SEPARATOR)
            if idx > 0:
                kept_claimed.append(line[:idx])
            else:
                kept_malformed.append(line)

    return ParsedBlocks(
        deleted_claimed=deleted_claimed,
        kept_claimed=kept_claimed,
        kept_malformed=kept_malformed,
    )


@dataclass(frozen=True)
class GateOutcome:
    """Typed result of `deletion_block_gate()`.

    Fields:
        passed -- True iff every claim matched staged reality (or the gate
            was skipped -- a skip is always a pass).
        skipped -- True iff the skip-gate-when-empty rule fired (empty
            `gate_paths` AND no Step 2.67 block in the message).
        diagnostics -- human-readable mismatch lines, empty when `passed`.
    """

    passed: bool
    skipped: bool
    diagnostics: List[str] = field(default_factory=list)


def _parse_name_status_deletions(name_status_stdout: str) -> List[str]:
    """Extract pure-deletion paths ("D\\t<path>") from `git diff --name-status` output.

    Purpose: the Python equivalent of `git diff --cached --diff-filter=D
    --name-only` without adding a new git_native wrapper for a diff-filter
    variant -- this module already needs the fuller `--name-status` output
    for nothing else, so filtering client-side keeps the git_native surface
    unchanged. Rename lines ("R100\\told\\tnew") are intentionally excluded --
    `--diff-filter=D` in the bash original only ever matched pure deletions,
    never renames. This exclusion is preserved for Assertion-3's own use of
    this function's output -- see `_parse_name_status_rename_sources` below
    for the SEPARATE, narrower reader Assertion-1 needs.
    """
    deletions: List[str] = []
    for line in name_status_stdout.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] == "D":
            deletions.append(parts[1])
    return deletions


def _parse_name_status_rename_sources(name_status_stdout: str) -> List[str]:
    """Extract rename SOURCE paths ("R<score>\\t<old>\\t<new>") from `git diff
    --name-status --find-renames` output.

    Purpose (2026-08-06 fix, live incident -- a move-set commit, ~15
    changelog blocks + ~300 review-trail records relocated into archive
    directories, refused with "Deleted-claim NOT staged for deletion" for
    every moved path, even after the caller staged the deletions itself
    first): `commit_pipeline.explicit_stage` classifies a path that
    vanished from the worktree and reappeared elsewhere as a genuine
    deletion (`StageOutcome.deletion_paths`), and `run_commit_pipeline`
    composes a "Deleted (Step 2.67):" claim for it -- correctly, the old
    path is gone. But once BOTH the vacated source and the identical-
    content destination are staged in the SAME `git add` batch, git's own
    `--find-renames` diff pairs them into one `R100 old new` line instead
    of a `D old` + `A new` pair -- `_parse_name_status_deletions` above
    deliberately excludes rename lines (see its own docstring), so
    Assertion-1 (below) saw the source path as NOT staged for deletion at
    all, and failed a claim the pipeline's own staging logic had made
    accurate.

    This is a SEPARATE, narrower reader of the same `--name-status` output,
    used ONLY by Assertion-1 (below) to recognize that a rename source path
    has genuinely vacated its old location in the staged tree -- the same
    fact `explicit_stage` already used to justify the Deleted claim in the
    first place. Assertion-3's F3 inverse check keeps using
    `_parse_name_status_deletions` UNCHANGED (rename lines still excluded
    there) -- an ordinary content-preserving rename must not be forced to
    carry a Step 2.67 block just because this function also exists; only
    Assertion-1's "does this CLAIM match staged reality" question widens.
    """
    sources: List[str] = []
    for line in name_status_stdout.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].startswith("R"):
            sources.append(parts[1])
    return sources


def _staged_deletions_and_renames_in_process(
    cwd: Union[str, Path], gate_scope: Set[str]
) -> Tuple[Set[str], Set[str]]:
    """`(staged_deletions, rename_sources)` for `gate_scope`, in-process (no
    `git` spawn) -- the POST-`git add` counterpart of `commit_pipeline.
    _swept_rename_delete_paths` (same exact-(mode,sha)-match rename test),
    with its OWN fresh read of the CURRENT (post-add) index rather than
    reusing `explicit_stage`'s pre-add snapshot -- byte-identical `git diff
    --cached --name-status --find-renames` argv, but a different index state
    either side of the `git add` (2026-08-26, C2b of docs/dispatch-briefs/
    2026-08-26-the-commit-op-stops-asking-git-eleven-times/C2b.md).

    A `gate_scope` member present at HEAD but absent from the current index
    is either a swept DELETE or a swept RENAME: an exact `(mode, sha)` match
    against some OTHER current-index path is a spawn-free proof of a
    100%-identical rename (an ordinary `git mv`/add+rm of unmodified
    content); no exact match reads as a plain delete.

    Never called with an empty `gate_scope` -- there is no bounded candidate
    set to check `head_blobs` against without walking the whole HEAD tree,
    so the caller keeps the unscoped `diff_cached_name_status` spawn for
    that case (mirroring `deletion_block_gate`'s own unscoped/`whole_index`
    branch).

    Raises `IndexParseError` on an unmerged (mid-merge-conflict) index --
    caller decides the fallback, mirroring `_swept_rename_delete_paths`'s
    own contract.
    """
    staged_deletions: Set[str] = set()
    rename_sources: Set[str] = set()
    if not gate_scope:
        return staged_deletions, rename_sources

    index_snapshot = read_index(cwd)
    scoped_paths = sorted(gate_scope)
    head_result = head_blobs(cwd, scoped_paths)
    if not head_result:
        return staged_deletions, rename_sources

    sha_to_paths: Dict[str, List[str]] = {}
    for ip, entry in index_snapshot.items():
        sha_to_paths.setdefault(entry.sha, []).append(ip)

    for p in scoped_paths:
        head_entry = head_result.get(p)
        if head_entry is None:
            continue
        if p in index_snapshot:
            continue
        _, head_sha_value = head_entry
        candidates = [ip for ip in sha_to_paths.get(head_sha_value, []) if ip != p]
        if candidates:
            rename_sources.add(p)
        else:
            staged_deletions.add(p)

    return staged_deletions, rename_sources


def deletion_block_gate(
    msg_text: str,
    gate_paths: Sequence[str],
    *,
    cwd: Union[str, Path],
    whole_index: bool = False,
) -> GateOutcome:
    """Validate a composed commit message's Step-2.67 blocks against staged reality.

    Purpose: the C3 AC10 deletion-block gate port. See module docstring for
    the three assertions and the skip-gate-when-empty / scoped-to-gate_paths
    rules.

    AC3/AC11 (docs/plans/2026-08-13-claim-release-deadlock-and-the-doctrine-
    that-rejects-it.md): protects against a commit message whose Step-2.67
    Deleted/Kept claims diverge from staged reality -- git itself has no
    notion of commit-MESSAGE-vs-diff consistency, only diff content. Every
    call recomputes this from current git state; nothing is cached or held
    across calls. Outlet: fix the message body or re-stage to match the
    claims, then re-invoke -- a fresh sub-second gate call, no human wait
    (the CLI's own remedy text at the bottom of this module states the same
    retry path).

    Params:
        msg_text   -- the composed commit message (from `commit_message.
                       compose_message`).
        gate_paths -- C2's `compute_gate_paths()` output; the scope every
                      staged read below is filtered to (except
                      `tracked_at_head`, intentionally unscoped -- see
                      negative-spec). Ignored (treated as always-unscoped)
                      when `whole_index=True`.
        cwd        -- the git worktree root the gate reads staged/HEAD state
                      from.
        whole_index -- when True, disables the skip-gate-when-empty
                      shortcut. `gate_paths=[]` is ALREADY unscoped in the
                      staged-read filtering below regardless of this flag
                      (an empty `gate_scope` set falls through to the
                      unfiltered branch) -- the flag exists ONLY to
                      distinguish two different meanings of an empty
                      `gate_paths`: C2's "nothing in scope, skip entirely"
                      (default, `whole_index=False`) vs. the CLI
                      trampoline's "no pathspec given, check the WHOLE
                      staged index" (`whole_index=True`) -- the bash
                      original's standalone/no-pathspec mode, where an
                      empty `gate_paths` must NOT short-circuit past the F3
                      inverse check.
    """
    has_block = has_step267_block(msg_text)
    gate_scope: Set[str] = set(gate_paths)

    if not whole_index and not gate_paths and not has_block:
        return GateOutcome(passed=True, skipped=True, diagnostics=[])

    parsed = parse_step267_blocks(msg_text)

    # Assertion-1 needs a WIDER set than Assertion-3 does (2026-08-06 fix --
    # see `_parse_name_status_rename_sources`'s own docstring for the
    # incident): a Deleted-claimed path whose old location git paired into a
    # rename (identical/near-identical content staged at a new path in the
    # SAME batch) has genuinely vacated that path in the staged tree, even
    # though it is not a bare `D` line. Scoped by `gate_scope` the same way
    # `staged_deletions` is.
    #
    # In-process (`_staged_deletions_and_renames_in_process`, no `git`
    # spawn) whenever `gate_scope` is non-empty -- the common, scoped
    # caller shape. `IndexParseError` (mid-merge-conflict index) falls back
    # to the original unscoped `diff --cached --name-status --find-renames`
    # spawn, filtered client-side, mirroring `_swept_rename_delete_paths`'s
    # own fallback contract. The unscoped case (`gate_scope` empty --
    # `whole_index=True`'s no-pathspec mode) has no bounded candidate set to
    # check `head_blobs` against, so it keeps the spawn unconditionally.
    if gate_scope:
        try:
            staged_deletions_set, rename_sources_set = _staged_deletions_and_renames_in_process(
                cwd, gate_scope
            )
        except IndexParseError:
            name_status_result = diff_cached_name_status(cwd, find_renames=True)
            all_staged_deletions = _parse_name_status_deletions(name_status_result.stdout)
            staged_deletions_set = {p for p in all_staged_deletions if p in gate_scope}
            all_rename_sources = _parse_name_status_rename_sources(name_status_result.stdout)
            rename_sources_set = {p for p in all_rename_sources if p in gate_scope}
        staged_deletions = sorted(staged_deletions_set)
    else:
        name_status_result = diff_cached_name_status(cwd, find_renames=True)
        staged_deletions = _parse_name_status_deletions(name_status_result.stdout)
        staged_deletions_set = set(staged_deletions)
        rename_sources_set = set(_parse_name_status_rename_sources(name_status_result.stdout))

    staged_or_renamed_away_set = staged_deletions_set | rename_sources_set

    # Assertion-2's two reads answer ONE question -- does a Kept-claimed path
    # still exist -- and nothing else in this function consumes either set, so
    # a message with no Kept-claims skips both reads outright rather than
    # computing and discarding them. Both legs are now index-vs-HEAD lookups
    # via `git_state` (no `git` spawn for the staged-set leg; the HEAD-tree
    # leg is `head_blobs`, scoped to exactly `parsed.kept_claimed` -- a
    # membership check on a handful of paths, never the unscopeable full
    # `ls-tree -r HEAD` walk (~27k paths on this repo) the old form issued).
    # Kept-claims are the rare shape on this commit path; the empty case is
    # the hot one.
    staged_all_set: Set[str] = set()
    tracked_at_head: Set[str] = set()
    diagnostics: List[str] = []
    if parsed.kept_claimed:
        # `read_index` raises `IndexParseError` on ANY unmerged (stage != 0)
        # entry -- an ordinary mid-merge-conflict repo state, not only a
        # malformed index (git_state.py:47-50's contract: it never degrades
        # silently). Every other production caller of `read_index` /
        # `_index_blobs` catches this; this gate must too, or a conflicted
        # index turns "gate computes a refusal" into "the commit op crashes"
        # (code-review finding F1). Matches `_index_blobs`'s own posture
        # (git_native.py `_GIT_READ_FAILED`): a read it cannot answer is a
        # refusal, never a pass, and the refusal stays visible in
        # `diagnostics` rather than failing silently.
        try:
            all_staged = list(read_index(cwd).keys())
        except IndexParseError as exc:
            diagnostics.append(
                f"Kept-claim check: staged index unreadable ({exc}) -- "
                "degraded read, gate refuses rather than guessing"
            )
        else:
            staged_all = [p for p in all_staged if p in gate_scope] if gate_scope else all_staged
            staged_all_set = set(staged_all)

        tracked_at_head = set(head_blobs(cwd, parsed.kept_claimed).keys())

    # Assertion-1: every Deleted-claimed path MUST be staged for deletion --
    # a bare `D` line, OR the vacated source of a staged rename (see
    # `staged_or_renamed_away_set`'s own comment, immediately above).
    for path in parsed.deleted_claimed:
        if path not in staged_or_renamed_away_set:
            diagnostics.append(f"Deleted-claim NOT staged for deletion: {path}")

    # Assertion-2: every Kept-claimed path MUST exist (HEAD or staged).
    for path in parsed.kept_claimed:
        if path not in tracked_at_head and path not in staged_all_set:
            diagnostics.append(f"Kept-claim does not exist at HEAD or in staged set: {path}")

    # Malformed Kept lines (no em-dash separator) -- always a mismatch.
    for raw in parsed.kept_malformed:
        diagnostics.append(
            "Kept-line has no em-dash separator (unparseable, expected "
            f"'<path> — <reason>'): {raw}"
        )

    # Assertion-3 (F3, the inverse check): staged deletions IN SCOPE present
    # but no Step 2.67 block at all in the message is a fail. Scoped to
    # gate_paths -- a sibling session's own staged deletion outside
    # gate_paths never trips this (parity assertion (e)).
    if staged_deletions and not has_block:
        diagnostics.append("Staged deletions present but commit body has no Step 2.67 block:")
        for path in staged_deletions:
            diagnostics.append(f"  {path}")

    return GateOutcome(passed=not diagnostics, skipped=False, diagnostics=diagnostics)


# ---------------------------------------------------------------------------
# Dirty-tree gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DirtyTreeOutcome:
    """Typed result of `dirty_tree_gate()`.

    Fields:
        passed          -- True iff every dirty path was attributable
                            (phantom / staged / known-concurrent-owner).
        unattributable  -- case-(c) paths, in `git status --porcelain` order.
                            NEVER auto-disposed by this module -- see
                            negative-spec.
    """

    passed: bool
    unattributable: List[str] = field(default_factory=list)


_SCOPE_ITEM_RE = re.compile(r"^  - (.+)$")


def _extract_scope_paths(handoff_text: str) -> List[str]:
    """Extract `scope:` block list-item paths from a handoff's frontmatter.

    Purpose: the Python equivalent of the bash original's awk idiom
    (`/^scope:/{found=1; next} found && /^  - /{print substr($0, 5)} found
    && /^---/{exit} found && /^[a-z]/{exit}`) -- byte-identical stop
    conditions: a line starting with a lowercase letter at column 0 (the
    next top-level frontmatter key) or a `---` line (frontmatter close)
    both terminate the scope block.
    """
    paths: List[str] = []
    in_scope = False
    for line in handoff_text.splitlines():
        if line.startswith("scope:"):
            in_scope = True
            continue
        if not in_scope:
            continue
        m = _SCOPE_ITEM_RE.match(line)
        if m:
            paths.append(m.group(1))
            continue
        if line.startswith("---"):
            break
        if line[:1].isalpha() and line[:1].islower():
            break
    return paths


def _build_known_scope(worktree_root: Path) -> Set[str]:
    """Union of `scope:` paths from every claimed `state/handoffs/*.md`.

    Purpose: case-(b) known-concurrent-owner membership set for
    `dirty_tree_gate()`. A handoff is "claimed" (another live session owns
    its scope) when `coordinator_core.claim_state.resolve_claim_state`
    reports a live holder -- ledger-first, frontmatter mirror as fallback.

    Spec backlink: pln-claim-state-make-the-ledger-th-6641e3
    § C3 / AC4. Before this fix, a live ledger claim with a branch-reverted
    mirror (no `claimed_by`/`consumed_by`) dropped its `scope:` paths from
    `known_scope` entirely, reclassifying the claim holder's own in-progress
    files as case-(c) unattributable.

    `common_dir` is resolved once per call, not once per handoff, per C1's
    hot-path cost note (`git_common_dir` is itself `lru_cache`d, so this is a
    dict-lookup saving, not a subprocess-avoidance one).
    """
    known_scope: Set[str] = set()
    handoffs_dir = worktree_root / "state" / "handoffs"
    if not handoffs_dir.is_dir():
        return known_scope

    try:
        common_dir = git_common_dir(worktree_root)
    except Exception:
        common_dir = None

    for hf in sorted(handoffs_dir.glob("*.md")):
        if not hf.is_file():
            continue
        # Review: coordinator:code-reviewer C3 P3 — renamed from `claim_state`,
        # which shadowed the sibling module `coordinator_core.claim_state`
        # this function imports `resolve_claim_state` from.
        resolved_claim = resolve_claim_state(hf, common_dir=common_dir, repo_root=worktree_root)
        if resolved_claim.holder is None:
            continue
        try:
            text = hf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            print(f"skip: _build_known_scope: text = hf.read_text(encoding=\"utf-8\", errors=\"replace\") failed: {sys.exc_info()[1]}", file=sys.stderr)
            continue
        known_scope.update(_extract_scope_paths(text))

    return known_scope


def _diff_name_only_worktree(cwd: Union[str, Path], paths: Sequence[str]) -> GitResult:
    """`git diff --name-only -- <paths>` — batched worktree-vs-index real-diff set.

    Purpose: the batched replacement for calling `git diff --quiet` once per
    tracked-unstaged porcelain line in `dirty_tree_gate()`'s EOL-phantom
    filter (docs/plans/2026-08-07-n-plus-one-git-spawn-class-and-
    amplification-gate.md § C1). One `git diff --name-only` invocation,
    scoped to every tracked-unstaged candidate path in a single call,
    returns exactly the subset WITH a real diff; any candidate absent from
    `stdout` is a phantom (worktree content already equals the index -- a
    Git-for-Windows stat-staleness artifact). Deliberately NOT "pass all
    paths to one `git diff --quiet`", which collapses to a single yes/no for
    the whole set and loses per-path resolution -- this call inverts that:
    the returned name list IS the per-path resolution.

    DELIBERATELY NOT `--no-optional-locks`, unlike `status_porcelain`
    (imported from `git_native`) and every other read wrapper in
    `git_native.py`. Suppressing the optional lock makes git compute the
    stat-cache refresh in memory and discard it, so a lock-suppressed read
    can never CLEAR phantom-dirty state -- only a real lock-taking write
    does. Adding it here would leave every phantom permanently dirty and
    re-filtered on each ceremony (the flapping-count symptom in
    DoE-claude's bash-on-windows-gotchas.md § 11) -- this omission is
    deliberate, not an oversight; do not "fix" it back in to match the
    house read-wrapper idiom.

    `paths` is the caller's own tracked-unstaged candidate set -- never
    called unscoped. Empty `paths` short-circuits to an empty, `ok=True`
    result without spawning a subprocess (an unscoped `git diff --name-only`
    with no trailing pathspec would scan the whole tree, never the intent of
    a caller that passed nothing to scope to).
    """
    if not paths:
        return GitResult(returncode=0, stdout="", stderr="")
    return _git(["diff", "--name-only", "--", *paths], cwd=cwd)


def _dirty_candidate_paths(cwd: Union[str, Path], paths: Optional[Sequence[str]] = None) -> List[str]:
    """Every path `dirty_tree_gate` could classify, WITHOUT `git status`.

    Purpose: `status --porcelain` costs ~1500ms on a 36k-file worktree and
    scoping the pathspec does not move it (measured 1543-1600ms scoped to four
    paths, against 1431-1710ms unscoped) -- because `status` also runs the
    index-vs-HEAD tree comparison, and THAT is the expensive half
    (`diff-index --cached HEAD` alone: 1487ms; `diff-files`: 48ms). At a 2.0s
    `ipc.CEREMONY_BUDGET_SECS`, one such call is 75% of the whole budget, and
    `ceremony.commit` was timing out on 28 of 31 calls because of it.
    → docs/research/2026-08-26-the-ceremony-budget-is-spent-on-one-git-status.md

    `dirty_tree_gate` does not USE the index-vs-HEAD half it was paying for:
    it recomputes staged-ness itself from `read_index` + `head_blobs` (see
    `_is_staged` there), having explicitly stopped trusting status's own X
    column. So the two cheap halves are the whole requirement:

      - `diff-files` -- tracked paths whose worktree bytes diverge from the
        index, INCLUDING worktree deletions. The index-vs-worktree axis.
      - `ls-files --others --exclude-standard` -- untracked, non-ignored
        paths. Exactly status's `??` population (status excludes ignored too).

    Equivalence, not approximation: the paths this drops relative to `status`
    are the index-vs-HEAD-only ones -- staged-with-clean-worktree, and staged
    deletions -- and those are precisely the paths `dirty_tree_gate`'s
    classification loop `continue`s on at its FIRST check (`_is_staged` →
    skip). A path it would have reported cannot be one this omits.

    Deliberately NOT `-M`/`--find-renames`: a rename staged in the index is an
    index-vs-HEAD fact, already skipped as staged. On the worktree axis the two
    halves report the old path (via `diff-files`, as a deletion) and the new one
    (via `ls-files --others`) separately, which is the same pair of paths
    status's `XY DEST\\0SRC\\0` rename record resolves to under
    `_iter_porcelain_z_paths`. No rename record means no second field to
    consume, so no record-aware reader is needed here.

    `-z` for the same reason `_status_porcelain_unquoted` uses it: these paths
    key `read_index`/`head_blobs` lookups verbatim, and both are unquoted. `-z`
    is the only shape that disables git's C-quoting unconditionally.

    Fail-open on a failed chunk, preserving `dirty_tree_gate`'s pre-existing
    behaviour EXACTLY -- it never checked `status_result.ok` either, so a git
    failure yielded an empty path list and a passing gate. Preserved rather
    than fixed here because this change ships under a live outage and must not
    also change what the gate refuses; the fail-open is filed separately.

    Chunked against the Windows argv cap, as its predecessor was.
    """
    both: List[str] = []
    for base in (
        ["--no-optional-locks", "diff-files", "-z", "--name-only"],
        # `--directory --no-empty-directory` is load-bearing, not tidying:
        # `status --porcelain` collapses a wholly-untracked directory to ONE
        # `dir/` record, and this gate's two filters (`known_scope` from a
        # handoff `scope:` entry, and `gate_scope` exact-match) key on that
        # collapsed shape. Enumerating the directory's files instead turns one
        # attributable `state/subagent-share/<sid>/` entry into N unmatched
        # per-file entries -- measured here as 34 collapsed paths becoming 216
        # file paths, every one of them a fresh false "unattributable".
        [
            "--no-optional-locks", "ls-files", "-z", "--others",
            "--exclude-standard", "--directory", "--no-empty-directory",
        ],
    ):
        if paths is None:
            result = _git(base, cwd=cwd)
            if not result.ok:
                return []
            both.append(result.stdout)
            continue
        for chunk in _chunk_paths(list(paths)):
            result = _git([*base, "--", *chunk], cwd=cwd)
            if not result.ok:
                return []
            both.append(result.stdout)

    seen: Set[str] = set()
    ordered: List[str] = []
    for field in "".join(both).split("\x00"):
        if field and field not in seen:
            seen.add(field)
            ordered.append(field)
    return ordered


def _status_porcelain_unquoted(cwd: Union[str, Path], paths: Optional[Sequence[str]] = None) -> GitResult:
    """`git status --porcelain -z`, RAW/unquoted paths -- REGRESSION-2 fix.

    `status_porcelain()` (git_native.py) deliberately keeps git's default
    C-quoting (`"like\\ this"`-style escaping of a space/non-ASCII/control
    byte in a path) so every OTHER caller of that shared wrapper keeps
    parsing the same shape it always has -- see that function's own
    docstring. `dirty_tree_gate` below cannot share that call: it keys
    `read_index`/`head_blobs` lookups on the porcelain path verbatim, and
    those two are never quoted (`read_index` decodes the index's raw UTF-8
    bytes; `head_blobs` reads `ls-tree -z`, itself NUL-delimited/unquoted).
    A quoted porcelain path therefore misses both lookups and falls through
    to "unattributable" -- a real, clean tracked-unstaged edit silently
    misreported as a phantom-widening false positive.

    `-c core.quotepath=false` alone does NOT close this gap -- measured, not
    assumed: it only suppresses octal-escaping of non-ASCII bytes, but git
    still wraps a plain space-containing path in `"..."` quotes regardless
    of that setting. `-z` is the only shape that disables quoting
    unconditionally (git's own NUL-delimited machine-readable mode), per
    this chunk's dispatch brief's preference for fixing at the source over
    hand-writing a C-unquote decoder. `-z` changes the RECORD shape too, not
    only the quoting: no trailing newline, and a rename/copy record is
    `XY DEST\\0SRC\\0` (two NUL-terminated fields, dest first) instead of the
    v1 human-readable `XY SRC -> DEST` line -- `_iter_porcelain_z_paths()`
    below is the record-aware reader this shape requires; `_porcelain_path()`
    (the ` -> ` splitter) is NOT reused here, it parses the other shape.

    Chunked identically to `status_porcelain()` against the Windows argv
    cap; any failed chunk short-circuits and is returned as-is."""
    base = ["--no-optional-locks", "status", "--porcelain", "-z"]
    if paths is None:
        return _git(base, cwd=cwd)

    combined: List[str] = []
    for chunk in _chunk_paths(list(paths)):
        result = _git([*base, "--", *chunk], cwd=cwd)
        if not result.ok:
            return result
        combined.append(result.stdout)
    return GitResult(returncode=0, stdout="".join(combined), stderr="")


def _iter_porcelain_z_paths(stdout: str) -> List[str]:
    """Read a `git status --porcelain -z` stream, yielding one DEST path per
    record (matching `_porcelain_path()`'s "only the destination matters"
    contract for the ` -> ` v1 shape, adapted to `-z`'s NUL-delimited
    `XY DEST\\0SRC\\0` rename/copy record). A plain (non-rename) record is
    `XY PATH\\0`, one field, RAW bytes -- no quoting to strip."""
    fields = stdout.split("\x00")
    paths: List[str] = []
    i = 0
    while i < len(fields):
        field = fields[i]
        if field == "":
            i += 1
            continue
        # Fixed-width prefix, NOT `str.partition(" ")` -- the XY code's
        # first slot is itself a literal space for most statuses (` M`,
        # ` D`, ...), so partitioning on the first space would swallow the
        # code into an empty string and treat "M" (or "D") as part of the
        # path. `XY PATH` is exactly a 2-char code, one space, then the raw
        # path -- position 2 is the space, position 3 is where PATH starts.
        code = field[:2]
        path = field[3:]
        paths.append(path)
        # A rename/copy record's first character class ("R"/"C" in either
        # XY slot) is followed by a SECOND NUL-terminated field (the SOURCE
        # path) that this reader must consume and discard so it is never
        # mistaken for the next record's own field.
        if "R" in code or "C" in code:
            i += 1
        i += 1
    return paths


def _porcelain_path(line: str) -> str:
    """Extract the (destination) path from one `git status --porcelain` line.

    Purpose: handles the rename/copy "orig -> dest" format -- callers only
    ever care about the destination path (matching the bash original's
    `${path##* -> }` parameter expansion).

    shell-doc-ok: quotes the bash original's real parameter expansion, whose
    subject is git porcelain's own literal " -> " rename separator.
    """
    path = line[3:]
    if " -> " in path:
        path = path.rsplit(" -> ", 1)[-1]
    return path


def dirty_tree_gate(
    worktree_root: Union[str, Path],
    gate_paths: Optional[Sequence[str]] = None,
) -> DirtyTreeOutcome:
    """Three-way classify every dirty working-tree path; report case-(c) paths.

    Purpose: the C3 AC10 dirty-tree gate port. See module docstring for the
    classification rules (staged / EOL-phantom / known-concurrent-owner /
    unattributable) and the auto-stash refusal negative-spec.

    AC3/AC11 (docs/plans/2026-08-13-claim-release-deadlock-and-the-doctrine-
    that-rejects-it.md): protects against committing over a dirty path this
    session cannot attribute to itself OR to a known concurrent claim holder
    -- git's own status has no concept of session ownership, only tracked/
    untracked/staged. Every call re-reads `git status --porcelain` and the
    live handoff scope fresh; no lock or lease is held between calls. Outlet:
    the SAME session/EM disposes the path itself -- stage it, claim it via a
    handoff `scope:` entry, or narrow `gate_paths` to exclude it -- and
    re-invokes; a fresh sub-second gate call, no human wait. The negative-
    spec's "human/EM judgment call" already names the EM as a sufficient
    in-band decider -- disposition never requires waiting on a human
    specifically, only refuses to auto-guess on the caller's behalf.

    Params:
        worktree_root -- the git worktree root; also where `state/handoffs/`
                          is resolved for the known-concurrent-owner scan.
        gate_paths     -- optional scope for case-(c) reporting only, mirroring
                          `deletion_block_gate`'s `gate_paths` parameter, with
                          ONE deliberate divergence in the empty-sequence
                          sentinel:
                            `None` (the default)  -- unfiltered. Every
                              case-(c) path is reported, matching both
                              existing callers (`ops.dirty_tree_gate`'s CLI
                              trampoline, a wholly separate implementation
                              this function does not back, and any direct
                              unscoped caller of this function itself).
                            `[]` (empty, non-None) -- scoped to NOTHING.
                              Every otherwise-(c) path is excluded; the gate
                              always passes. This is deliberately NOT the
                              same as `None` -- see the module negative-spec
                              for the 2026-07-22 incident this distinction
                              fixes (a `[]`-means-unfiltered sentinel left
                              the reported incident reachable through
                              `commit_pipeline`'s own empty-pathspec case).
                            non-empty -- a path that would otherwise
                              classify as (c) is reported ONLY if it also
                              falls inside this set -- a dirty path outside
                              the caller's own pathspec (a live peer
                              session's file on a shared branch, the normal
                              case) is excluded, never scored against this
                              caller.

                          A non-empty `gate_paths` takes a DIFFERENT,
                          zero-`git`-spawn code path than `None` (see DR-227,
                          docs/decisions/DR-227-whole-tree-dirty-classifier-
                          redundant-under-explicit-path-scoping.md, 2026-08-26
                          follow-up): rather than enumerating the whole dirty
                          tree via `git diff-files`/`git ls-files` and
                          filtering it down, the scoped case classifies each
                          `gate_paths` member directly against `read_index()`
                          and `os.path.exists()`. This does not attempt the
                          diverged-bytes axis (see the classification loop's
                          own comment) -- DR-227 proves that axis unreachable
                          through the real caller, and `None` still covers it
                          for any caller that needs it.
    """
    root = Path(worktree_root)
    scoped = gate_paths is not None
    gate_scope: Set[str] = set(gate_paths) if gate_paths else set()

    unattributable: List[str] = []

    # Scoped to NOTHING (`gate_paths == []`, the empty-but-non-None sentinel
    # documented above): every otherwise-(c) path is excluded at the filter
    # below, so the whole-tree walk that feeds it is read and then discarded
    # in full. Return the passing outcome without spawning `git` at all.
    if scoped and not gate_scope:
        return DirtyTreeOutcome(passed=True, unattributable=[])

    if scoped:
        # DR-227 (docs/decisions/DR-227-whole-tree-dirty-classifier-
        # redundant-under-explicit-path-scoping.md), 2026-08-26 follow-up:
        # the vacuity argument proves case (c) is empty by construction for
        # the staged half of `gate_paths` -- every staged path is already
        # caught by case (a) before scoping is even consulted (see this
        # module's own "degenerate by construction" negative-spec above).
        # The only member that CAN reach case (c) through the real caller
        # (`run_commit_pipeline`) is an unstaged deletion: an index entry
        # whose worktree path has vacated without ever being staged for
        # deletion. Answered here with `os.path.exists()` over the caller's
        # own `gate_scope` at zero `git` spawns, retiring the two
        # `_dirty_candidate_paths` subprocesses (`git diff-files` +
        # `git ls-files --others`) this scoped path used to pay for just to
        # re-derive a set already known to be empty.
        #
        # A second, symmetric axis -- untracked (no index entry, present on
        # disk) -- is answered the same zero-spawn way, purely as
        # fail-closed defence-in-depth (DR-227's own rationale) against a
        # future caller hand-building `gate_paths` with a path that was
        # never staged at all; today's real caller cannot produce one
        # (`compute_gate_paths` only ever unions `stage.staged_paths` and
        # `deleted_paths`).
        #
        # DELIBERATELY NOT attempted here: the diverged-bytes axis (a
        # tracked, unstaged path whose worktree CONTENT differs from the
        # index). That axis is BLOCKED absent a filter-aware in-process
        # hash -- git's own content comparison (autocrlf, other filters)
        # cannot be reproduced with a stat/byte check without reproducing
        # the exact false-positive class the reverted `da156a723` attempt
        # shipped (measured on this repo: 326/400 clean autocrlf-normalized
        # tracked files MISMATCH under a naive worktree-bytes-vs-index-sha
        # comparison). Per DR-227, this axis is unreachable through the real
        # caller anyway -- every non-deletion `gate_paths` member is already
        # staged and excluded by case (a) before this classification runs.
        # The unscoped branch below (`gate_paths=None`) is untouched and
        # still covers this axis via `git diff-files` for its own callers.
        try:
            index_snapshot = read_index(root)
        except IndexParseError as exc:
            return DirtyTreeOutcome(
                passed=False,
                unattributable=[f"<index unreadable: {exc}>"],
            )

        for path in sorted(gate_scope):
            in_index = path in index_snapshot
            on_disk = os.path.exists(root / path)
            if in_index == on_disk:
                # Both true: staged/clean, or the blocked diverged-bytes
                # axis -- not answered by this fast path. Both false: not
                # a member of either reachable axis at all.
                continue
            unattributable.append(path)

        return DirtyTreeOutcome(passed=not unattributable, unattributable=unattributable)

    # Unscoped (`gate_paths=None`) -- the original whole-tree walk, retained
    # for `ops.dirty_tree_gate`'s CLI trampoline and any other direct
    # unscoped caller of this function. DR-227's vacuity argument does not
    # apply here: there is no caller-supplied candidate set to bound the
    # search to, so the diverged-bytes axis still needs `git diff-files`'s
    # real content comparison.
    all_paths: List[str] = _dirty_candidate_paths(root, None)
    known_scope = _build_known_scope(root)

    # (a)'s "staged" classification no longer trusts git status's own X
    # column -- it is recomputed from `read_index` + `head_blobs` (the
    # index-vs-HEAD half, no worktree read), per this chunk's resolution
    # of the worktree-axis fork: a path is staged iff its index entry
    # diverges from (or is absent from) HEAD, or the path has vacated the
    # index entirely while still present at HEAD (a staged deletion).
    # `read_index` raises `IndexParseError` on ANY unmerged (stage != 0)
    # entry -- an ordinary mid-merge-conflict repo state (git_state.py:47-50's
    # contract: it never degrades silently). Left uncaught this turned "gate
    # computes an unattributable verdict" into "the commit op crashes"
    # (code-review finding F1). This gate's own stated preference for a read
    # it cannot answer is "unattributable", never a silent pass -- fail
    # toward that, with a marker entry so the degraded read stays visible in
    # `unattributable` rather than looking like an ordinary clean tree.
    try:
        index_snapshot = read_index(root)
    except IndexParseError as exc:
        return DirtyTreeOutcome(
            passed=False,
            unattributable=[f"<index unreadable: {exc}>"],
        )
    head_result = head_blobs(root, all_paths) if all_paths else {}

    def _is_staged(path: str) -> bool:
        entry = index_snapshot.get(path)
        head_entry = head_result.get(path)
        if entry is not None:
            if head_entry is None:
                return True
            return (entry.mode, entry.sha) != head_entry
        return head_entry is not None

    # The EOL-phantom filter that used to sit here is GONE, and its absence is
    # the point rather than an omission: it was a second whole-tree `git diff`
    # (`_diff_name_only_worktree`, measured 1272ms -- the gate's single largest
    # cost, larger than the `status` call) whose only job was to re-check, by
    # content, paths that `git status --porcelain` had reported dirty on a STAT
    # mismatch alone. Git-for-Windows stat staleness makes that population real,
    # so against a status-sourced candidate list the filter was load-bearing.
    #
    # `_dirty_candidate_paths` no longer sources from `status`. `diff-files` is
    # a diff: when the stat says dirty it hashes the worktree bytes before
    # emitting, so a phantom cannot reach `all_paths` in the first place. Not
    # merely argued -- measured on this worktree, both directions:
    #
    #     status --porcelain (tracked)   n=72   756ms
    #     diff-files --name-only         n=59    67ms
    #     git diff --name-only (content) n=59  1324ms
    #     phantoms status reported that diff-files already dropped : 13
    #     phantoms that LEAKED through diff-files                  :  0
    #     real diffs diff-files MISSED                             :  0
    #
    # The filter was stripping a population that can no longer occur. Should
    # that ever stop holding, the symptom is LOUD and fail-closed -- a phantom
    # would classify as (c) and refuse the commit, naming the path -- never a
    # silent pass. → docs/research/2026-08-26-the-ceremony-budget-is-spent-on-
    # one-git-status.md
    for path in all_paths:
        # (a) Staged: this session's own pending commit.
        if _is_staged(path):
            continue

        # (b) Known concurrent owner.
        if path in known_scope:
            continue

        # Out-of-scope for THIS caller (gate_paths is not None -- scoped --
        # and path not in gate_scope, which may itself be empty) -- still
        # dirty/unattributable in absolute terms, but not this caller's
        # business to report. Skipped BEFORE appending to `unattributable`,
        # not merely excluded from `passed` -- callers that inspect
        # `unattributable` directly must not see it either. `scoped` (not
        # `gate_scope` truthiness) gates this -- an EMPTY-but-non-None
        # `gate_paths` must still exclude everything, not fall through to
        # unfiltered.
        if scoped and path not in gate_scope:
            continue

        # (c) Unattributable (in scope, or gate_paths is None -- unscoped).
        unattributable.append(path)

    return DirtyTreeOutcome(passed=not unattributable, unattributable=unattributable)


# ---------------------------------------------------------------------------
# Carry gate
# ---------------------------------------------------------------------------

_HANDOFF_PATH_RE = re.compile(r"^state/handoffs/[^/]+\.md$")

_CARRY_GATE_RESTAGE_HINT = (
    "carry_gate: refused paths are left UNSTAGED -- fix the carried_items "
    "entries above and re-stage (the tree will show `?? state/...` for them "
    "until you do)."
)


def carry_gate(
    worktree_root: Union[str, Path],
    gate_paths: Sequence[str],
) -> GateOutcome:
    """Refuse a staged `state/handoffs/*.md` whose `carried_items` declare
    undeclared state -- a third gate beside `deletion_block_gate` and
    `dirty_tree_gate`, modelled on the latter (same module, same
    `GateOutcome` shape).

    Purpose: `docs/plans/2026-08-10-the-carry-gate-the-commit-pipeline-never-
    asked-for.md` § C1 (AC1-AC7). Delegates every rule to
    `coordinator_core.ops.handoff_carry_gate.evaluate_gate` -- this function
    does not re-implement the carry_id/disposition/disposition_detail rules,
    it only decides WHICH staged paths to run them against and how a
    violation reaches this pipeline's `diagnostics`.

    Scope: filtered to `gate_paths` entries matching `state/handoffs/*.md`
    (single path segment under `state/handoffs/`, mirroring
    `_build_known_scope`'s own non-recursive `handoffs_dir.glob("*.md")`
    scan above). An empty filtered set returns `skipped=True` without
    reading a single file (AC5) -- this gate has nothing to say about a
    commit that stages no handoff.

    Per-path outcome:
      - The path is ABSENT from the worktree at gate time (AC8) -- a
        legitimate skip, not a refusal, checked via `(root / path).exists()`
        BEFORE `read_carried_items` is ever called (never via catching the
        `OSError` that call would otherwise raise -- that would re-open
        AC7, see below). A path only reaches this gate at all because
        `commit_message.compute_gate_paths` put it in `gate_paths` --
        `[*commit_paths, *deleted_paths]` -- which folds in every
        EM-authored `deleted_paths` entry alongside staged content; a
        deliberate `git rm state/handoffs/*.md` (`/distill` disposal, any
        handoff removal) is exactly that: an entry with no file behind it
        by design, not a read failure. Archival *swept* renames never reach
        `gate_paths` this way -- `compute_commit_paths` folds those in
        separately -- so this skip is scoped to genuine EM-authored
        deletions only.
      - The path EXISTS but `read_carried_items` raises `CarryGateError`
        (unparseable frontmatter, `carried_items` not a list) or `OSError`
        (permissions, race, or any other read failure) -- a REFUSAL, not a
        skip (AC7), mirroring `baton_assemble.apply.
        _dispatch_handoff_carry_gate`'s own unreadable-vs-refusal
        distinction one layer up. The existence check above means this
        branch's `OSError` is never the ordinary "file is being deleted"
        case -- only a genuinely present-but-unreadable file reaches it.
      - `read_carried_items` returns `[]` (absent `carried_items` key, or an
        explicitly empty array) -- `evaluate_gate([])` is `ok=True`; this is
        a legitimate pass here, NOT the vacuous-pass hazard the plan's
        Anti-scope names -- that hazard was about authoring-time validation
        with no staged-path precondition; this gate only ever fires on a
        staged handoff, so absence of the FIELD (as opposed to absence of
        the FILE, the AC8 case above) genuinely means "nothing carried".
      - `evaluate_gate(items)` returns `ok=False` -- every violation line is
        appended to `diagnostics` VERBATIM (AC3), prefixed only with the
        path, never re-worded.

    Any refusal appends `_CARRY_GATE_RESTAGE_HINT` once (AC6) -- the
    pipeline leaves a refused path unstaged; the operator must re-stage
    after fixing the entries above.

    AC3/AC11 (docs/plans/2026-08-13-claim-release-deadlock-and-the-doctrine-
    that-rejects-it.md): protects against a staged handoff whose
    `carried_items` claim state the entries themselves don't actually
    satisfy -- git stages file bytes, it has no notion of this field's
    semantics. Every call re-reads and re-evaluates the staged file fresh;
    nothing persists between calls. Outlet: the EM authoring the handoff
    fixes the flagged entries per the diagnostic (`_CARRY_GATE_RESTAGE_HINT`
    above), re-stages, and re-invokes -- a fresh sub-second gate call, no
    human wait.
    """
    root = Path(worktree_root)
    handoff_paths = [p for p in gate_paths if _HANDOFF_PATH_RE.match(p)]

    if not handoff_paths:
        return GateOutcome(passed=True, skipped=True, diagnostics=[])

    diagnostics: List[str] = []

    for path in handoff_paths:
        # AC8: absence is the deletion signal, checked BEFORE the read --
        # never via catching read_carried_items' own OSError, which would
        # also swallow a genuinely unreadable EXISTING file and re-open AC7.
        if not (root / path).exists():
            continue

        try:
            items = read_carried_items(str(root / path))
        except CarryGateError as exc:
            diagnostics.append(
                f"{path}: carry_gate: unparseable carried_items -- {exc}"
            )
            continue
        except OSError as exc:
            diagnostics.append(f"{path}: carry_gate: could not read handoff -- {exc}")
            continue

        result = evaluate_gate(items)
        if not result.ok:
            for violation in result.violations:
                diagnostics.append(f"{path}: {violation}")

    if diagnostics:
        diagnostics.append(_CARRY_GATE_RESTAGE_HINT)

    return GateOutcome(passed=not diagnostics, skipped=False, diagnostics=diagnostics)


# ---------------------------------------------------------------------------
# Op-scope coverage gate
# ---------------------------------------------------------------------------

_REGISTRY_MAP_RELPATH = "coordinator_core/ops/_registry_map.py"
_OP_SCOPES_RELPATH = "coordinator_core/op_scopes.py"
_OP_MODULE_MAP_VAR = "OP_MODULE_MAP"
_OP_KEY_SCOPE_VAR = "_OP_KEY_SCOPE"

_OP_SCOPE_GATE_REMEDY = (
    "Add each to _OP_KEY_SCOPE (coordinator_core/op_scopes.py) with a justified "
    "'none'/'common_dir'/'show_top' verdict -- never default to 'none' by omission."
)


class _MultipleModuleBindingsError(Exception):
    """Raised by `_extract_dict_str_keys` when `var_name` is bound more than once
    at module level.

    Purpose: the gate's established contract is "cannot evaluate -> refuse, never
    guess" (see that function's own docstring). A second module-level rebind of
    the target name is exactly the case `ast.walk`'s first-match-wins traversal
    used to resolve silently -- reading the FIRST binding and staying blind to a
    later, authoritative one (e.g. `VAR: Dict[str, str] = {}` followed by a real
    `VAR = {...}` rebind, never a `.update()`). Refusing here, rather than
    picking the last binding, keeps the gate consistent with its own no-guessing
    contract even though "return the last one" would happen to be right for that
    shape -- a guess that is right today and silently wrong for some other
    rebind order tomorrow is the dead-guard hazard this whole gate exists to
    close.
    """

    def __init__(self, var_name: str) -> None:
        super().__init__(var_name)
        self.var_name = var_name


def _extract_dict_str_keys(source: str, var_name: str, *, filename: str) -> Optional[Set[str]]:
    """Statically extract the string-literal keys of a module-level `<var_name> = {...}`
    dict literal, via `ast.parse` -- no import.

    Purpose: `op_scope_coverage_gate()` below needs the key sets of
    `_registry_map.OP_MODULE_MAP` and `op_scopes._OP_KEY_SCOPE` on a commit hot path.
    Importing either module to read them is not acceptable there -- an import runs
    arbitrary module-level code (both modules are otherwise side-effect-free today,
    but this gate must not depend on that staying true forever, and `_registry_map.py`'s
    own docstring states it must never import an op module itself) -- so this reads the
    literal dict AST instead, exactly as `write_surface_manifest._declares_write_surface`
    already does for a narrower "does this bind a name" question.

    Returns `None` (never an empty set as a substitute) when `var_name` is not bound to
    a dict literal anywhere at module level, or the source fails to parse -- both are
    "the predicate could not be evaluated", which the caller must treat as a refusal, not
    a silent zero-violations pass (a dead guard is the exact failure class this gate
    exists to prevent). Only `ast.Constant` string keys are collected; a non-literal key
    (an f-string, a variable) cannot be resolved statically and is simply not counted --
    today neither table uses one.

    Scoped deliberately to MODULE-LEVEL bindings only (`tree.body`, not
    `ast.walk(tree)`): a same-named local inside a function body is not a
    binding of the module attribute this gate cares about and must never
    count toward the multiplicity check below.

    Raises `_MultipleModuleBindingsError(var_name)` when more than one
    module-level `Assign`/`AnnAssign` binds `var_name` -- see that
    exception's own docstring for why refusing beats guessing which
    binding is authoritative.
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return None

    def _keys_from_dict(dict_node: ast.expr) -> Optional[Set[str]]:
        if not isinstance(dict_node, ast.Dict):
            return None
        return {
            key.value
            for key in dict_node.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }

    bindings: List[Optional[ast.expr]] = []
    for node in tree.body:
        # `OP_MODULE_MAP: Dict[str, str] = {...}` and `_OP_KEY_SCOPE:
        # Dict[str, str] = {...}` are both type-annotated module-level
        # bindings (`ast.AnnAssign`), not bare `ast.Assign` -- both real
        # tables use this form, so this must check both node types (mirrors
        # `write_surface_manifest._declares_write_surface`'s own
        # `Assign`-then-`AnnAssign` pair for the same reason).
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == var_name:
                    bindings.append(node.value)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == var_name:
                bindings.append(node.value)

    if len(bindings) > 1:
        raise _MultipleModuleBindingsError(var_name)
    if not bindings:
        return None
    value = bindings[0]
    if value is None:
        return None
    return _keys_from_dict(value)


def op_scope_coverage_gate(
    worktree_root: Union[str, Path],
    gate_paths: Sequence[str],
) -> GateOutcome:
    """Refuse a commit staging `_registry_map.py` that would register an op with no
    matching `_OP_KEY_SCOPE` entry.

    Purpose: `_registry_map.OP_MODULE_MAP` and `op_scopes._OP_KEY_SCOPE` are two
    independently-maintained tables -- registering an op in the former carries no
    structural requirement to add it to the latter, and an omitted entry silently
    defaults to `"none"` scope (see `op_scopes.py`'s own module docstring) rather than
    failing loud. Three ops (`ceremony.chunk_commits`, `chain_ancestry_waivers.reap`,
    `scratchpad.sweep`) shipped this way at once; for `chain_ancestry_waivers.reap` the
    default was actively wrong -- its REMOVE-ONLY reaper fell back to the engine
    process's cwd instead of the caller's worktree, exactly the state its own module
    docstring carries a DO-NOT-RUN-AGAINST-THE-LIVE-TREE warning for. This gate is the
    commit-time backstop `test_dispatch_message.py::
    test_op_key_scope_table_covers_all_registered_ops` already proves as a predicate,
    but that test only fires when someone happens to run that file -- this makes the
    same check unavoidable at the point the omission is introduced.

    Direction is deliberately one-way: only `OP_MODULE_MAP - _OP_KEY_SCOPE` (a
    registered-but-unclassified op) is a violation. `_OP_KEY_SCOPE` legitimately carries
    more entries than `OP_MODULE_MAP` (225 vs 222 at gate-authoring time) -- test-only or
    legacy scope entries with no live registry counterpart are not a defect this gate has
    any business flagging.

    Scope filter: `skipped=True`, no file read at all, unless `_registry_map.py` is
    among `gate_paths` -- a commit that does not touch the registry has nothing for this
    gate to say, mirroring `carry_gate`'s own cheap-skip shape.

    Absence classes (the three cases a naive port of this predicate gets wrong):
      - `_registry_map.py` absent from the worktree (a staged deletion) -> SKIP, not a
        refusal -- there is no `OP_MODULE_MAP` left to check coverage for.
      - `op_scopes.py` absent, or its `_OP_KEY_SCOPE` dict literal cannot be located --
        REFUSE. `op_scopes.py` need not itself be staged (it is read from the worktree
        regardless, same as `_registry_map.py`); this gate cannot verify coverage
        without it and must not pass silently in that state.
      - Either file's target dict is not found by the AST walk (renamed, restructured,
        assigned via something other than a literal `{...}`, or the source fails to
        parse) -> REFUSE, naming which dict was not found. A gate that passes when its
        own predicate fails to evaluate is a dead guard -- the exact failure class the
        omission-default-to-"none" defect above already demonstrates once.
      - Either target name is bound MORE THAN ONCE at module level (e.g. a placeholder
        `VAR: Dict[str, str] = {}` followed by a later genuine `VAR = {...}` rebind) ->
        REFUSE, naming the variable and that it is bound more than once. Which binding is
        authoritative cannot be known statically without re-deriving Python's own
        execution order; guessing "last wins" would be right for that shape and silently
        wrong for some other rebind order -- refusing is consistent with every other
        "predicate could not be evaluated" case above. See `_extract_dict_str_keys`'s own
        docstring and `_MultipleModuleBindingsError`.

    AC3/AC11 (docs/plans/2026-08-13-claim-release-deadlock-and-the-doctrine-
    that-rejects-it.md): protects against an op silently defaulting to
    `"none"` scope for lack of an `_OP_KEY_SCOPE` entry -- git has no notion
    of this cross-table registration invariant, only file content. Every
    call re-reads both source files and re-parses their dict literals fresh;
    nothing persists between calls. Outlet: the EM authoring the op
    registration adds the missing entries per `_OP_SCOPE_GATE_REMEDY`
    (printed in the diagnostics above), re-stages, and re-invokes -- a fresh
    sub-second gate call, no human wait.
    """
    gate_scope: Set[str] = set(gate_paths)
    if _REGISTRY_MAP_RELPATH not in gate_scope:
        return GateOutcome(passed=True, skipped=True, diagnostics=[])

    root = Path(worktree_root)
    registry_path = root / _REGISTRY_MAP_RELPATH

    if not registry_path.exists():
        # Staged deletion of the registry map itself -- nothing left to check
        # coverage for; a legitimate skip, never a refusal.
        return GateOutcome(passed=True, skipped=True, diagnostics=[])

    try:
        registry_source = registry_path.read_text(encoding="utf-8")
    except OSError as exc:
        return GateOutcome(
            passed=False,
            skipped=False,
            diagnostics=[
                f"op_scope_coverage_gate: could not read {_REGISTRY_MAP_RELPATH} -- {exc}"
            ],
        )

    try:
        registered_ops = _extract_dict_str_keys(
            registry_source, _OP_MODULE_MAP_VAR, filename=str(registry_path)
        )
    except _MultipleModuleBindingsError as exc:
        return GateOutcome(
            passed=False,
            skipped=False,
            diagnostics=[
                f"op_scope_coverage_gate: `{exc.var_name}` is bound more than once at "
                f"module level in {_REGISTRY_MAP_RELPATH} -- refusing (cannot tell which "
                "binding is authoritative)"
            ],
        )
    if registered_ops is None:
        return GateOutcome(
            passed=False,
            skipped=False,
            diagnostics=[
                f"op_scope_coverage_gate: could not locate a `{_OP_MODULE_MAP_VAR} = {{...}}` "
                f"dict literal in {_REGISTRY_MAP_RELPATH} -- refusing (a parse failure must "
                "not read as \"no violations\")"
            ],
        )

    scopes_path = root / _OP_SCOPES_RELPATH
    if not scopes_path.exists():
        return GateOutcome(
            passed=False,
            skipped=False,
            diagnostics=[
                f"op_scope_coverage_gate: {_OP_SCOPES_RELPATH} is absent from the worktree "
                "-- cannot verify op-scope coverage, refusing"
            ],
        )

    try:
        scopes_source = scopes_path.read_text(encoding="utf-8")
    except OSError as exc:
        return GateOutcome(
            passed=False,
            skipped=False,
            diagnostics=[
                f"op_scope_coverage_gate: could not read {_OP_SCOPES_RELPATH} -- {exc}"
            ],
        )

    try:
        scoped_ops = _extract_dict_str_keys(
            scopes_source, _OP_KEY_SCOPE_VAR, filename=str(scopes_path)
        )
    except _MultipleModuleBindingsError as exc:
        return GateOutcome(
            passed=False,
            skipped=False,
            diagnostics=[
                f"op_scope_coverage_gate: `{exc.var_name}` is bound more than once at "
                f"module level in {_OP_SCOPES_RELPATH} -- refusing (cannot tell which "
                "binding is authoritative)"
            ],
        )
    if scoped_ops is None:
        return GateOutcome(
            passed=False,
            skipped=False,
            diagnostics=[
                f"op_scope_coverage_gate: could not locate a `{_OP_KEY_SCOPE_VAR} = {{...}}` "
                f"dict literal in {_OP_SCOPES_RELPATH} -- refusing (a parse failure must not "
                "read as \"no violations\")"
            ],
        )

    unclassified = sorted(registered_ops - scoped_ops)
    if not unclassified:
        return GateOutcome(passed=True, skipped=False, diagnostics=[])

    diagnostics: List[str] = [
        f"op_scope_coverage_gate: {len(unclassified)} op(s) registered in "
        f"{_REGISTRY_MAP_RELPATH} with no _OP_KEY_SCOPE entry:"
    ]
    diagnostics.extend(f"  {name}" for name in unclassified)
    diagnostics.append(_OP_SCOPE_GATE_REMEDY)
    return GateOutcome(passed=False, skipped=False, diagnostics=diagnostics)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

_PROG_NAME = "check-workstream-complete-deletion-blocks.sh"


def _parse_cli_args(argv: Sequence[str]) -> Optional[tuple]:
    """Parse `<msg_file> [-- <pathspec>...]`; returns None on a usage error.

    Purpose: reproduces the bash original's argv shape byte-for-byte --
    a bare `--` with zero trailing paths is equivalent to omitting it
    (whole-index mode), matching the original's header comment.
    """
    if len(argv) < 1:
        return None
    msg_file = argv[0]
    rest = list(argv[1:])
    pathspec: List[str] = []
    if rest and rest[0] == "--":
        pathspec = rest[1:]
    return (msg_file, pathspec)


def main(argv: Sequence[str]) -> int:
    """CLI entry point: validate a prepared commit message's Step-2.67 blocks.

    Purpose: a fail-loud gate -- callers rely on exit codes 0/1/2/3 exactly
    as the bash original defined them; see module docstring's Provenance note.

    Exit codes (reproduced verbatim from the bash original):
        0 -- all claims match staged reality (gate green; safe to commit)
        1 -- claim mismatch (fix commit body or re-stage)
        2 -- usage error (missing arg, msg_file unreadable)
        3 -- environment error (not in a git repo)
    """
    parsed = _parse_cli_args(argv)
    if parsed is None:
        print(
            f"usage: {_PROG_NAME} <prepared-commit-msg-file> [-- <pathspec>...]",
            file=sys.stderr,
        )
        return 2
    msg_file, pathspec = parsed

    try:
        with open(msg_file, "r", encoding="utf-8", errors="replace") as fh:
            msg_text = fh.read()
    except OSError:
        print(f"cannot read prepared commit message file: {msg_file}", file=sys.stderr)
        return 2

    cwd = os.getcwd()
    rev_parse = _git(["rev-parse", "--git-dir"], cwd=cwd)
    if not rev_parse.ok:
        print("not in a git repository (run from inside a repo)", file=sys.stderr)
        return 3

    # No pathspec at all (or `--` with zero trailing paths, per _parse_cli_args)
    # is whole-index mode -- must NOT trip deletion_block_gate()'s C2-only
    # skip-gate-when-empty shortcut (see that function's `whole_index` param
    # docstring for why an empty gate_paths means something different here
    # than it does for C2's caller).
    outcome = deletion_block_gate(
        msg_text, gate_paths=pathspec, cwd=cwd, whole_index=not pathspec
    )

    if not outcome.passed:
        diagnostic = "\n".join(outcome.diagnostics)
        print(
            f"workstream-complete Step 2.67 block validation failed:\n{diagnostic}",
            file=sys.stderr,
        )
        print(
            "\nFix the commit body to match `git diff --cached` reality, then re-run.",
            file=sys.stderr,
        )
        return 1
    return 0
