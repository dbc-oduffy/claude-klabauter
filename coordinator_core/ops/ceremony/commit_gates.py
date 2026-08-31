"""
coordinator_core.ops.ceremony.commit_gates -- native ports of the deleted
`check-workstream-complete-deletion-blocks.sh` (164 LOC) and `dirty-tree-gate.sh`
(187 LOC), the C3 chunk of the `wsc_tail` rebuild
(docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md).

NO IN-COMMIT CALLER, AND THAT IS RECORDED, NOT AN OVERSIGHT. As of 2026-08-29
nothing on the commit path invokes these gates. `run_commit_pipeline` called
three of them (`deletion_block_gate`, `carry_gate`, `op_scope_coverage_gate`)
immediately before landing; it was killed at the 500ms brightline and C3
repointed every caller onto `commit_paths` / `ceremony.commit_v2`, which
implement none of them
(docs/plans/2026-08-29-the-push-subsystem-leaves-and-then-the-pipeline-can-go.md,
C4). The capability drop is filed as a P1 with the exposure enumerated and a
proposed action:
state/bug-backlog/2026-08-29-the-commit-v2-route-runs-none-of-the-fou-3e8811d511b7.yaml.
(The `dirty_tree_gate()` originally listed alongside these three had zero
production callers of its own -- unlike the other three, nothing on any
route ever invoked it -- and was deleted outright under the brightline kill
bar rather than carried as a fourth capability-drop entry; the P1's exposure
enumeration above predates that deletion and should be read as three gates,
not four.)

READ THAT BEFORE REINSTATING OR DELETING EITHER SIDE. The gates are not free,
and `commit_v2` is the zero-spawn replacement for an op killed on process
cost -- putting them back blind puts that cost on the sanctioned committer
every session and the dispatchable `git-commit-agent` route through. The P1
wants a spike measuring each gate in-process first. Equally, "no caller" is
not licence to delete this module: `deletion_block_gate` still has one live
entry point in `main()` below, reached by
`coordinator/bin/check-workstream-complete-deletion-blocks` (registered in
`authz/dispatchable.py`).

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

from coordinator_core.git.git_state import IndexParseError, head_blobs, read_index
from coordinator_core.ops.ceremony.git_native import (
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


def declared_deletion_gate(
    worktree_root: Union[str, Path],
    gate_paths: Sequence[str],
    declared_deletions: Sequence[str],
) -> GateOutcome:
    """Every IN-SCOPE staged deletion must be declared in `declared_deletions`.

    SIBLING to `deletion_block_gate`, not a replacement -- see this module's
    header and `docs/plans/2026-08-30-deletion-accountability-without-the-
    cere.md`. `deletion_block_gate`'s Assertion-3 requires a "Step 2.67"
    commit-body block, a `workstream-complete` ceremony convention the
    general committer (`ceremony.commit_v2`) cannot assume -- measured, it
    would refuse ~86% of this repo's commits. This gate replaces the prose
    oracle with a structural one: `ceremony.commit_v2` receives
    `params.deleted_paths`, an explicit declaration of what the commit
    removes, so accountability becomes "does staged reality match the
    declaration" with no commit-message parsing at all.

    Predicate: every path in `gate_paths` STAGED FOR DELETION must appear in
    `declared_deletions`. A declared path that is NOT staged for deletion is
    not this gate's business -- `commit_paths` already refuses `cannot read
    <path>` for that direction. An ordinary rename (source vacates one path,
    destination stages at another, same batch) is not staged-for-deletion
    reality here either -- reuses `_staged_deletions_and_renames_in_process`,
    which already separates a pure delete from a rename source via the exact
    (mode, sha) match test, so a plain rename never reaches this gate as an
    undeclared deletion.

    Scoped to `gate_paths` exactly, via `_staged_deletions_and_renames_in_
    process` -- never the whole index (see that function's own contract).
    A sibling session's own staged deletion outside `gate_paths` is not
    this caller's business and can never trip this gate.

    Skip-when-empty: an empty `gate_paths` has no bounded candidate set to
    check and is a legitimate no-deletions-in-scope call -- skipped, not
    scored as an ambiguous pass (mirrors `deletion_block_gate`'s own
    skip-gate-when-empty rule).

    Raises no exception: `_staged_deletions_and_renames_in_process`'s
    `IndexParseError` (an unmerged, mid-merge-conflict index) is caught and
    reported as a FAILING outcome naming the unmerged state -- a read this
    gate cannot answer is a refusal, never a silent pass (same posture as
    `dirty_tree_gate`'s F1 code-review finding, and `deletion_block_gate`'s
    own Kept-claim read above).

    CANDIDATE PRE-FILTER, so the index is read only when we are about to
    refuse (2026-08-30 plan amendment, same day as C1/C2 -- the first cut of
    this gate called `_staged_deletions_and_renames_in_process` unconditionally,
    which reads the WHOLE index internally (`read_index(cwd)` has no scoping
    parameter) and cost ~58ms on this repo, pushing `_pre_commit_gates` over
    `commit_v2`'s 50ms `PROCESS_TIME_TARGET_MS` -- measured, not guessed:
    `coordinator_core/benchmarks/tests/test_commit_v2_process_time_gate.py`
    went from green to a 60.5ms bracketed mean). The gate's actual job is to
    catch an UNDECLARED deletion, and an ordinary commit -- including every
    commit that declares its deletions correctly -- has no candidate for
    that at all: a `gate_paths` member that is either already declared, or
    still present on disk, cannot possibly be an undeclared deletion, and
    both are answerable with no index read whatsoever (`declared_deletions`
    is an in-memory set; `os.path.exists` is a stat, not a git read). Only a
    path that survives BOTH filters -- undeclared AND absent from the
    worktree -- might be a genuine undeclared staged deletion, and only then
    is `_staged_deletions_and_renames_in_process` called, scoped to that
    narrowed candidate set (still the same helper, same rename test, same
    unmerged-index posture -- nothing about C1's reuse mandate changes,
    only when it runs).

    Ordering is load-bearing: `declared_deletions` membership is checked
    BEFORE `os.path.exists`, so a correct N-path deletion commit that
    declares every one of them short-circuits per-path on the cheap set
    membership test and never reaches a single `stat` call, let alone the
    index read -- reversing the order would pay N stats to learn what the
    declaration already said.

    NAMED LIMIT, not an oversight: a path staged for deletion that STILL
    EXISTS on disk (mode/content changed at the git-index level relative to
    HEAD, but the file itself was never removed from the worktree -- an
    unusual git-level state, not merely "not on disk") will not be seen as
    an undeclared deletion by this fast path, because it fails the
    `os.path.exists` filter and is never handed to the real staged-deletion
    check. This is safe, not merely fast: `commit_paths` reads and commits
    exactly that file's worktree bytes for any `gate_paths` member in
    `paths`, so nothing is silently removed in that shape -- there is no
    deletion for the caller to have failed to declare.

    Zero `git` spawns on every reachable branch -- `_staged_deletions_and_
    renames_in_process` is itself spawn-free for a non-empty `gate_scope`,
    and the pre-filter above is a set membership test plus a stat, neither
    of which spawns.
    """
    gate_scope: Set[str] = set(gate_paths)
    if not gate_scope:
        return GateOutcome(passed=True, skipped=True, diagnostics=[])

    declared_set = set(declared_deletions)
    root = Path(worktree_root)
    candidates = {
        p for p in gate_scope
        if p not in declared_set and not (root / p).exists()
    }
    if not candidates:
        return GateOutcome(passed=True, skipped=False, diagnostics=[])

    try:
        staged_deletions_set, _rename_sources_set = _staged_deletions_and_renames_in_process(
            worktree_root, candidates
        )
    except IndexParseError as exc:
        return GateOutcome(
            passed=False,
            skipped=False,
            diagnostics=[
                f"declared_deletion_gate: staged index unreadable ({exc}) -- "
                "degraded read, gate refuses rather than guessing"
            ],
        )

    undeclared = sorted(staged_deletions_set - declared_set)
    if not undeclared:
        return GateOutcome(passed=True, skipped=False, diagnostics=[])

    shown = undeclared[:5]
    diagnostics = [f"Staged deletion not declared in deleted_paths: {p}" for p in shown]
    remainder = len(undeclared) - len(shown)
    if remainder > 0:
        diagnostics.append(f"...and {remainder} more -- pass them in deleted_paths")

    return GateOutcome(passed=False, skipped=False, diagnostics=diagnostics)


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
    literal dict AST instead, exactly as `write_surface_discovery._declares_write_surface`
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
        # `write_surface_discovery._declares_write_surface`'s own
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
