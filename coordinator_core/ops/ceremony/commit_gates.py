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

Spec backlink: docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md § C3 (AC10).
Provenance: ported from `example-doctrine-repo:coordinator/bin/check-workstream-complete-deletion-blocks.sh`
  and `example-doctrine-repo:coordinator/bin/dirty-tree-gate.sh` (both still present on disk at
  `/Users/example-operator/X/example-doctrine-repo/coordinator/bin/` at port time -- not yet deleted
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
  - Neither gate shells out to bash, node, or awk -- all parsing is native
    Python string/regex work; every git read routes through
    `git_native._git` (AC2/AC3), never a bare `subprocess.run`.
  - The em-dash split in Kept-block parsing uses a literal Python string
    (U+2014, three UTF-8 bytes 0xE2 0x80 0x94) -- Python has no BSD-awk vs
    gawk octal-vs-hex portability landmine to reproduce; noted here, not
    carried forward.
  - `tracked_at_head` (the Kept-claim existence check) is intentionally left
    UNSCOPED to `gate_paths` -- it is a HEAD snapshot, not staged state, and
    the bash original scopes only the staged reads.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Set, Union

from coordinator_core.claim_state import resolve_claim_state
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.ops.ceremony.git_native import (
    GitResult,
    _git,
    diff_cached_name_only,
    diff_cached_name_status,
    status_porcelain,
)

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

    name_status_result: GitResult = diff_cached_name_status(cwd, find_renames=True)
    all_staged_deletions = _parse_name_status_deletions(name_status_result.stdout)
    staged_deletions = (
        [p for p in all_staged_deletions if p in gate_scope]
        if gate_scope
        else all_staged_deletions
    )
    staged_deletions_set = set(staged_deletions)

    # Assertion-1 needs a WIDER set than Assertion-3 does (2026-08-06 fix --
    # see `_parse_name_status_rename_sources`'s own docstring for the
    # incident): a Deleted-claimed path whose old location git paired into a
    # rename (identical/near-identical content staged at a new path in the
    # SAME batch) has genuinely vacated that path in the staged tree, even
    # though it is not a bare `D` line. Scoped by `gate_scope` the same way
    # `staged_deletions` is, immediately above.
    all_rename_sources = _parse_name_status_rename_sources(name_status_result.stdout)
    rename_sources = (
        [p for p in all_rename_sources if p in gate_scope] if gate_scope else all_rename_sources
    )
    staged_or_renamed_away_set = staged_deletions_set | set(rename_sources)

    name_only_result: GitResult = diff_cached_name_only(cwd)
    all_staged = [p for p in name_only_result.stdout.splitlines() if p]
    staged_all = [p for p in all_staged if p in gate_scope] if gate_scope else all_staged
    staged_all_set = set(staged_all)

    ls_tree_result = _git(["ls-tree", "-r", "HEAD", "--name-only"], cwd=cwd)
    tracked_at_head: Set[str] = {p for p in ls_tree_result.stdout.splitlines() if p}

    diagnostics: List[str] = []

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

    Spec backlink: docs/plans/2026-08-07-claim-state-ledger-first-authoritative-read.md
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
    example-doctrine-repo's bash-on-windows-gotchas.md § 11) -- this omission is
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
    """
    root = Path(worktree_root)
    known_scope = _build_known_scope(root)
    scoped = gate_paths is not None
    gate_scope: Set[str] = set(gate_paths) if gate_paths else set()

    unattributable: List[str] = []

    status_result = status_porcelain(root)
    parsed_lines: List[tuple] = []
    phantom_candidates: List[str] = []
    for line in status_result.stdout.splitlines():
        if not line:
            continue

        xy = line[:2]
        path = _porcelain_path(line)
        x_char = xy[0] if xy else " "
        parsed_lines.append((x_char, path))

        # EOL phantom filter (tracked-unstaged only -- untracked files are
        # never phantoms since there is no index entry to compare against).
        if x_char == " ":
            phantom_candidates.append(path)

    # Batched EOL-phantom check (docs/plans/2026-08-07-n-plus-one-git-spawn-
    # class-and-amplification-gate.md § C1): ONE `git diff --name-only`
    # over every tracked-unstaged candidate, instead of one `git diff
    # --quiet` per porcelain line. `real_diff_paths` is exactly the subset
    # WITH a real diff; any candidate absent from it is a phantom -- see
    # `_diff_name_only_worktree()`'s own docstring for why this is the
    # correct inversion (not "pass all paths to one `git diff --quiet`",
    # which loses per-path resolution).
    real_diff_paths: Set[str] = set()
    if phantom_candidates:
        diff_result = _diff_name_only_worktree(root, phantom_candidates)
        real_diff_paths = {p for p in diff_result.stdout.splitlines() if p}

    for x_char, path in parsed_lines:
        # (a) Staged: X status char not ' '/'?' -> this session's own pending commit.
        if x_char not in (" ", "?"):
            continue

        if x_char == " " and path not in real_diff_paths:
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
