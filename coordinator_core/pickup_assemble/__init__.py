"""
coordinator_core.pickup_assemble — the `pickup-assemble` computed-skill engine.

Purpose: computes `pickup/SKILL.md`'s MECHANICAL branch inventory (classification,
preflight evidence, gate facts) into one read-only decision object per the frozen
contract, and surfaces every JUDGMENT branch as an overridable `judgment_points`
offer rather than deciding it. The EM's job collapses to resolving the judgment
residue this module surfaces — see the contract for the full rationale.

Contract (frozen, reviewed): DoE-claude coordinator/docs/wiki/computed-skills.md
Branches computed against: DoE-claude coordinator/skills/pickup/SKILL.md
Spec backlink: docs/plans/2026-07-23-computed-skills-pickup-beachhead.md, chunk A2
Registration seam: this module ships no bash veneer and needs none — it is
consumed directly by the `coordinator/bin/pickup-assemble` trampoline (mirrors
`archive-stamp-cli`'s direct-import template-variant #1, per
docs/plans/2026-07-23-dr-088-ladder-enforcement-layers.md's ladder-registration
discipline: a new engine capability registers by shipping a thin bin/ trampoline
over an in-process coordinator_core module, never a parallel classifier).

READ-ONLY, by construction (AC2b/AC3): every function in this module only reads
disk/git state. Mutating actions are returned as `directives[]` entries naming an
existing atomic CLI (archive-stamp-cli, session-claim-cli, ...) — this module
never shells out to a mutating verb, never writes a file, and never runs
`git fetch` (a fetch is disk-state-mutating on the local ref cache and would
break AC3's "mutates nothing" guarantee) — the EM/consumer performs the fetch as
part of the pre-dispatch revalidation the `revalidate_at_dispatch` judgment-point
flag already demands (see § round-trip classification in the contract).

Consumes manifest (the Director of Engineering F3, AC16) — orchestrates, reimplements none:
    coordinator_core.session.liveness      -> gates.liveness_signal, gates.claim,
        gates.competing_claim (AC3, the Staff Engineer #3), gates.claim_grant's holder check
    coordinator_core.ops.handoff_gate_aging -> gates.aging_verdict
    coordinator_core.ops.extract_scope_paths -> fm["scope"] (key="scope"), consumed
        by preflight.tree_quiescence, and preflight.completeness_items[] source
        parse (key="completeness_checklist")
    coordinator_core.ops.parse_completeness_item -> preflight.completeness_items[]
    coordinator_core.frontmatter.primitives -> frontmatter parse/read
    coordinator_core.machine_resolver.registry_get -> sibling-repo resolution for
        preflight.tree_quiescence (`repos.<repo-id>`, never a hardcoded path)
    coordinator_core.ops.fleet._memo_resolver.resolve_receiver_inbox -> the SAME
        machine-local receiver-EM-id -> repo-root resolution `compute_addressee_gate`
        and `compute_tree_quiescence` already depend on, reused (not reimplemented)
        by `compute_reply_closure`'s sender-repo lookup (see § reply-closure below)
    coordinator_core.ops.fleet._memo_resolver.resolve_self_em_id -> THE ONE
        self-identity resolver (`compute_addressee_gate`'s `self:` display
        line and `compute_reply_closure`'s sender-id derivation both call it;
        no second `basename + '-em'` copy exists in this module)
    coordinator_core.ops.fleet.memo_check_addressee.compute_check_addressee_candidate
        + .format_addressee_message -> gates.addressee, called IN-PROCESS
        (2026-07-26 subprocess-elision spinoff — `coordinator/bin/cross-repo-memo.py
        --check-addressee` was a pure round-trip back into an already-imported
        `coordinator_core`; no subprocess is spawned here anymore)
    archive-stamp-cli / session-claim-cli / coordinator-queue-append /
        coordinator-tasks-mirror / refresh-roadmap-callout -> named directives[],
        never invoked in-process (they mutate; this module does not)
    coordinator_core.reconcile.commit_reality.evaluate_commit_reality -> gates.
        commit_reality (C2, DR-300 route (d)) — the single-baton HEAD-consistency
        verdict for the artifact under pickup, called exactly as
        `handoff_reconcile._handler` constructs it (including
        `_chain_ancestor_norm_paths`/`_norm_path` ancestor exclusion from the
        cross-handoff attribution guard's `other_open_handoffs`); never
        `handoff.reconcile_open` itself (see Negative-spec below)

Negative-spec:
    - Do NOT add a mutating code path here. A finding that "the assembler should
      just do X" for any X that writes to disk belongs in `directives[]`, not in
      a new function body in this module.
    - Do NOT re-implement `handoff.reconcile_open`'s candidate-commit scan, the
      session-liveness two-layer model, or the aging predicate's date arithmetic
      here — import and call the existing modules (AC16). `compute_closure_signals`'
      noun-overlap `git log` scan is NOT that scan re-implemented: it operates at
      per-pending-bullet text granularity for EM-judgment evidence (contract §
      "Does this commit close this pending item?"), never at handoff-scope-pathspec
      granularity for auto-ship transition decisions — a different question, a
      different algorithm, and (unlike `handoff.reconcile_open`) it mutates nothing.
    - Do NOT return a directive that runs a `completeness_checklist` probe command
      (`build_completeness_checklist`) — the probe-run confirmation is a
      `judgment_points` entry whose dispositions resolve nothing, by construction
      (contract § "Probe-confirmation is JUDGMENT, not a gates boolean").
"""
from __future__ import annotations

import hashlib
import heapq
import json
import logging
import os
import re
import struct
import subprocess
import sys
import zlib
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple, Optional, Sequence, Union

from coordinator_core.artifact_basename import md_fallback_candidates
from coordinator_core.ceremony_common.json_payload_flag import (
    detect_conflicting_payload_channels,
    resolve_json_payload_flag,
)
from coordinator_core.shipped_in_tokens import (
    _NO_COMMIT_TOKEN_RE as _SHIPPED_NO_COMMIT_RE,
    _SHA_HEX_RE as _SHIPPED_SHA_RE,
)
from coordinator_core.claim_state import handoff_claim_dir, resolve_claim_state
from coordinator_core import dag
from coordinator_core.git.git_objects import (
    _GitReadModelError,
    _MAX_DELTA_DEPTH,
    _PackIndex,
    _apply_git_delta,
    _iter_pack_files,
    _parse_pack_index,
    _read_loose_object,
    _read_object,
    _read_pack_object_at,
    _read_pack_object_by_sha,
)
from coordinator_core.contract.decision_object.judgment import (
    build_judgment_point as _shared_build_judgment_point,
    build_untrusted_gate_judgment_point as _shared_build_untrusted_gate_judgment_point,
)
from coordinator_core.frontmatter.baton_class import kind_values_for_canonical
from coordinator_core.frontmatter.primitives import (
    canonical_body_sha as _shared_canonical_body_sha,
    frontmatter_body_text as _shared_frontmatter_body_text,
    git_blob_sha1 as _shared_git_blob_sha1,
    read_fm_field_unquoted,
    split_frontmatter,
)
from coordinator_core import lifecycle
from coordinator_core.machine_resolver import registry_get
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.fleet._memo_resolver import (
    AmbiguousReceiverError,
    RegistryReadError,
    resolve_receiver_inbox,
    resolve_self_em_id as _resolve_self_em_id,
)
from coordinator_core.ops.fleet.memo_check_addressee import (
    compute_check_addressee_candidate as _compute_check_addressee_candidate,
    format_addressee_message as _format_addressee_message,
)
from coordinator_core.ops.handoff_gate_aging import check_one as _gate_aging_check_one
from coordinator_core.session.holder_evidence import (
    holder_evidence as _holder_evidence,
)
from coordinator_core.ops.parse_completeness_item import (
    _Malformed as _CompletenessMalformed,
    parse_completeness_item as _parse_completeness_item,
)
from coordinator_core.session_baton.store import merge_baton, read_baton
from coordinator_core.session import claims as _claims
from coordinator_core.session import core as _session_core
from coordinator_core.session import harness_registry as _harness_registry
from coordinator_core.session import liveness as _liveness
from coordinator_core.session import worktree_safety as _worktree_safety
from coordinator_core.session.work_state import (
    _parse_fm_dict,
    _resolve_ledger_first_holder,
    _resolve_send_message_addresses,
    _scan_handoff_dir,
)
from coordinator_core.sizing_disposition import (
    compute_sizing_disposition,
    unsized_next_move_prefix,
)
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.wire_paths import rel_id

# ---------------------------------------------------------------------------
# Exit-code contract (locally scoped to this CLI — see contract § Exit-code
# contract; NOT inherited from any house convention).
# ---------------------------------------------------------------------------
EXIT_OK = 0
EXIT_BUSINESS_FAIL = 1
EXIT_USAGE = 2
EXIT_TRANSPORT_FAIL = 3

# The three known archive dirs a swept baton may have landed in (contract
# § archive-fallback, mirrors pickup/SKILL.md's own enumeration verbatim).
ARCHIVE_DIRS = ("cross-repo/archive", "archive/handoffs", "archive/completed")

# The three known LIVE dirs a baton may still be sitting in, un-actioned —
# each paired with its `ARCHIVE_DIRS` sweep destination above (same index
# order): `cross-repo/inbox` sweeps to `cross-repo/archive`, `state/handoffs`
# sweeps to `archive/handoffs`, and `docs/plans` sweeps to `archive/completed`
# (`fleet.archive_completed_plans` — `archive/completed` is dated-completed
# PLANS, not a generic memo/handoff dump; verified against this repo's own
# `archive/completed/2026-07/` contents, which are all `docs/plans/`-shaped
# filenames). 2026-07-25 defect: `_archive_fallback_search` only ever looked
# at where a baton is swept TO, never at where it actually LIVES — a bare
# basename for an open, un-actioned `cross-repo/inbox/` memo reported as
# unresolvable, sending the operator hunting archives for a file sitting in
# plain sight. Live hits and archive hits are searched and reported
# separately (never merged into one bucket) — see `resolve_artifact`.
LIVE_DIRS = ("cross-repo/inbox", "state/handoffs", "docs/plans")

# Minimum length (after stripping a trailing `.md`) a passed slug must carry
# before the suffix-match fallback tier (2026-07-28, PM-ruled) will attempt
# it — a caller passing a unique tail of a memo/handoff basename with the
# `<date>-<sender>-` prefix omitted should still resolve, but a 2-3 char
# string would sweep every file in `LIVE_DIRS`/`ARCHIVE_DIRS` and match
# arbitrarily. 8 is the shortest length that stays defensible on this repo's
# OWN naming convention: an abbreviated git commit hash (`b6dc46d6`,
# `5a64811b`, `d84d0abe` — see this repo's own recent commits) is exactly 8
# hex chars and is exactly the kind of bare unique tail a caller would
# reasonably paste in isolation. Shorter than that has no natural anchor and
# is far more likely to be an accidental substring than an intentional slug.
_MIN_SUFFIX_SLUG_LEN = 8

_TERMINAL_HANDOFF_FIELDS = ("status", "deployment_state", "shipped_in")
_TERMINAL_MEMO_FIELDS = (
    "status",
    "decision",
    "decision_note",
    "actioned_note",
    "realized_by",
    "picked_up_by",
    # `kind`/`from`/`created` (2026-07-25 reply-closure defect fix, see
    # `compute_reply_closure`): the archived-fallback branch of `brief()`
    # only ever sees `resolution.terminal_fields` (`_build_archived_
    # resolution` deliberately hands it an empty `frontmatter: {}`) — without
    # these three fields here, an archived-but-actioned inbound consult/ask
    # memo is invisible to the reply-closure check, exactly reproducing the
    # defect this fix closes. `"from" in terminal_fields` also doubles as
    # this branch's memo-vs-handoff discriminator in `brief()`, since
    # `_TERMINAL_HANDOFF_FIELDS` never contributes that key.
    "kind",
    "from",
    "created",
)


# ---------------------------------------------------------------------------
# Small git/filesystem helpers — no shell, no bash, subprocess-argv only.
# ---------------------------------------------------------------------------

_NO_CONSOLE = no_console_creationflags()

# Verbs `_run_git` must run as a real `git` spawn rather than through the
# in-process read-model: `status`/`diff` (read-only, but their working-tree /
# index semantics are not modelled) and `add`/`commit` (MUTATIONS — the
# read-model reads committed history and structurally cannot stage or write).
_RUN_GIT_SPAWN_VERBS = ("status", "diff", "add", "commit")


# ---------------------------------------------------------------------------
# In-process git read-model (W0-2, plan `2026-07-24-canonical-resolution-
# engine.md` AC-6/AC-8) — cached `pathlib` `.git/` reads that replace the
# subprocess spawns `_run_git` used to make (Windows P0: 12 CreateProcess
# calls = 360-950ms on the read-only `brief` path). Pure stdlib (`zlib`,
# `struct`, `hashlib`) — NOT pygit2 (native-dep + import-cost regression,
# forbidden by AC-8). Understands loose objects, pack files (v2 `.idx` +
# OFS_DELTA/REF_DELTA chains), loose + `packed-refs`, and linked worktrees
# (a `.git` FILE containing `gitdir: <path>` + that gitdir's own
# `commondir` pointer back to the shared refs/objects store).
#
# `git status --porcelain` is the one sanctioned residual spawn (plan body:
# "status --porcelain may keep at most one spawn if unavoidable — flag
# it") — reproducing its stat/hash/`.gitignore` working-tree-vs-index
# semantics in-process is a materially different (and much larger) project
# than reading committed history, and the plan explicitly carves it out.
# `git diff` is a second sanctioned residual spawn (Review: code-reviewer —
# Finding 1): `_classify_stamp_delta`'s `stale-bookkeeping`/`stale-
# substantive` verdict is PM-gating, and `difflib.unified_diff`'s
# SequenceMatcher alignment is not guaranteed to partition the same two
# blobs into the same `+`/`-` lines as git's own Myers diff — a divergence
# there could misclassify a substantive change as bookkeeping and bypass
# the PM gate. It stays a real spawn to keep that comparison byte-for-byte
# git-equivalent. It is off the hot `brief()` path: `compute_execution_
# stamp_match` returns `None` before ever reaching `_classify_stamp_delta`
# on any artifact without an `execution_authorized_sha`/`Plan to Execute`
# pointer, which is the common case — so AC-6's hot-path-zero-spawn holds.
# Every other git call in this module funnels through `_run_git`, which
# now dispatches on argv[0] to the read-model instead of spawning; callers
# are unchanged (Finding 4a's uniform-`CompletedProcess`-on-failure
# contract from the old subprocess-only body is preserved verbatim: a
# read-model miss degrades to `returncode=1, stdout=""`, never a raised
# exception, never a spawn fallback).
#
# Negative-spec: does NOT replicate git's `--follow` rename-tracking for
# the `-S<needle>` pickaxe search (`_in_process_pickaxe`) — a renamed-then-
# edited file's pre-rename history is invisible to the in-process walk.
# Does NOT replicate git's merge-history simplification for path-scoped
# `log` (`_commit_touches_path` treats a commit as touching `path` if the
# blob differs from ANY parent, not git's full simplify-merges algorithm;
# `_in_process_pickaxe` has the same gap for its own parent loop — it
# returns a merge commit the moment ANY parent's needle count differs, even
# when the merge is TREESAME to another parent and real git would walk
# past it).
#
# CORRECTED (stamp-integrity investigation, `tasks/mise-findings/stamp-
# integrity.md`, DoE-claude, 2026-07-30): an earlier revision of this note
# characterized the pickaxe gap as narrow — rename-only. That was false.
# `_in_process_pickaxe` was directly reproduced disagreeing with real git
# on a **never-renamed** path too (a TREESAME-to-first-parent merge commit,
# see the test named above), and `_walk_commits`'s own docstring already
# calls its date-order walk "an approximation, not a guarantee." Because a
# wrong answer here fed a PM-gating verdict (`compute_execution_stamp_
# match` via `_find_stamp_commit`), that call site no longer routes through
# this read-model at all — `_find_stamp_commit` spawns real `git` directly
# instead (see its own docstring). `_in_process_pickaxe`/`_dispatch_log`'s
# `-1 --follow -S...` arm remain in this module only as the implementation
# `test_pickup_assemble_git_readmodel_parity.py` pins the divergence
# against; no production call site still depends on their answer being
# correct. See the W0-2 executor report for the original plan citation.
# ---------------------------------------------------------------------------


# `_GitReadModelError` is imported from `coordinator_core.git.git_objects`
# (see top-of-file imports) -- one exception type for both the extracted
# object-store reader and every other read-model dispatch site in this
# module that raises/catches it.


class _GitDirs(NamedTuple):
    """`git_dir` is worktree-private (HEAD, index); `common_dir` is where
    refs/objects actually live — identical to `git_dir` for a normal
    repo, but the *main* worktree's `.git` dir for a linked worktree."""

    git_dir: Path
    common_dir: Path


_GIT_DIRS_CACHE: dict[str, Optional[tuple[Path, _GitDirs]]] = {}
_PACKED_REFS_CACHE: dict[str, dict[str, str]] = {}
# Keyed on the commit sha itself — see `_commit_meta`, the single
# read-and-parse entry point for commit objects in this module.
_COMMIT_META_CACHE: dict[str, dict[str, Optional[dict[str, Any]]]] = {}
# Nested per str(common_dir), then keyed on (tree_sha, path_component) ->
# the matching entry's sha, or None — see `_blob_sha_at_tree_path`, the
# single tree-descent function in this module.
_TREE_PATH_STEP_CACHE: dict[str, dict[tuple[str, str], Optional[str]]] = {}

_HEX40_RE = re.compile(r"[0-9a-fA-F]{40}")
_HEX_ABBREV_RE = re.compile(r"[0-9a-fA-F]{4,39}")
_GITDIR_POINTER_RE = re.compile(r"^gitdir:\s*(.+)$")

#: `resolve_artifact`'s revision tier (2026-08-14) candidate shape — a
#: caller citing a delivery-commit SHA rather than its artifact path. Full
#: hex range git itself accepts for an abbreviated sha (7 is git's default
#: abbrev floor; 40 is a full sha) — never mistaken for a real path/basename
#: because a `.md` basename can't be pure-hex-and-nothing-else at this
#: length in this corpus's `<date>-<sender>-<slug>.md` convention.
_REVISION_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _resolve_common_dir(git_dir: Path) -> Path:
    """A linked worktree's private `git_dir` carries a `commondir` file
    pointing back at the main worktree's `.git` — refs and objects live
    there, not under the linked worktree's own private dir."""
    commondir_file = git_dir / "commondir"
    if commondir_file.is_file():
        try:
            rel = commondir_file.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return git_dir
        candidate = Path(rel)
        return candidate if candidate.is_absolute() else (git_dir / candidate).resolve()
    return git_dir


def _discover_git_dirs(start: Path) -> Optional[tuple[Path, _GitDirs]]:
    """Walk upward from `start` looking for a `.git` dir (normal repo) or
    `.git` file (`gitdir: <path>`, linked worktree) — mirrors git's own
    repository-discovery walk. Returns `(worktree_root, _GitDirs)` or None
    when `start` is not inside a git worktree. Cached per resolved `start`
    path (the discovery walk touches the filesystem at every level)."""
    key = str(start)
    if key in _GIT_DIRS_CACHE:
        return _GIT_DIRS_CACHE[key]
    cur = start.resolve()
    result: Optional[tuple[Path, _GitDirs]] = None
    while True:
        dot_git = cur / ".git"
        if dot_git.is_dir():
            result = (cur, _GitDirs(dot_git, _resolve_common_dir(dot_git)))
            break
        if dot_git.is_file():
            try:
                text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                break
            match = _GITDIR_POINTER_RE.match(text)
            if not match:
                break
            gitdir_path = Path(match.group(1).strip())
            if not gitdir_path.is_absolute():
                gitdir_path = (cur / gitdir_path).resolve()
            result = (cur, _GitDirs(gitdir_path, _resolve_common_dir(gitdir_path)))
            break
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    _GIT_DIRS_CACHE[key] = result
    return result


def _pack_index_prefix_matches(pidx: _PackIndex, prefix_hex: str) -> list[str]:
    """Linear scan for abbreviated-sha resolution (`cat-file -e` on a short
    sha). An abbreviated sha may be shorter than 2 hex chars, in which case
    the fanout table (first-byte-only) cannot bound the scan at all — this
    always does an unconditional full scan regardless of prefix length,
    rather than fanout-bounding the ≥2-char case (Review: code-reviewer —
    Finding 8: reworded to not imply an optimization the code doesn't do);
    correct for the object counts this module deals with, just not the
    fastest possible."""
    prefix = prefix_hex.lower()
    n = len(pidx.offsets)
    matches = []
    for i in range(n):
        sha_hex = pidx.shas[i * 20 : i * 20 + 20].hex()
        if sha_hex.startswith(prefix):
            matches.append(sha_hex)
    return matches


def _find_object_by_prefix(common_dir: Path, prefix_hex: str) -> Optional[str]:
    """Abbreviated-sha resolution (`cat-file -e <short-sha>`) — scans
    loose object directories under the matching first-byte prefix, then
    every pack's sha table. Returns the sha only when the prefix is
    unambiguous (matches exactly one object), mirroring git's own
    ambiguous-abbrev failure mode."""
    prefix = prefix_hex.lower()
    matches: set[str] = set()
    objects_dir = common_dir / "objects"
    if len(prefix) >= 2:
        subdir = objects_dir / prefix[:2]
        if subdir.is_dir():
            for entry in subdir.iterdir():
                candidate = prefix[:2] + entry.name
                if candidate.startswith(prefix):
                    matches.add(candidate)
    elif objects_dir.is_dir():
        for subdir in objects_dir.iterdir():
            if subdir.is_dir() and len(subdir.name) == 2 and subdir.name.startswith(prefix):
                for entry in subdir.iterdir():
                    candidate = subdir.name + entry.name
                    if candidate.startswith(prefix):
                        matches.add(candidate)
    for idx_path, _ in _iter_pack_files(common_dir):
        pidx = _parse_pack_index(idx_path)
        if pidx is None:
            continue
        matches.update(_pack_index_prefix_matches(pidx, prefix))
    if len(matches) == 1:
        return next(iter(matches))
    return None


def _read_packed_refs(common_dir: Path) -> dict[str, str]:
    key = str(common_dir)
    if key in _PACKED_REFS_CACHE:
        return _PACKED_REFS_CACHE[key]
    result: dict[str, str] = {}
    path = common_dir / "packed-refs"
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for line in text.splitlines():
            if not line or line.startswith("#") or line.startswith("^"):
                continue
            sha, sep, name = line.partition(" ")
            if sep and _HEX40_RE.fullmatch(sha):
                result[name] = sha.lower()
    _PACKED_REFS_CACHE[key] = result
    return result


def _read_ref_chain(common_dir: Path, ref: str) -> Optional[str]:
    """Resolves a ref path relative to `common_dir` — a loose ref file
    (following `ref:` indirection through an arbitrary number of hops
    until a cycle is detected, via the `seen` set below) or a
    `packed-refs` entry. Review: code-reviewer — Finding 6: docstring
    previously claimed a single-hop-only convention the code does not
    implement."""
    seen: set[str] = set()
    current = ref
    while current not in seen:
        seen.add(current)
        path = common_dir / current
        if path.is_file():
            try:
                content = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                return None
            if content.startswith("ref:"):
                current = content[4:].strip()
                continue
            if _HEX40_RE.fullmatch(content):
                return content.lower()
            return None
        packed = _read_packed_refs(common_dir)
        if current in packed:
            return packed[current]
        return None
    return None


def _read_head_sha(dirs: _GitDirs) -> Optional[str]:
    head_path = dirs.git_dir / "HEAD"
    if not head_path.is_file():
        return None
    try:
        content = head_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if content.startswith("ref:"):
        return _read_ref_chain(dirs.common_dir, content[4:].strip())
    if _HEX40_RE.fullmatch(content):
        return content.lower()
    return None


def _current_branch_name(dirs: _GitDirs) -> Optional[str]:
    """`git rev-parse --abbrev-ref HEAD` equivalent: the branch's short
    name when HEAD is symbolic, the literal string `"HEAD"` when detached
    (matching real git's behavior — never a bare sha), or None when HEAD
    itself can't be read (not a git worktree)."""
    head_path = dirs.git_dir / "HEAD"
    if not head_path.is_file():
        return None
    try:
        content = head_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if content.startswith("ref:"):
        ref = content[4:].strip()
        return ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
    if _HEX40_RE.fullmatch(content):
        return "HEAD"
    return None


def _peel_annotated_tag(common_dir: Path, sha: str) -> str:
    """Peels an annotated tag object to the commit (or other object) it
    names, recursively — a tag can point at another tag. Real `git rev-
    parse`/`log` resolve a tag revision through to its target implicitly;
    without this, `_walk_commits` silently drops a tag-pointing revision
    (its `push()` only accepts `type == "commit"`), treating it as if it
    had zero history. Returns `sha` unchanged for a non-tag object or on a
    cycle (`seen` guard, mirroring `_read_ref_chain`'s convention)."""
    seen: set[str] = set()
    current = sha
    while current not in seen:
        seen.add(current)
        obj = _read_object(common_dir, current)
        if obj is None or obj[0] != "tag":
            return current
        target = None
        for line in obj[1].decode("utf-8", errors="replace").splitlines():
            if line.startswith("object "):
                target = line[len("object ") :].strip()
                break
        if target is None:
            return current
        current = target
    return current


def _resolve_revision(dirs: _GitDirs, value: str) -> Optional[str]:
    """General revision resolver: `HEAD`, a full 40-hex sha, an
    unambiguous abbreviated sha, or a branch/tag/remote-tracking short
    name (loose ref file or `packed-refs` entry). Peels an annotated tag
    to its target before returning (Review: code-reviewer — Finding 5)."""
    sha = _resolve_revision_raw(dirs, value)
    if sha is None:
        return None
    return _peel_annotated_tag(dirs.common_dir, sha)


def _resolve_revision_raw(dirs: _GitDirs, value: str) -> Optional[str]:
    value = value.strip()
    if value == "HEAD":
        return _read_head_sha(dirs)
    if _HEX40_RE.fullmatch(value):
        # A full 40-hex string is trusted only after confirming the object
        # exists — mirroring the abbreviated-sha path just below, which
        # already returns `None` on no match via `_find_object_by_prefix`.
        # Without this check, a 40-hex value that names no object in the
        # store (e.g. a caller-fabricated or copy-pasted-wrong sha) would
        # short-circuit here and be reported "resolved" by every downstream
        # consumer, which then fails on the *next* lookup with a misleading
        # "resolved but delivered no artifact" message instead of the
        # correct "does not resolve as a commit" diagnosis. One
        # content-addressed lookup via `_read_object` — the same cost the
        # abbreviated path already pays for its prefix scan, and free on
        # repeat within a `brief()` process thanks to `_OBJECT_CACHE`. No
        # type filter: a full sha naming a tree/blob/tag is still a real
        # object in the store, so it is trusted here exactly as it always
        # was — only a sha naming nothing at all now falls through.
        if _read_object(dirs.common_dir, value.lower()) is not None:
            return value.lower()
    if _HEX_ABBREV_RE.fullmatch(value):
        found = _find_object_by_prefix(dirs.common_dir, value.lower())
        if found:
            return found
    for candidate in (f"refs/heads/{value}", f"refs/tags/{value}", f"refs/remotes/{value}", value):
        sha = _read_ref_chain(dirs.common_dir, candidate)
        if sha:
            return sha
    packed = _read_packed_refs(dirs.common_dir)
    for candidate in (f"refs/heads/{value}", f"refs/tags/{value}", f"refs/remotes/{value}", value):
        if candidate in packed:
            return packed[candidate]
    return None


def _parse_commit(content: bytes) -> dict[str, Any]:
    text = content.decode("utf-8", errors="replace")
    parents: list[str] = []
    tree: Optional[str] = None
    committer_epoch: Optional[int] = None
    lines = text.split("\n")
    for line in lines:
        if not line:
            break
        if line.startswith("tree "):
            tree = line[5:].strip()
        elif line.startswith("parent "):
            parents.append(line[7:].strip())
        elif line.startswith("committer "):
            tokens = line.split(" ")
            try:
                committer_epoch = int(tokens[-2])
            except (IndexError, ValueError):
                committer_epoch = None
    subject_lines = text.split("\n\n", 1)
    subject = subject_lines[1].split("\n", 1)[0] if len(subject_lines) == 2 else ""
    return {"tree": tree, "parents": parents, "committer_epoch": committer_epoch, "subject": subject}


def _commit_meta(common_dir: Path, sha: str) -> Optional[dict[str, Any]]:
    """The one entry point in this module that reads-and-parses a commit
    object. Every caller that needs a commit's tree/parents/committer_epoch/
    subject — `_walk_commits`, `_commit_touches_path`, `_resolve_path_in_commit`,
    `_is_ancestor_or_self`, and `_dispatch_log`'s `-1 --format=%ct` arm —
    routes through here rather than calling `_read_object` + `_parse_commit`
    directly, so a given commit is read and parsed at most once per
    `brief()` process. Cache key is the commit sha itself — content-addressed,
    so (like `_OBJECT_CACHE`) the memo is sound for the process's entire
    lifetime; it never keys on a mutable input (a path, a ref, a pid), which
    is what distinguishes it from the call-scoped `session_live` pid dedupe
    (AC8 negative-spec) — a memo keyed on a sha cannot go stale within a
    single invocation."""
    cache = _COMMIT_META_CACHE.setdefault(str(common_dir), {})
    if sha in cache:
        return cache[sha]
    obj = _read_object(common_dir, sha)
    result = _parse_commit(obj[1]) if (obj is not None and obj[0] == "commit") else None
    cache[sha] = result
    return result


def _parse_tree(content: bytes) -> list[tuple[str, str, str]]:
    entries: list[tuple[str, str, str]] = []
    i = 0
    n = len(content)
    while i < n:
        space = content.index(b" ", i)
        mode = content[i:space].decode("ascii", errors="replace")
        nul = content.index(b"\x00", space + 1)
        name = content[space + 1 : nul].decode("utf-8", errors="replace")
        sha = content[nul + 1 : nul + 21].hex()
        entries.append((mode, name, sha))
        i = nul + 21
    return entries


def _scan_tree_entry(content: bytes, name: str) -> Optional[str]:
    """Scans a tree object's raw bytes for the entry named `name`, returning
    on the first match instead of decoding every entry's name the way
    `_parse_tree` does (which stays, unchanged, for its other callers).
    Used only by `_blob_sha_at_tree_path`, which never needs more than one
    name per tree."""
    name_bytes = name.encode("utf-8", errors="replace")
    name_len = len(name_bytes)
    i = 0
    n = len(content)
    while i < n:
        space = content.index(b" ", i)
        nul = content.index(b"\x00", space + 1)
        if nul - space - 1 == name_len and content[space + 1 : nul] == name_bytes:
            return content[nul + 1 : nul + 21].hex()
        i = nul + 21
    return None


def _blob_sha_at_tree_path(common_dir: Path, tree_sha: Optional[str], path: str) -> Optional[str]:
    """The one tree-descent function in this module — every path-at-tree
    lookup (`_commit_touches_path`, `_resolve_path_in_commit`) routes
    through here rather than walking a tree independently. Each descent
    step is memoized on `(common_dir, tree_sha, path_component)`: a tree
    sha is content-addressed, so the same subtree reached from two
    different commits (the common case on a linear history) is scanned via
    `_scan_tree_entry` at most once per process, never via a full
    `_parse_tree` decode of every entry."""
    if not tree_sha:
        return None
    parts = [p for p in path.split("/") if p]
    if not parts:
        return None
    current = tree_sha
    cache = _TREE_PATH_STEP_CACHE.setdefault(str(common_dir), {})
    for i, part in enumerate(parts):
        step_key = (current, part)
        if step_key in cache:
            child = cache[step_key]
        else:
            obj = _read_object(common_dir, current)
            child = _scan_tree_entry(obj[1], part) if (obj is not None and obj[0] == "tree") else None
            cache[step_key] = child
        if child is None:
            return None
        if i == len(parts) - 1:
            return child
        current = child
    return None


def _walk_tree_md_paths(common_dir: Path, tree_sha: Optional[str], prefix: str, out: dict[str, str]) -> None:
    """Recursively collects every `.md` blob under `tree_sha` into `out`
    (repo-relative path -> blob sha), descending subtrees (mode `40000`)
    the way `_blob_sha_at_tree_path` descends a single named path — this is
    the whole-subtree counterpart used by `_changed_md_paths_for_revision`,
    which needs every `.md` path under a `LIVE_DIRS`/`ARCHIVE_DIRS` root
    rather than one path it already knows the name of."""
    if not tree_sha:
        return
    obj = _read_object(common_dir, tree_sha)
    if obj is None or obj[0] != "tree":
        return
    for mode, name, sha in _parse_tree(obj[1]):
        rel = f"{prefix}/{name}" if prefix else name
        if mode == "40000":
            _walk_tree_md_paths(common_dir, sha, rel, out)
        elif name.endswith(".md"):
            out[rel] = sha


def _changed_md_paths_for_revision(common_dir: Path, sha: str) -> list[tuple[str, Optional[str]]]:
    """The `.md` paths `sha` changed under `LIVE_DIRS + ARCHIVE_DIRS`
    (repo-relative, commit-time paths — the revision tier in
    `resolve_artifact` re-derives current locations from these basenames
    rather than trusting them directly, since an actioned memo has since
    moved), paired with the path's blob sha *in this commit* (`None` when
    `sha` deleted the path). A path counts as changed if its blob at `sha`
    (possibly absent) differs from the same path's blob in every parent it
    has (mirrors `_commit_touches_path`'s per-path semantics, applied across
    a whole directory subtree instead of one caller-named path); a root
    commit counts every present path as changed, exactly as
    `_commit_touches_path` does for a root commit. Unlike a naive walk of
    just `sha`'s own tree, this also walks each parent's tree so a path
    present only in a parent (deleted by `sha`) is still enumerated and
    reported — otherwise a delete-only commit would be invisible here, at
    odds with `_commit_touches_path`'s presence/absence handling for the
    single-path case."""
    commit = _commit_meta(common_dir, sha)
    if commit is None:
        return []
    parents = commit["parents"]
    parent_commits = [c for c in (_commit_meta(common_dir, p) for p in parents) if c is not None]
    changed: list[tuple[str, Optional[str]]] = []
    for rel_dir in LIVE_DIRS + ARCHIVE_DIRS:
        dir_tree_sha = _blob_sha_at_tree_path(common_dir, commit["tree"], rel_dir)
        dir_md_paths: dict[str, str] = {}
        if dir_tree_sha is not None:
            _walk_tree_md_paths(common_dir, dir_tree_sha, rel_dir, dir_md_paths)
        parent_dir_paths: list[dict[str, str]] = []
        for parent_commit in parent_commits:
            parent_tree_sha = _blob_sha_at_tree_path(common_dir, parent_commit["tree"], rel_dir)
            parent_md_paths: dict[str, str] = {}
            if parent_tree_sha is not None:
                _walk_tree_md_paths(common_dir, parent_tree_sha, rel_dir, parent_md_paths)
            parent_dir_paths.append(parent_md_paths)
        if dir_tree_sha is None and not any(parent_dir_paths):
            continue
        all_paths = set(dir_md_paths)
        for parent_md_paths in parent_dir_paths:
            all_paths.update(parent_md_paths)
        for path in all_paths:
            blob_sha = dir_md_paths.get(path)
            if not parent_commits:
                if blob_sha is not None:
                    changed.append((path, blob_sha))
                continue
            if any(parent_md_paths.get(path) != blob_sha for parent_md_paths in parent_dir_paths):
                changed.append((path, blob_sha))
    return changed


def _resolve_path_in_commit(common_dir: Path, commit_sha: str, path: str) -> Optional[tuple[str, bytes]]:
    commit = _commit_meta(common_dir, commit_sha)
    if commit is None:
        return None
    blob_sha = _blob_sha_at_tree_path(common_dir, commit["tree"], path)
    if blob_sha is None:
        return None
    blob = _read_object(common_dir, blob_sha)
    return (blob_sha, blob[1]) if blob else None


def _walk_commits(common_dir: Path, start_sha: str):
    """Yields `(sha, parsed_commit)` from `start_sha` via a max-heap keyed
    on `-committer_epoch` — approximates `git log`'s default date-order
    topological walk closely enough for this module's evidence-only
    signals. This is an approximation, not a guarantee: a parent is only
    pushed once its child is popped, so a parent with a later committer
    timestamp than its already-popped child (clock skew, rebase, import)
    can become the new heap max and pop next, a local increase in the
    emitted sequence. `--since` filtering (`_walk_commits_since`) does not
    rely on strict non-increasing order — it tolerates local increases via
    a slop budget instead of a hard `break`. See also the same-caveat note
    on `_latest_commit_touching_path` and `_in_process_pickaxe` below,
    whose "most recent" semantics assume this order approximately holds."""
    seen: set[str] = set()
    heap: list[tuple[int, str, dict[str, Any]]] = []

    def push(sha: str) -> None:
        if sha in seen:
            return
        seen.add(sha)
        commit = _commit_meta(common_dir, sha)
        if commit is None:
            return
        heapq.heappush(heap, (-(commit["committer_epoch"] or 0), sha, commit))

    push(start_sha)
    while heap:
        _neg_ts, sha, commit = heapq.heappop(heap)
        yield sha, commit
        for parent in commit["parents"]:
            push(parent)


def _commit_touches_path(common_dir: Path, sha: str, commit: dict[str, Any], path: str) -> bool:
    """A commit "touches" `path` if the blob sha at that path differs from
    the corresponding blob sha in every parent (or the path's mere
    presence/absence differs) — approximates `git log -- path` without
    replicating git's full merge-history simplification (negative-spec,
    module docstring)."""
    current = _blob_sha_at_tree_path(common_dir, commit["tree"], path)
    parents = commit["parents"]
    if not parents:
        return current is not None
    for parent_sha in parents:
        parent_commit = _commit_meta(common_dir, parent_sha)
        if parent_commit is None:
            continue
        if parent_commit["tree"] == commit["tree"]:
            continue  # identical tree as this commit's -> path cannot differ, no descent needed
        parent_blob = _blob_sha_at_tree_path(common_dir, parent_commit["tree"], path)
        if parent_blob != current:
            return True
    return False


def _commit_deletes_path(common_dir: Path, sha: str, commit: dict[str, Any], path: str) -> bool:
    """True when `commit` deleted `path` — absent from `commit`'s own tree
    but present in at least one parent's tree. Call only once
    `_commit_touches_path` has already confirmed the commit touches `path`
    (a deletion is a specific case of "touches"), to avoid a redundant tree
    descent on commits that don't touch the path at all.

    A commit with no parents (initial commit) cannot delete anything — the
    path was never there to remove."""
    current = _blob_sha_at_tree_path(common_dir, commit["tree"], path)
    if current is not None:
        return False
    parents = commit["parents"]
    if not parents:
        return False
    for parent_sha in parents:
        parent_commit = _commit_meta(common_dir, parent_sha)
        if parent_commit is None:
            continue
        parent_blob = _blob_sha_at_tree_path(common_dir, parent_commit["tree"], path)
        if parent_blob is not None:
            return True
    return False


_SINCE_SLOP = 5  # git revision.c SLOP — keep walking past the date bound this
                 # many commits, because committer dates are not monotonic
                 # along parent edges (clock skew, rebase, import).


def _walk_commits_since(common_dir: Path, head_sha: str, since_epoch: Optional[int]):
    """Bounds `_walk_commits` to the `--since` window with a slop budget.

    Committer dates are not monotonic along parent edges (clock skew,
    rebase, import), so a plain `break` on the first out-of-window commit
    can silently truncate results: a skewed-newer ancestor may only be
    reachable by descending through an out-of-window commit first. An
    out-of-window commit is therefore never yielded, but its parents are
    still pushed onto the walk; each in-window commit resets the slop
    budget, and the walk gives up only once the budget is exhausted.
    `since_epoch=None` disables the bound and yields every commit.
    """
    slop = _SINCE_SLOP
    for sha, commit in _walk_commits(common_dir, head_sha):
        ts = commit["committer_epoch"] or 0
        if since_epoch is not None and ts < since_epoch:
            slop -= 1
            if slop == 0:
                break
            continue  # do NOT emit, but DO let the walk push this commit's
                      # parents -- a skewed newer ancestor is only reachable
                      # through it
        else:
            slop = _SINCE_SLOP  # an in-window commit resets the budget
        yield sha, commit


def _in_process_log_oneline(dirs: _GitDirs, argv: list[str]) -> list[tuple[str, str]]:
    since_epoch: Optional[int] = None
    path_filter: Optional[str] = None
    i = 0
    n = len(argv)
    while i < n:
        token = argv[i]
        if token == "--":
            if i + 1 < n:
                path_filter = argv[i + 1]
            i += 2
            continue
        if token.startswith("--since="):
            since_epoch = _parse_since_date(token[len("--since=") :])
            i += 1
            continue
        i += 1

    head_sha = _resolve_revision(dirs, "HEAD")
    if head_sha is None:
        return []
    results: list[tuple[str, str]] = []
    for sha, commit in _walk_commits_since(dirs.common_dir, head_sha, since_epoch):
        if path_filter is not None and not _commit_touches_path(dirs.common_dir, sha, commit, path_filter):
            continue
        results.append((sha, commit["subject"]))
    return results


def _parse_since_date(date_str: str) -> Optional[int]:
    """A date-only `--since` value (`YYYY-MM-DD`) is inherently UTC-shaped —
    there is no wall-clock/timezone component to localize. Never call naive
    `.timestamp()` on it: Python treats a naive `datetime` as LOCAL time, so
    every previous caller silently got its epoch offset by the host's UTC
    offset, and CPython on Windows additionally raises `OSError: [Errno 22]`
    converting a local-time date on or near 1970-01-01 (the exact fallback
    `_artifact_since_date` returns for a dateless basename) because the
    underlying CRT `mktime` rejects negative/near-zero results. Attaching
    `tzinfo=timezone.utc` before `.timestamp()` fixes both: no local-offset
    skew, and no epoch-adjacent CRT failure."""
    try:
        return int(
            datetime.strptime(date_str.strip(), "%Y-%m-%d")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except ValueError:
        return None


def _latest_commit_touching_path(dirs: _GitDirs, path: str) -> Optional[str]:
    # "Latest" == first touching commit found in `_walk_commits`'s pop
    # order, which is an approximation of non-increasing timestamp order,
    # not a guarantee (see `_walk_commits`'s docstring) — under clock skew
    # this can return a commit that isn't truly the most recent toucher.
    head_sha = _resolve_revision(dirs, "HEAD")
    if head_sha is None:
        return None
    for sha, commit in _walk_commits(dirs.common_dir, head_sha):
        if _commit_touches_path(dirs.common_dir, sha, commit, path):
            return sha
    return None


def _list_local_branches(common_dir: Path) -> list[tuple[str, str]]:
    result: dict[str, str] = {}
    heads_dir = common_dir / "refs" / "heads"
    if heads_dir.is_dir():
        for entry in heads_dir.rglob("*"):
            if entry.is_file():
                name = entry.relative_to(heads_dir).as_posix()
                try:
                    sha = entry.read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    continue
                if _HEX40_RE.fullmatch(sha):
                    result[name] = sha.lower()
    for name, sha in _read_packed_refs(common_dir).items():
        if name.startswith("refs/heads/"):
            result.setdefault(name[len("refs/heads/") :], sha)
    return sorted(result.items())


# Kinds that classify() labels `spinoff` (contract MECHANICAL checklist,
# Step 1.5 classification table). Note the label is the string "spinoff";
# renaming frontmatter values never renames that label.

# Membership is EXPLICIT, not derived from `baton_class()`, and that is a
# finding rather than a shortcut. This set's members do not share one
# `baton_class`: `spinoff` derives `deflection` while `spinoff-roadmap` /
# `roadmap-baton` derive `intention`. A `baton_class()`-based predicate here
# would both WIDEN the set (pulling in every other `deflection` kind) and
# NARROW it (dropping `roadmap-baton`, which is what the migrated live
# records actually carry) -- so it would silently change behaviour in two
# directions at once. Preserving the membership beats deriving it.
#
# Legacy values are retained PERMANENTLY, not time-boxed: sibling repos still
# carry pre-rename values on disk after this repo's records have migrated, and
# a half-migrated fleet is the normal state of a fleet vocabulary change.
#
# Each retired/successor pair is sourced from the canonical `_PRE_RENAME_ALIASES`
# table via `kind_values_for_canonical()` instead of being spelled as a literal
# collection here (AC4 -- see `test_baton_class_is_the_only_membership_set.py`).
_SPINOFF_CLASSIFIED_KINDS = frozenset(
    {"spinoff"}
    | set(kind_values_for_canonical("roadmap-baton"))
    | set(kind_values_for_canonical("goal-seed"))
)

def _is_ancestor_or_self(common_dir: Path, target_sha: str, tip_sha: str) -> bool:
    seen: set[str] = set()
    stack = [tip_sha]
    while stack:
        sha = stack.pop()
        if sha == target_sha:
            return True
        if sha in seen:
            continue
        seen.add(sha)
        commit = _commit_meta(common_dir, sha)
        if commit is None:
            continue
        stack.extend(commit["parents"])
    return False


def _in_process_branch_contains(dirs: _GitDirs, value: str) -> str:
    target = _resolve_revision(dirs, value)
    if target is None:
        return ""
    current = _current_branch_name(dirs)
    lines = []
    for name, tip_sha in _list_local_branches(dirs.common_dir):
        if _is_ancestor_or_self(dirs.common_dir, target, tip_sha):
            marker = "* " if name == current else "  "
            lines.append(f"{marker}{name}")
    return "\n".join(lines) + ("\n" if lines else "")


def _blob_text_at_commit_path(common_dir: Path, commit_sha: str, path: str) -> Optional[str]:
    resolved = _resolve_path_in_commit(common_dir, commit_sha, path)
    return resolved[1].decode("utf-8", errors="replace") if resolved is not None else None


def _in_process_show_path(dirs: _GitDirs, revision: str, path: str) -> Optional[str]:
    sha = _resolve_revision(dirs, revision)
    if sha is None:
        return None
    return _blob_text_at_commit_path(dirs.common_dir, sha, path)


def _in_process_pickaxe(common_dir: Path, start_sha: str, needle: str, path: str) -> Optional[str]:
    """Approximates `git log -1 --follow -S<needle> --format=%H -- path` —
    the most recent commit (walking back from `start_sha`) where the
    occurrence count of `needle` in the blob at `path` differs from ANY
    of its parents' occurrence counts at the same path (returns on the
    first differing parent, not "all parents differ"). Does NOT implement
    `--follow` rename-tracking — a needle introduced before `path`'s most
    recent rename is invisible to this walk. Also does NOT implement
    git's merge-history simplification: a merge commit that is TREESAME to
    one parent on this path (the merge's own content matches that parent
    exactly) is still returned here if any OTHER parent's count differs,
    where real git would walk past it into the TREESAME parent. "Most
    recent" also inherits `_walk_commits`'s pop-order approximation (see
    its docstring) — not a guarantee under clock skew.

    Both gaps were reproduced disagreeing with real git (stamp-integrity
    investigation, `tasks/mise-findings/stamp-integrity.md`, DoE-claude,
    2026-07-30, Root cause B) — including the merge case above with NO
    rename involved, which an earlier revision of this module's negative-
    spec comment incorrectly called "narrow... rename-only." Because that
    wrong-answer risk feeds a PM-gating verdict
    (`compute_execution_stamp_match`), `_find_stamp_commit` no longer calls
    this function (or routes through `_run_git`'s read-model dispatch at
    all) — it spawns real `git` directly instead. This function and
    `_dispatch_log`'s `-1 --follow -S...` arm remain only as the
    implementation `test_pickup_assemble_git_readmodel_parity.py` pins the
    divergence against; treat a change here as inert unless a new caller is
    added, and any new caller must be off a PM-gating verdict path or must
    itself verify against real git first."""
    needle_bytes = needle.encode("utf-8", errors="replace")
    for sha, commit in _walk_commits(common_dir, start_sha):
        current_blob = _blob_bytes_at_commit_path(common_dir, sha, path)
        current_count = current_blob.count(needle_bytes) if current_blob is not None else 0
        parents = commit["parents"]
        if not parents:
            if current_count != 0:
                return sha
            continue
        for parent_sha in parents:
            parent_blob = _blob_bytes_at_commit_path(common_dir, parent_sha, path)
            parent_count = parent_blob.count(needle_bytes) if parent_blob is not None else 0
            if parent_count != current_count:
                return sha
    return None


def _blob_bytes_at_commit_path(common_dir: Path, commit_sha: str, path: str) -> Optional[bytes]:
    resolved = _resolve_path_in_commit(common_dir, commit_sha, path)
    return resolved[1] if resolved is not None else None


def _dispatch_log(dirs: _GitDirs, rest: list[str]) -> Optional[str]:
    if rest[:1] == ["--oneline"]:
        pairs = _in_process_log_oneline(dirs, rest[1:])
        return "".join(f"{sha} {subject}\n" for sha, subject in pairs)

    if rest[:2] == ["-1", "--format=%ct"]:
        remainder = rest[2:]
        if remainder[:1] == ["--"]:
            path = remainder[1] if len(remainder) > 1 else ""
            sha = _latest_commit_touching_path(dirs, path)
        else:
            revision = remainder[0] if remainder else "HEAD"
            sha = _resolve_revision(dirs, revision)
        if sha is None:
            return ""
        commit = _commit_meta(dirs.common_dir, sha)
        if commit is None:
            return ""
        ts = commit["committer_epoch"]
        return f"{ts}\n" if ts is not None else ""

    if rest[:1] == ["-1"] and "--follow" in rest:
        needle: Optional[str] = None
        path: Optional[str] = None
        i = 0
        while i < len(rest):
            token = rest[i]
            if token.startswith("-S"):
                needle = token[2:]
            elif token == "--":
                path = rest[i + 1] if i + 1 < len(rest) else None
                break
            i += 1
        if needle is None or path is None:
            raise _GitReadModelError(f"unsupported pickaxe log args: {rest!r}")
        head_sha = _resolve_revision(dirs, "HEAD")
        if head_sha is None:
            return ""
        found = _in_process_pickaxe(dirs.common_dir, head_sha, needle, path)
        return f"{found}\n" if found else ""

    raise _GitReadModelError(f"unsupported log args: {rest!r}")


def _dispatch_git_readmodel(sub: str, rest: list[str], dirs: _GitDirs, root: Path) -> Optional[str]:
    if sub == "rev-parse":
        if rest == ["--show-toplevel"]:
            return f"{root}\n"
        if rest == ["--abbrev-ref", "HEAD"]:
            name = _current_branch_name(dirs)
            return f"{name}\n" if name else ""
        # `rev-parse HEAD` / `rev-parse <rev>` — the bare-revision form used by
        # `apply._scoped_commit` to read back the SHA it just committed. Resolve
        # via the read-model rather than forcing a spawn; a miss falls through
        # to the standard read-model-miss fallback (`returncode=1`).
        if len(rest) == 1 and not rest[0].startswith("-"):
            sha = _resolve_revision(dirs, rest[0])
            if sha is not None:
                return f"{sha}\n"
            raise _GitReadModelError(f"unresolved revision: {rest[0]!r}")
        raise _GitReadModelError(f"unsupported rev-parse args: {rest!r}")

    if sub == "log":
        return _dispatch_log(dirs, rest)

    if sub == "cat-file":
        if len(rest) == 2 and rest[0] == "-e":
            sha = _resolve_revision(dirs, rest[1])
            exists = sha is not None and _read_object(dirs.common_dir, sha) is not None
            return "" if exists else None
        raise _GitReadModelError(f"unsupported cat-file args: {rest!r}")

    if sub == "branch":
        if len(rest) == 2 and rest[0] == "--contains":
            return _in_process_branch_contains(dirs, rest[1])
        raise _GitReadModelError(f"unsupported branch args: {rest!r}")

    if sub == "show":
        if len(rest) == 1 and ":" in rest[0]:
            revision, _sep, path = rest[0].partition(":")
            return _in_process_show_path(dirs, revision, path)
        raise _GitReadModelError(f"unsupported show args: {rest!r}")

    raise _GitReadModelError(f"unsupported git subcommand: {sub!r}")


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Every git call in this module funnels through here. Most of the
    original spawn sites now dispatch to the in-process read-model above
    (`_dispatch_git_readmodel`) on `args[0]`; `status --porcelain` and
    `diff` are the two sanctioned residual spawns (see module docstring
    above `_NO_CONSOLE` for why — `diff` feeds `_classify_stamp_delta`'s
    PM-gating verdict and must stay byte-for-byte git-equivalent). The
    (OSError, subprocess.SubprocessError) / read-model-miss guard lives at
    this single choke point (Finding 4a) so a missing `git` binary, a
    permissions error, a `timeout=30` firing on a residual spawn, or an
    unresolved revision degrades to a uniform non-zero
    `CompletedProcess`-shaped fallback for every caller (`_current_branch`,
    `_branch_age_days`, `_git_log_oneline`, the SHA-premise branch of
    `compute_premise_checks`, `_commit_recency_signal`, `resolve_repo_root`)
    rather than propagating an uncaught exception through `brief()`/
    `main()`. Never raises."""
    argv_repr = ["git", "-C", str(cwd), *args]
    if not args:
        return subprocess.CompletedProcess(args=argv_repr, returncode=1, stdout="", stderr="")

    # `status`/`diff` are read-only spawns whose stat/index semantics the
    # read-model deliberately does not reproduce; `add`/`commit` are MUTATING
    # verbs a read-model can NEVER serve (it reads committed history, it cannot
    # stage or write objects) — `apply._scoped_commit` funnels its real commit
    # through here, so these must spawn real `git`. Routing a mutation to the
    # read-model was the W0-2 regression: it degraded to `returncode=1,
    # stderr=""`, surfacing as a valid `git add` failing with an empty error.
    if args[0] in _RUN_GIT_SPAWN_VERBS:
        try:
            return subprocess.run(
                ["git", "-C", str(cwd), *args],
                capture_output=True,
                text=True,
                timeout=30,
                **_NO_CONSOLE,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            # Preserve the real cause instead of an empty stderr — a mutation
            # that cannot spawn must be diagnosable (returncode + cwd + reason).
            return subprocess.CompletedProcess(
                args=argv_repr,
                returncode=1,
                stdout="",
                stderr=f"git {args[0]} spawn failed in {cwd}: {type(exc).__name__}: {exc}",
            )

    discovered = _discover_git_dirs(cwd)
    if discovered is None:
        return subprocess.CompletedProcess(args=argv_repr, returncode=1, stdout="", stderr="")
    root, dirs = discovered

    try:
        stdout = _dispatch_git_readmodel(args[0], args[1:], dirs, root)
    except (
        _GitReadModelError,
        OSError,
        zlib.error,
        struct.error,
        UnicodeDecodeError,
        IndexError,
        ValueError,
        # Review: code-reviewer — Finding 2: a cyclic/over-deep REF_DELTA
        # chain (not depth-guarded like the direct OFS_DELTA recursion —
        # see `_MAX_DELTA_DEPTH`) would otherwise propagate an uncaught
        # RecursionError past this "Never raises" chokepoint.
        RecursionError,
    ):
        return subprocess.CompletedProcess(args=argv_repr, returncode=1, stdout="", stderr="")

    if stdout is None:
        return subprocess.CompletedProcess(args=argv_repr, returncode=1, stdout="", stderr="")
    return subprocess.CompletedProcess(args=argv_repr, returncode=0, stdout=stdout, stderr="")


def resolve_repo_root(start: Optional[Path] = None) -> Optional[Path]:
    """Resolve the enclosing git worktree root for `start` (default cwd).

    Naked-Python path resolution (AC2b) — no shell, no bash. Returns None when
    `start` is not inside a git worktree (including when `_run_git` itself
    degraded to its fallback `CompletedProcess` — that fallback's `returncode
    != 0` already routes here, so no separate try/except is needed).

    NEGATIVE-SPEC — the `rev-parse --show-toplevel` hop is load-bearing, not a
    formality. `cwd = start or Path.cwd()` is the exact shape audited in
    `state/audits/2026-08-26-session-hub-writers-path-resolution.md` as the one
    that reaches a repo no caller named; this instance is safe ONLY because
    `--show-toplevel` re-anchors the process cwd to the worktree the process is
    actually inside. Building a path by joining onto `cwd` directly — or
    "simplifying" this to return `cwd` when the git call looks skippable —
    reintroduces that defect silently, and nothing warns.
    """
    cwd = start or Path.cwd()
    result = _run_git(["rev-parse", "--show-toplevel"], cwd)
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return Path(out) if out else None


def _current_branch(repo_root: Path) -> Optional[str]:
    result = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root)
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch or None


def _branch_age_days(repo_root: Path, branch: str) -> Optional[int]:
    """Days since the branch's tip commit — evidence only (contract §
    preflight), never a verdict."""
    result = _run_git(["log", "-1", "--format=%ct", branch], repo_root)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        commit_epoch = int(result.stdout.strip())
    except ValueError:
        return None
    now = datetime.now(timezone.utc).timestamp()
    return max(0, int((now - commit_epoch) // 86400))


# ---------------------------------------------------------------------------
# Archive-fallback + classification (Step 1.5 — MECHANICAL per the Staff Engineer F15)
# ---------------------------------------------------------------------------

def _search_dirs_for_basename(repo_root: Path, basename: str, rel_dirs: tuple[str, ...]) -> list[Path]:
    """The ONE place that knows how to recurse a set of repo-relative dirs
    for a file named `basename` and return every hit — detect-then-fail-
    loud on a multi-hit is the caller's job (never first-wins; contract §
    artifact), this just enumerates. `_archive_fallback_search` and
    `_live_fallback_search` are both thin callers over this one walk —
    do NOT add a second copy of this loop; the live-vs-archive distinction
    is which `rel_dirs` tuple is passed in, not a different algorithm.

    Bare-slug fallback (2026-07-28 defect fix): when `basename` carries no
    suffix, every artifact class this resolver serves (handoffs, memos,
    plans) is stored as `<slug>.md` on disk, so a literal `rglob(basename)`
    never matches anything and a bare slug was unconditionally unresolvable
    even when the file plainly exists. In that case both the literal
    `basename` AND `basename + ".md"` are searched, and every hit from
    either form is accumulated into the SAME returned list — deliberately
    narrowed to `.md` (never a `basename + ".*"` wildcard glob), since every
    artifact class here is markdown and a wildcard would let unrelated
    extensions in and muddy the multi-hit ambiguity contract. A basename
    that already carries a suffix is unaffected — literal match only, as
    before. Candidate-form generation itself is delegated to
    `coordinator_core.artifact_basename.md_fallback_candidates` (shared
    across three modules, see that module's docstring) — this function
    remains the one place that WALKS."""
    basenames = md_fallback_candidates(basename)
    hits: list[Path] = []
    for rel_dir in rel_dirs:
        base = repo_root / rel_dir
        if not base.is_dir():
            continue
        for candidate_basename in basenames:
            for candidate in base.rglob(candidate_basename):
                if candidate.is_file():
                    hits.append(candidate)
    return hits


def _archive_fallback_search(repo_root: Path, basename: str) -> list[Path]:
    """Recurse the three known archive dirs for `basename` — where a swept
    baton may have LANDED."""
    return _search_dirs_for_basename(repo_root, basename, ARCHIVE_DIRS)


def resolve_archived_basename(repo_root: Path, basename: str) -> list[Path]:
    """PUBLIC delegating wrapper over `_archive_fallback_search` — for
    callers outside this module (e.g. `plan_assemble.predicates.triage`)
    that need the archive-dirs walk without reimplementing it. Returns
    every hit found under `ARCHIVE_DIRS`; multi-hit disambiguation is the
    caller's job, exactly as `_archive_fallback_search` already documents —
    this wrapper adds no new walking code, it only exposes the existing
    one."""
    return _archive_fallback_search(repo_root, basename)


def _live_fallback_search(repo_root: Path, basename: str) -> list[Path]:
    """Recurse the three known live dirs for `basename` — where an open,
    un-actioned baton actually LIVES (2026-07-25 defect fix)."""
    return _search_dirs_for_basename(repo_root, basename, LIVE_DIRS)


#: Characters that count as a filename-component boundary for the suffix
#: tier's separator check (§ `_basename_has_slug_suffix`). Both are load-
#: bearing: `cross-repo/inbox`/`cross-repo/archive` memos and most
#: `docs/plans`/`archive/completed` slugs are hyphen-joined
#: (`2026-07-28-sender-foo-bar.md`), but `state/handoffs` — the single most
#: common pickup artifact class — is routinely
#: `YYYY-MM-DD_HHMMSS_slug.md` (verified on disk 2026-07-28, e.g.
#: `2026-07-04_201950_roadmap-strang-03.md`), so a hyphen-only boundary
#: would silently break suffix resolution for that whole class.
_SUFFIX_BOUNDARY_CHARS = ("-", "_")


def _basename_has_slug_suffix(candidate_name: str, slug: str) -> bool:
    """True when `candidate_name` (a file's bare basename, e.g.
    `2026-07-28-doe-claude-em-foo-bar.md`) ends with `slug` once a trailing
    `.md` is stripped from BOTH sides (2026-07-28 suffix-match tier), AND the
    match starts at a genuine filename-COMPONENT boundary — either index 0
    of the stripped stem, or immediately preceded by a
    `_SUFFIX_BOUNDARY_CHARS` separator. This is the ONE predicate both
    `_live_fallback_search_suffix` and `_archive_fallback_search_suffix`
    apply — do not re-derive it inline in either.

    The boundary check exists because a bare `.endswith()` also matches
    mid-word (slug `ate-recommendation` would cleanly match
    `...forwarder-gate-recommendation.md`) — a silent wrong-artifact pick
    with no ambiguity signal, worse than a clean not-found for a resolver
    whose result gets claimed and mutated (2026-07-28 review finding, PM
    ruling: fail-loud beats fuzzy here). Contrast the pre-existing elision
    tier's identical `endswith`-without-boundary laxity (`_elision_glob_pattern`):
    that tier only fires when the caller typed an explicit `…`/`...` marker,
    so its fuzziness is opt-in and visible in what was written — this tier
    fires on an ordinary-looking bare slug, so it does not get that same
    latitude.

    Suffix-only, deliberately: filenames in this contract are always
    `<date>-<sender>-<slug>`, so a caller who omitted the prefix is missing
    the HEAD of the name, not the tail — a prefix match would match the
    wrong end and let an unrelated slug that merely starts the same win.
    Case-sensitive by design (contract ask): do not lowercase either side.
    """
    stripped_name = candidate_name[: -len(".md")] if candidate_name.endswith(".md") else candidate_name
    stripped_slug = slug[: -len(".md")] if slug.endswith(".md") else slug
    if not stripped_name.endswith(stripped_slug):
        return False
    boundary_index = len(stripped_name) - len(stripped_slug)
    if boundary_index == 0:
        return True
    return stripped_name[boundary_index - 1] in _SUFFIX_BOUNDARY_CHARS


def _search_dirs_for_slug_suffix(repo_root: Path, slug: str, rel_dirs: tuple[str, ...]) -> list[Path]:
    """The ONE place that walks a set of repo-relative dirs looking for a
    file whose basename ENDS WITH `slug` (§ `_basename_has_slug_suffix`) —
    mirrors `_search_dirs_for_basename`'s role for the exact-match tier.
    `_archive_fallback_search_suffix` and `_live_fallback_search_suffix` are
    both thin callers over this one walk; do not add a second copy of the
    predicate application inside either.

    Callers are responsible for the `_MIN_SUFFIX_SLUG_LEN` floor — this
    function applies the predicate to whatever `slug` it is given and does
    not itself refuse a short one, so a caller skipping the length check
    would sweep the whole tree.
    """
    hits: list[Path] = []
    for rel_dir in rel_dirs:
        base = repo_root / rel_dir
        if not base.is_dir():
            continue
        for candidate in base.rglob("*"):
            if candidate.is_file() and _basename_has_slug_suffix(candidate.name, slug):
                hits.append(candidate)
    return hits


def _archive_fallback_search_suffix(repo_root: Path, slug: str) -> list[Path]:
    """Suffix-match `slug` against every archive-dir basename — where a swept
    baton may have LANDED (mirrors `_archive_fallback_search`'s exact-match
    counterpart)."""
    return _search_dirs_for_slug_suffix(repo_root, slug, ARCHIVE_DIRS)


def _live_fallback_search_suffix(repo_root: Path, slug: str) -> list[Path]:
    """Suffix-match `slug` against every live-dir basename — where an open,
    un-actioned baton actually LIVES (mirrors `_live_fallback_search`'s
    exact-match counterpart)."""
    return _search_dirs_for_slug_suffix(repo_root, slug, LIVE_DIRS)


# ---------------------------------------------------------------------------
# Elision-tolerant path resolution (2026-07-24 incident) — a PM/EM baton
# handoff is routinely copy-pasted out of a terminal transcript, and the
# terminal elides long paths (a UUID mid-filename gets replaced with a
# single-glyph U+2026 or an ASCII `...` run). `resolve_artifact` treats
# such a basename as a glob pattern instead of failing closed on a path
# that was never going to exist literally.
# ---------------------------------------------------------------------------

#: Single-glyph ellipsis a terminal may substitute for an elided path run.
_ELLIPSIS_CHAR = "…"


def _is_elided_basename(basename: str) -> bool:
    """True when `basename` carries either elision marker (U+2026 or the
    ASCII `...` three-dot run) — the trigger for glob-based resolution."""
    return _ELLIPSIS_CHAR in basename or "..." in basename


def _elision_glob_pattern(basename: str) -> str:
    """Split `basename` on its elision marker into a `<prefix>*<suffix>`
    glob pattern — e.g. `2026-07-24_210324_…md` ->
    `2026-07-24_210324_*md`. Prefers the ASCII `...` split when both
    markers happen to be present; callers only invoke this after
    `_is_elided_basename` confirms at least one is."""
    marker = "..." if "..." in basename else _ELLIPSIS_CHAR
    prefix, _, suffix = basename.partition(marker)
    return f"{prefix}*{suffix}"


def _is_safe_elision_path(artifact_path: str) -> bool:
    """False for an absolute path or a path carrying a literal `..`
    traversal component — elision resolution never globs on those inputs
    (contract § security: search stays inside the repo). A caller that
    fails this check simply gets no elision resolution, which falls
    through to the pre-existing not-found error path unchanged."""
    candidate = Path(artifact_path)
    if candidate.is_absolute():
        return False
    return not any(part == ".." for part in candidate.parts)


def _elision_search_roots(artifact_path: str, repo_root: Path) -> list[tuple[Path, bool]]:
    """`(root, recursive)` pairs to glob, in search order: the directory
    named in the passed path first (non-recursive — that is the exact
    directory the caller named), then each `LIVE_DIRS` entry (recursive —
    an open baton benefits from the same widened search a literal-basename
    lookup gets, 2026-07-25 defect fix), then each `ARCHIVE_DIRS` entry
    (recursive — those are `YYYY-MM`-sharded, per contract § archive-
    fallback). Live roots are searched before archive roots, matching
    `resolve_artifact`'s "search live first" ordering."""
    passed_dir = repo_root / Path(artifact_path).parent
    roots: list[tuple[Path, bool]] = [(passed_dir, False)]
    roots.extend((repo_root / rel_dir, True) for rel_dir in LIVE_DIRS)
    roots.extend((repo_root / rel_dir, True) for rel_dir in ARCHIVE_DIRS)
    return roots


def _resolve_elided_artifact(artifact_path: str, repo_root: Path) -> list[Path]:
    """Glob-resolve an elided `artifact_path`'s basename against the search
    roots. Returns every distinct match found, in root-search order,
    deduplicated by resolved absolute path (the passed-path directory and
    an `ARCHIVE_DIRS` entry can overlap when the caller names a native
    archive path). Returns `[]` unconditionally for an unsafe path
    (§ `_is_safe_elision_path`) rather than raising — the caller falls
    through to the ordinary not-found flow."""
    if not _is_safe_elision_path(artifact_path):
        return []
    pattern = _elision_glob_pattern(Path(artifact_path).name)
    hits: list[Path] = []
    seen: set[Path] = set()
    for root_dir, recursive in _elision_search_roots(artifact_path, repo_root):
        if not root_dir.is_dir():
            continue
        globber = root_dir.rglob(pattern) if recursive else root_dir.glob(pattern)
        for candidate in sorted(globber):
            if not candidate.is_file():
                continue
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            hits.append(candidate)
    return hits


class _ArtifactElisionInconclusive(Exception):
    """Raised when an elided `artifact_path` glob-resolves to 2+ distinct
    candidates — a genuine ask, never a "most recent wins" guess (contract
    § "surface to PM, do not guess"). Carries every candidate (repo-
    relative where possible) so `brief()` can build the judgment point
    verbatim."""

    def __init__(self, artifact_path: str, candidates: list[str]):
        super().__init__(artifact_path)
        self.artifact_path = artifact_path
        self.candidates = candidates


def _extract_terminal_fields(fm_text: str, field_names: tuple[str, ...]) -> dict[str, Any]:
    return {
        name: read_fm_field_unquoted(fm_text, name)
        for name in field_names
        if read_fm_field_unquoted(fm_text, name) is not None
    }


def _is_spinoff_kind(kind: str | None) -> bool:
    """`kind` values that classify as `spinoff` (contract § MECHANICAL
    checklist, Step 1.5 classification table; `coordinator/CLAUDE.md`'s
    spinoff-lineage enumeration) — a spinoff is a fork with
    `predecessor: none`, not a continuation. This classifier feeds
    `classify()`'s `spinoff` vs `handoff` split; it does NOT feed pickup/
    SKILL.md's recovery-banner prepend step ("a recovery means the prior
    session died uncleanly") — that fact is `compute_claim_grant`'s
    `gates.claim_grant.unclean_prior_holder` field, an unrelated producer.
    (Prior revisions of this docstring claimed the opposite; there was no
    producer for that banner at all until `unclean_prior_holder` was added
    — see that field's docstring.)

    C3 (baton-kind-vocabulary migration): replaces the former
    `_SPINOFF_KINDS = frozenset({"spinoff", "spinoff-roadmap", "spinoff-goal"})`
    with a call to the canonical `baton_class()` derivation (C2/D2) plus one
    explicit compatibility literal.

    FINDING — the original three-member set does not correspond to one
    `baton_class`: `spinoff` and `spinoff-goal` both derive `deflection`
    (goal-seed under D1's rename), but `spinoff-roadmap` (D1's still-live
    pre-rename source name for `roadmap-baton`) derives `intention`. The
    original set was already spanning two axis values under one boolean;
    `baton_class()` does not collapse that — it makes the crossing visible.
    Preserved verbatim (not silently narrowed to one class) rather than
    resolved here; report only, per this chunk's brief.
    """
    return kind in _SPINOFF_CLASSIFIED_KINDS


def _has_memo_shape(fm_text: str) -> bool:
    """`from:`+`to:` presence — the frontmatter-shape discriminator that
    distinguishes a memo from a handoff/spinoff independent of path or live
    status. Factored out of `classify()` so the archive-fallback path in
    `resolve_artifact` (which resolves a terminal/archived artifact whose
    `status:` need not match any of `classify()`'s live-status enums) can
    reuse the same test rather than a second, path-string-based heuristic
    (Finding 7)."""
    return (
        read_fm_field_unquoted(fm_text, "from") is not None
        and read_fm_field_unquoted(fm_text, "to") is not None
    )


#: The receiver-side memo terminal-status vocabulary — a memo whose frontmatter
#: carries either value is a closed, terminal record (never pickup-able). Kept
#: as ONE module-level frozenset so a fourth terminal status added later cannot
#: drift across the three consumers below that each independently need this
#: set (classify's memo-shape gate, the archive-fallback classification, and
#: the M0 terminal short-circuit) — a literal `{"actioned", "superseded"}` at
#: each of the three sites would be exactly the fork-not-share pattern this
#: plan's Problem section names as the cost of leaving them hard-coded.
_MEMO_TERMINAL_STATUS = frozenset({"actioned", "superseded"})


def classify(path: Path, fm_text: str, repo_root: Path) -> str:
    """Path + frontmatter shape -> handoff | memo | spinoff | ambiguous.

    Mirrors pickup/SKILL.md's classification table (§ MECHANICAL checklist,
    Step 1.5) verbatim: `state/handoffs/` + status/deployment_state shape ->
    handoff (or spinoff, when `kind: spinoff`); `cross-repo/inbox/` OR
    from:+to:+status: open|actioned -> memo; otherwise -> ambiguous (do not
    guess).
    """
    in_handoffs_dir = "state/handoffs" in path.as_posix()
    in_inbox_dir = "cross-repo/inbox" in path.as_posix()

    kind = read_fm_field_unquoted(fm_text, "kind")
    status = read_fm_field_unquoted(fm_text, "status")
    deployment_state = read_fm_field_unquoted(fm_text, "deployment_state")

    if in_handoffs_dir and status in {"active", "consumed", "open", "claimed"}:
        if deployment_state is not None:
            if _is_spinoff_kind(kind):
                return "spinoff"
            return "handoff"
        # Ledger-first (C11, row 20): a mirror revert can drop
        # `deployment_state` alongside the claim stamp, which would
        # otherwise fall through to `ambiguous` and refuse to route a live,
        # ledger-claimed handoff at all (fact-find row 20). A live ledger
        # claim on this path can only exist for a handoff/spinoff —
        # `claim_state` is class-generic onto handoffs only, per that
        # module's own Anti-scope — so it settles the classification
        # without needing `deployment_state` to have survived the revert.
        try:
            claim_state = resolve_claim_state(repo_root / path, repo_root=repo_root)
        except Exception:
            claim_state = None
        if claim_state is not None and claim_state.source == "ledger":
            if _is_spinoff_kind(kind):
                return "spinoff"
            return "handoff"

    if in_inbox_dir or (_has_memo_shape(fm_text) and (status == "open" or status in _MEMO_TERMINAL_STATUS)):
        return "memo"

    return "ambiguous"


def _is_under_archive_dir(path: Path, repo_root: Path) -> bool:
    """True when `path` resolves inside one of `ARCHIVE_DIRS` — reuses the
    same enumeration `_archive_fallback_search` scans, applied to a path
    that was passed directly rather than discovered by search."""
    if not _is_relative(path, repo_root):
        return False
    # Review: code-reviewer Finding 4 — pre-existing site left off the
    # shared rel_id helper when this module's other 12 sites were converted.
    rel = rel_id(path, repo_root)
    return any(rel == d or rel.startswith(d + "/") for d in ARCHIVE_DIRS)


def _build_archived_resolution(display_path: str, archive_hit: Path, repo_root: Path) -> dict[str, Any]:
    """Builds the terminal `archived` artifact block for a single resolved
    archive-resident file — shared by the archive-fallback search hit and by
    a native archive path passed directly (Defect 2), so both routes to a
    swept baton land on the same terminal `artifact.resolution` rather than
    the direct route dead-ending in `ambiguous`. Reuses `_has_memo_shape`
    (Finding 7's discriminator) to pick the terminal-field set instead of a
    second path heuristic.

    `resolution.archived_class` (2026-07-27 defect fix) carries that SAME
    `_has_memo_shape` verdict forward as an explicit `"memo" | "handoff"`
    discriminator — not just used to pick `terminal_fields`'s key set and
    then discarded. Before this field existed, the top-level
    `classification` was always the single literal string `"archived"`
    regardless of what kind of artifact was archived, so a caller keyed
    purely off `classification` (as `pickup_assemble.apply._class_and_
    basename` was) had no way to recover memo-vs-handoff for an archived
    artifact and silently guessed `"handoff"` for every one — see that
    function's own docstring for the caller-side half of this fix."""
    text = archive_hit.read_text(encoding="utf-8", errors="replace")
    split = split_frontmatter(text)
    fm_text = split.fm_text if split is not None else ""
    is_memo = _has_memo_shape(fm_text)
    terminal_fields = _extract_terminal_fields(
        fm_text, _TERMINAL_MEMO_FIELDS if is_memo else _TERMINAL_HANDOFF_FIELDS
    )
    # `frontmatter` stays `{}` here (pre-existing shape, unrelated to this
    # field) — `chain` is computed straight off `archive_hit`'s own parsed
    # frontmatter (AC8: a month-nested `archive/handoffs/YYYY-MM/` artifact
    # computes the same chain as its live-path twin), not off the
    # deliberately-terminal-only `terminal_fields` extraction above.
    # `is_memo` (not the "archived" classification) is the discriminator here:
    # an archived MEMO is terminal correspondence with no continuity chain, and
    # `classification` has already collapsed to "archived" for both shapes by
    # this point, so keying on it alone would emit a chain block for a memo.
    chain = None if is_memo else _compute_artifact_chain(archive_hit, repo_root, "archived")
    return {
        "path": display_path,
        "classification": "archived",
        "frontmatter": {},
        "resolution": {
            "status": "archived",
            "archive_path": rel_id(archive_hit, repo_root),
            "terminal_fields": terminal_fields,
            "archived_class": "memo" if is_memo else "handoff",
        },
        "chain": chain,
    }


#: Matched-pair wrapper punctuation a caller-pasted path is commonly found
#: wrapped in when it's rendered inline in prose (a memo sentence, a chat
#: message) — see `_sanitize_artifact_path_str`.
_PATH_WRAPPERS = {"(": ")", "[": "]", "<": ">", '"': '"', "'": "'", "`": "`"}

#: Sentence-final punctuation a caller-pasted path commonly picks up when a
#: human ends a sentence with it (`...fix.md.`, `...fix.md,` etc).
_TRAILING_SENTENCE_PUNCT = ".,;:!?"

#: A hard line wrap the rendering surface inserted into a pasted path, plus
#: the continuation indent on either side of it — see
#: `_sanitize_artifact_path_str` § Line-wrap tolerance for why this rejoins
#: with no separator. Deliberately NOT `\s` on either side: a match must be
#: anchored on a real newline, so a path containing an ordinary interior
#: space is never touched.
_LINE_WRAP_RE = re.compile(r"[ \t]*\r?\n[ \t]*")


def _last_path_segment(path_str: str) -> str:
    """Returns the final `/`- or `\\`-delimited segment of `path_str` (both
    separators, since Windows paths pasted here may use either) — used by
    `_sanitize_artifact_path_str` to detect a bare `.`/`..` component before
    stripping trailing punctuation would corrupt it."""
    return re.split(r"[\\/]", path_str)[-1] if path_str else path_str


def _sanitize_artifact_path_str(raw: str) -> str:
    """Fallback-candidate generator: strips prose wrappers and trailing
    sentence punctuation a human commonly pastes around an inline artifact
    path (2026-07-27 incident — `/coordinator:pickup <path>.` with a
    sentence-final period reported "not found" for a file that plainly
    existed, because the literal string carried the trailing `.`).

    Strips, to a fixed point (so a wrapped-and-punctuated path like
    "(`foo.md`)." resolves fully in one call):
      - surrounding whitespace;
      - an interior HARD LINE WRAP — a `\\n`/`\\r\\n` plus whatever
        horizontal whitespace the wrapping surface put on either side of it
        — rejoined with NO separator (§ line-wrap tolerance below);
      - a matched wrapper pair — `(...)`, `[...]`, `<...>`, `"..."`,
        `'...'`, backticks — ONLY when BOTH ends match; an unmatched
        leading `(` with no closing `)` is left untouched;
      - a single trailing `.`/`,`/`;`/`:`/`!`/`?`.

    Line-wrap tolerance (2026-08-10, PM ask — the Windows case): a long
    absolute path pasted into a prompt, a terminal, or a slash-command
    argument is routinely hard-wrapped mid-token by the rendering surface,
    arriving as `...predecessor-not-th\\n  e-new-baton.md`. Windows paths
    are long enough that this is the common case there, not an edge one.
    A newline is not a legal character in a Windows filename and is
    vanishingly rare in a POSIX one, so its presence is evidence of the
    wrap itself, never of the path — the rejoin is unconditional and the
    adjacent horizontal whitespace (the continuation indent) goes with it.

    Negative-spec — the rejoin uses NO separator, so a wrap that fell on a
    genuine SPACE inside a path (`.../My Documents/x.md` broken after
    `My`) is NOT recovered: nothing in the wrapped string distinguishes a
    consumed space from a mid-token break, and coordinator artifact
    basenames are kebab-case by construction. Do not "fix" this by also
    emitting a space-joined variant unless the caller is first widened to
    take a candidate LIST — a second single-string guess would just move
    which of the two cases silently resolves wrong.

    Negative-spec — this is a FALLBACK CANDIDATE, never a normalizer: the
    caller must attempt resolution with the RAW string first and only fall
    back to this function's output on a miss, so a legitimately-period-
    ending or dot-component path is never mutated out from under a caller
    whose raw input already resolves.

    Guards (do not weaken without re-reading the incident writeup):
      - never strips trailing punctuation off a bare `.` or `..` PATH
        COMPONENT (the last `/`/`\\`-delimited segment) — stripping `..` or
        `.` would silently change which directory the path names;
      - never strips a lone trailing `:` off a bare Windows drive letter
        (`C:` must keep its colon — a whole-string match, not a segment
        check, since a drive letter is never a path *component* alongside
        others).
    """
    s = raw
    while True:
        stripped = s.strip()
        if stripped != s:
            s = stripped
            continue
        unwrapped = _LINE_WRAP_RE.sub("", s)
        if unwrapped != s:
            s = unwrapped
            continue
        if len(s) >= 2 and s[0] in _PATH_WRAPPERS and s[-1] == _PATH_WRAPPERS[s[0]]:
            s = s[1:-1]
            continue
        if s and s[-1] in _TRAILING_SENTENCE_PUNCT:
            if _last_path_segment(s) in (".", ".."):
                break
            if s[-1] == ":" and re.match(r"^[A-Za-z]:$", s):
                break
            s = s[:-1]
            continue
        break
    return s


def _literal_hit(path: Path) -> bool:
    """`is_file()`, but rejects a hit that only "exists" because Win32
    silently strips trailing dots/spaces off the final path component
    (`Path("h1.md.").is_file()` is True on Windows, resolving to `h1.md` —
    POSIX has no such normalization). Without this guard, a raw literal
    carrying caller-pasted trailing punctuation (`_sanitize_artifact_path_
    str`'s own target case) spuriously "hits" on Windows only, so the
    RAW-then-sanitized fallback contract above silently skips the sanitize
    step and the trailing punctuation leaks into the returned display path
    — never reproducible on POSIX, so it must be caught here rather than
    relying on the OS-level is_file() check alone."""
    if not path.is_file():
        return False
    name = path.name
    if not name or name[-1] not in ". ":
        return True
    try:
        return any(entry.name == name for entry in path.parent.iterdir())
    except OSError:
        return False


def _reanchor_repo_relative(artifact_path: str, repo_root: Path) -> Optional[Path]:
    """Re-anchor a `/<repo-basename>/<rest>` (or bare `<repo-basename>/<rest>`)
    path at the actual repo root when it does not exist literally.

    Handles the common paste shape a caller produces when they name the repo
    directory but not its real filesystem parent — e.g.
    `/claude-klabauter/cross-repo/inbox/m.md` where the repo actually lives at
    `/Users/…/X/claude-klabauter`. Strips the leading `/<repo-basename>/` segment
    and re-anchors the remainder at `repo_root`. Returns the re-anchored `Path`
    only when it exists on disk, else `None` (a no-op — the caller keeps the
    original literal path so the archive-fallback / not-found paths are
    unchanged).
    """
    basename = repo_root.name
    segments = artifact_path.lstrip("/").split("/")
    if len(segments) >= 2 and segments[0] == basename:
        candidate = repo_root.joinpath(*segments[1:])
        # Review: code-reviewer — routed through _literal_hit (not a bare
        # is_file()) for consistency with the three call sites inside
        # resolve_artifact's main chain; a caller no longer has to re-check
        # this function's return value for a Win32 trailing-dot/space
        # false-positive.
        if _literal_hit(candidate):
            return candidate
    return None


def _resolve_found_file(found_path: Path, repo_root: Path) -> dict[str, Any]:
    """Reads + classifies a file that is confirmed to exist on disk —
    shared by `resolve_artifact`'s direct-literal-path hit and its
    resolved-live-basename hit (`_live_fallback_search`), so there is
    exactly one place that turns "a file exists at this Path" into an
    `artifact` block. Do NOT inline a second copy of this at either call
    site (the shape to avoid, per this module's brief)."""
    display_path = rel_id(found_path, repo_root) if _is_relative(found_path, repo_root) else str(found_path)
    text = found_path.read_text(encoding="utf-8", errors="replace")
    split = split_frontmatter(text)
    if split is None:
        return {
            "path": display_path,
            "classification": "ambiguous",
            "frontmatter": {},
            "resolution": None,
            "chain": None,
        }
    classification = classify(found_path, split.fm_text, repo_root)
    # Defect 2 — a well-formed handoff/memo passed at its NATIVE archive
    # path (e.g. `archive/handoffs/2026-07/...`) satisfies `found_path`
    # existing directly, so it never reaches the archive-fallback search;
    # `classify()`'s `in_handoffs_dir`/`in_inbox_dir` path checks then
    # correctly find neither and fall through to `ambiguous`. Resolve it to
    # the same terminal `archived` shape the fallback search produces for a
    # swept baton, rather than surfacing a resolvable terminal artifact as
    # PM-facing `ambiguous`.
    if classification == "ambiguous" and _is_under_archive_dir(found_path, repo_root):
        return _build_archived_resolution(display_path, found_path, repo_root)
    fm = _parse_fm_dict(split.fm_text)
    return {
        "path": display_path,
        "classification": classification,
        "frontmatter": fm,
        "resolution": None,
        "chain": _compute_artifact_chain(found_path, repo_root, classification),
    }


def resolve_artifact(artifact_path: str, repo_root: Path) -> dict[str, Any]:
    """Resolve `artifact_path` (live-dir- and archive-fallback-aware,
    elision-tolerant) and classify it.

    When `artifact_path` doesn't exist literally, a bare basename is
    searched against BOTH `LIVE_DIRS` (where an open, un-actioned baton
    lives) and `ARCHIVE_DIRS` (where a swept one may have landed) — never
    just the latter (2026-07-25 defect fix). A single live hit resolves as
    an ordinary pickup (`classify()`'s handoff/spinoff/memo/ambiguous), a
    single archive hit resolves as a terminal `archived` record, and any
    basename hitting more than one location across BOTH sets surfaces as
    `classification: ambiguous` carrying every candidate — never a
    first-wins guess, and never silently preferring live over archived.

    Returns the `artifact` block of the decision object. Raises
    `_ArtifactUnreadable` on absent-and-not-found-anywhere (business failure, exit 1),
    `_ArtifactElisionInconclusive` when an elided basename glob-resolves to
    2+ candidates (business failure, exit 1 — a genuine ask, never a
    guess), and `_ArtifactAmbiguous` on unparseable/missing frontmatter
    (also exit 1 — the contract routes "surface to PM, do not guess"
    through the ambiguous classification, never a silent guess).

    Elision tolerance (2026-07-24 incident): when the passed basename
    carries a `…`/`...` elision marker, it is glob-resolved BEFORE the
    literal-path/archive-fallback logic below runs at all — a unique match
    substitutes the resolved literal path for the remainder of this
    function (the returned dict carries an `elision_resolution` key the
    caller narrates); a zero-match falls through unchanged, reproducing
    the pre-existing not-found error verbatim, because the still-elided
    basename cannot match anything downstream either.

    Suffix-match tolerance (2026-07-28, PM ruling): if the exact-basename
    tier (raw + sanitized) finds NOTHING, and the passed slug is at least
    `_MIN_SUFFIX_SLUG_LEN` chars, a second tier retries matching any
    `LIVE_DIRS`/`ARCHIVE_DIRS` basename that ENDS WITH the slug at a genuine
    filename-component boundary (a caller who omitted the
    `<date>-<sender>-` filename prefix) — never a bare prefix match, and
    never a mid-word match (§ `_basename_has_slug_suffix`). A single suffix
    hit resolves exactly like a single
    exact-basename hit and the returned dict carries a `suffix_resolution`
    key naming the guess; a suffix hit alongside another (live or archive)
    still surfaces as `classification: ambiguous` through the SAME
    multi-hit path as an exact-tier collision — never a silent
    longest-match/newest-date/prefer-live tiebreak.
    """
    #: Captured before any elision/sanitize transform mutates `artifact_path`
    #: below — the revision tier (2026-08-14) matches this RAW input against
    #: a sha shape, never a transformed/sanitized form (a real sha has no
    #: punctuation to sanitize and is never elided).
    _original_artifact_path = artifact_path

    elision_resolution: Optional[dict[str, str]] = None
    if _is_elided_basename(Path(artifact_path).name):
        candidates = _resolve_elided_artifact(artifact_path, repo_root)
        if len(candidates) > 1:
            raise _ArtifactElisionInconclusive(
                artifact_path,
                sorted(
                    rel_id(c, repo_root) if _is_relative(c, repo_root) else str(c)
                    for c in candidates
                ),
            )
        if len(candidates) == 1:
            resolved = candidates[0]
            resolved_display = (
                rel_id(resolved, repo_root) if _is_relative(resolved, repo_root) else str(resolved)
            )
            elision_resolution = {"passed": artifact_path, "resolved": resolved_display}
            artifact_path = resolved_display
        # else: zero matches — fall through with artifact_path unchanged.

    #: Set the moment a sanitized (punctuation-stripped) form of the passed
    #: path is what actually resolved — either directly (below) or via the
    #: basename fallback search — so `_tag` and `brief()` can narrate the
    #: correction instead of silently swallowing it (contract item 3).
    sanitize_resolution: Optional[dict[str, str]] = None

    #: Set the moment a bare, prefix-omitted SUFFIX of the passed slug is
    #: what actually resolved (2026-07-28 tier, PM ruling) — narrated the
    #: same way as `elision_resolution`/`sanitize_resolution` so a caller can
    #: see the engine guessed and what it landed on, never silently.
    suffix_resolution: Optional[dict[str, str]] = None

    #: Set the moment a git revision SHA is what actually resolved
    #: (2026-08-14 tier) — narrated the same "passed" -> "resolved" shape as
    #: the other tiers above, so a caller who cited a delivery-commit SHA
    #: sees the engine resolved it and what artifact it landed on.
    revision_resolution: Optional[dict[str, str]] = None

    def _tag(result: dict[str, Any]) -> dict[str, Any]:
        if elision_resolution is not None:
            result = {**result, "elision_resolution": elision_resolution}
        if sanitize_resolution is not None:
            result = {**result, "sanitize_resolution": sanitize_resolution}
        if suffix_resolution is not None:
            result = {**result, "suffix_resolution": suffix_resolution}
        if revision_resolution is not None:
            result = {**result, "revision_resolution": revision_resolution}
        return result

    live_path = (repo_root / artifact_path) if not Path(artifact_path).is_absolute() else Path(artifact_path)

    if not _literal_hit(live_path):
        reanchored = _reanchor_repo_relative(artifact_path, repo_root)
        if reanchored is not None:
            live_path = reanchored

    if not _literal_hit(live_path):
        # RAW path missed literally — retry with the sanitized (wrapper-/
        # trailing-punctuation-stripped) form as a FALLBACK candidate, never
        # a mutation of the working input (raw always tried first, above).
        sanitized_path = _sanitize_artifact_path_str(artifact_path)
        if sanitized_path != artifact_path:
            candidate = (
                (repo_root / sanitized_path)
                if not Path(sanitized_path).is_absolute()
                else Path(sanitized_path)
            )
            if not candidate.is_file():
                # Not routed through _literal_hit: relies on the invariant
                # that _sanitize_artifact_path_str already stripped trailing
                # sentence punctuation from `sanitized_path`'s basename, so
                # it can no longer end in "."/" " and trip Win32's
                # trailing-dot/space is_file() false positive (Review:
                # code-reviewer — this reliance was implicit; if
                # _sanitize_artifact_path_str's stripping behavior ever
                # changes, this call site depends on it staying true).
                reanchored = _reanchor_repo_relative(sanitized_path, repo_root)
                if reanchored is not None:
                    candidate = reanchored
            if candidate.is_file():
                # Same sanitize-strips-trailing-punctuation invariant as above.
                sanitize_resolution = {"passed": artifact_path, "resolved": sanitized_path}
                live_path = candidate
                artifact_path = sanitized_path

    if _literal_hit(live_path):
        return _tag(_resolve_found_file(live_path, repo_root))

    # Not found at the passed path — search where a baton actually LIVES
    # before searching where a swept one may have LANDED (Step 1.5, widened
    # 2026-07-25: the original archive-only fallback left every live,
    # un-actioned basename unresolvable). Both searches always run and are
    # reported separately — a live hit is an ordinary, actionable pickup; an
    # archive hit is a terminal record; the two are never merged into one
    # "first wins" bucket (contract § artifact, requirement 1/2 above).
    basename = Path(artifact_path).name
    live_hits = _live_fallback_search(repo_root, basename)
    archive_hits = _archive_fallback_search(repo_root, basename)
    total_hits = len(live_hits) + len(archive_hits)
    # Accumulated incrementally as each search actually runs, rather than
    # re-derived at the raise site below (Finding 1, 2026-07-28 review):
    # a static re-derivation silently under-reported when the sanitized
    # retry below also ran and also found nothing, and never revealed that
    # a sanitize retry happened at all. This list is the ONLY source of
    # truth the not-found error draws from.
    tried_basenames = md_fallback_candidates(basename)

    if total_hits == 0:
        sanitized_basename = _sanitize_artifact_path_str(basename)
        if sanitized_basename != basename:
            sanitized_live_hits = _live_fallback_search(repo_root, sanitized_basename)
            sanitized_archive_hits = _archive_fallback_search(repo_root, sanitized_basename)
            sanitized_total = len(sanitized_live_hits) + len(sanitized_archive_hits)
            # Unconditional: the sanitized forms were genuinely searched
            # above regardless of whether they found anything, so they
            # belong in `tried_basenames` unconditionally too — gating this
            # on `sanitized_total > 0` is exactly the bug Finding 1 named.
            tried_basenames.extend(md_fallback_candidates(sanitized_basename))
            if sanitized_total > 0:
                if sanitize_resolution is None:
                    sanitize_resolution = {"passed": artifact_path, "resolved": sanitized_basename}
                live_hits, archive_hits, total_hits = (
                    sanitized_live_hits,
                    sanitized_archive_hits,
                    sanitized_total,
                )

    # Suffix-match tier (2026-07-28, PM ruling) — ONLY attempted once the
    # exact-basename tier above (raw + sanitized) has found nothing at all.
    # A caller passing a unique TAIL of a memo/handoff basename with the
    # `<date>-<sender>-` prefix omitted (e.g. `foo-bar` for
    # `2026-07-28-sender-foo-bar.md`) should still resolve — but this must
    # never outrank an exact hit, and a bare PREFIX must never match (§
    # `_basename_has_slug_suffix`).
    if total_hits == 0:
        stripped_slug = basename[: -len(".md")] if basename.endswith(".md") else basename
        if len(stripped_slug) >= _MIN_SUFFIX_SLUG_LEN:
            # Unconditional the moment the length floor is cleared — same
            # discipline as the sanitized-retry tier above: a tier that ran
            # but is only recorded on success hides that it ran at all from
            # the not-found error (this is exactly Finding 1's bug, applied
            # to a second tier).
            tried_basenames.append(f"*{stripped_slug} (suffix match)")
            suffix_live_hits = _live_fallback_search_suffix(repo_root, basename)
            suffix_archive_hits = _archive_fallback_search_suffix(repo_root, basename)
            suffix_total = len(suffix_live_hits) + len(suffix_archive_hits)
            if suffix_total > 0:
                live_hits, archive_hits, total_hits = (
                    suffix_live_hits,
                    suffix_archive_hits,
                    suffix_total,
                )
                if suffix_total == 1:
                    # Narrate the guess — mirrors `elision_resolution` /
                    # `sanitize_resolution`'s "passed" -> "resolved" shape.
                    # A multi-hit suffix match falls straight into the
                    # `total_hits > 1` ambiguous branch below unnarrated —
                    # that branch already surfaces every candidate path, so
                    # there is no single "resolved" value to name.
                    only_hit = (suffix_live_hits or suffix_archive_hits)[0]
                    resolved_display = (
                        rel_id(only_hit, repo_root)
                        if _is_relative(only_hit, repo_root)
                        else str(only_hit)
                    )
                    suffix_resolution = {"passed": artifact_path, "resolved": resolved_display}

    # Revision tier (2026-08-14): a caller citing a git commit/revision SHA
    # instead of an artifact path — a peer EM habitually cites a memo's
    # delivery-commit SHA, never its filepath. Only attempted once every
    # earlier tier above has found nothing, so a real path/basename never
    # reaches here (non-regressive by construction). Resolve the revision,
    # take the `.md` paths it changed under `LIVE_DIRS + ARCHIVE_DIRS`, and
    # feed the resulting BASENAMES back through the same exact-basename
    # search used above rather than trusting the commit-time path — a memo
    # delivered to `cross-repo/inbox/` and later actioned has since moved to
    # `cross-repo/archive/`, so the commit-time path is stale-by-default; if
    # the basename search finds nothing but the commit-time path still
    # exists on disk, that literal path is the fallback.
    _revision_resolved_no_artifact = False
    _revision_sha_display: Optional[str] = None
    _revision_deleted_unresolvable: list[str] = []
    # `_revision_unresolvable` and `_revision_resolved_no_artifact` /
    # `_revision_deleted_unresolvable` are mutually exclusive by
    # construction: the former is set only in the `rev_sha is None` branch
    # below, the latter two only inside `rev_sha is not None`. No ordering
    # discipline is needed at the raise site to keep a resolved revision
    # from ever reaching the new arm — it structurally cannot.
    _revision_unresolvable = False
    if total_hits == 0 and _REVISION_SHA_RE.fullmatch(_original_artifact_path):
        # Unconditional the moment the shape matches, mirroring the
        # sanitize/suffix tiers' discipline above: recorded whether or not
        # the revision itself resolves, or resolves to any artifact.
        tried_basenames.append(f"revision {_original_artifact_path!r}")
        discovered = _discover_git_dirs(repo_root)
        rev_sha = _resolve_revision(discovered[1], _original_artifact_path) if discovered is not None else None
        if discovered is not None and rev_sha is not None:
            _revision_sha_display = rev_sha
            common_dir = discovered[1].common_dir
            changed_paths = _changed_md_paths_for_revision(common_dir, rev_sha)
            revision_live_hits: list[Path] = []
            revision_archive_hits: list[Path] = []
            for rel_path, changed_blob_sha in changed_paths:
                changed_basename = Path(rel_path).name
                b_live = _live_fallback_search(repo_root, changed_basename)
                b_archive = _archive_fallback_search(repo_root, changed_basename)
                if not b_live and not b_archive:
                    commit_time_path = repo_root / rel_path
                    if commit_time_path.is_file():
                        if _is_under_archive_dir(commit_time_path, repo_root):
                            b_archive = [commit_time_path]
                        else:
                            b_live = [commit_time_path]
                    elif changed_blob_sha is None:
                        # `sha` deleted `rel_path` and no basename match
                        # exists anywhere (not moved, not re-added under a
                        # different name) -- distinct from "delivered no
                        # artifact" (never touched anything under these
                        # dirs) and from a plain lookup miss (never resolved
                        # a revision at all): the revision delivered an
                        # artifact that no longer exists.
                        _revision_deleted_unresolvable.append(rel_path)
                for p in b_live:
                    if p not in revision_live_hits:
                        revision_live_hits.append(p)
                for p in b_archive:
                    if p not in revision_archive_hits:
                        revision_archive_hits.append(p)
            revision_total = len(revision_live_hits) + len(revision_archive_hits)
            if revision_total > 0:
                live_hits, archive_hits, total_hits = (
                    revision_live_hits,
                    revision_archive_hits,
                    revision_total,
                )
                if revision_total == 1:
                    only_hit = (revision_live_hits or revision_archive_hits)[0]
                    resolved_display = (
                        rel_id(only_hit, repo_root)
                        if _is_relative(only_hit, repo_root)
                        else str(only_hit)
                    )
                    revision_resolution = {
                        "passed": _original_artifact_path,
                        "resolved": resolved_display,
                    }
            elif _revision_deleted_unresolvable:
                # At least one changed path is a deletion this commit made
                # that never resolved anywhere else (not a move) -- distinct
                # from both "delivered no artifact" and a plain lookup miss.
                pass
            else:
                # Revision resolved but delivered no artifact under either
                # dir set — never reported as "not found at the passed
                # path" (spec item 3): that misdiagnoses a resolved-revision
                # failure as a plain lookup miss.
                _revision_resolved_no_artifact = True
        else:
            # SHA-shaped argument, but not a commit `_resolve_revision`
            # can find in this clone — a sender-side SHA copied into a
            # receiver-side pickup, or simply a typo. Distinct from both
            # revision arms above (those require `rev_sha is not None`)
            # and from a plain lookup miss: the generic message below
            # reports a filename search that never had a chance of
            # succeeding, which misdiagnoses the actual failure the same
            # way `_revision_resolved_no_artifact` was misdiagnosed before
            # spec item 3 fixed it.
            _revision_unresolvable = True

    if total_hits == 0:
        if _revision_deleted_unresolvable:
            raise _ArtifactUnreadable(
                f"{_original_artifact_path}: resolved as revision {_revision_sha_display} "
                f"but the artifact(s) it delivered no longer exist "
                f"({', '.join(repr(p) for p in _revision_deleted_unresolvable)}) "
                f"(basenames tried: {', '.join(repr(b) for b in tried_basenames)})"
            )
        if _revision_resolved_no_artifact:
            raise _ArtifactUnreadable(
                f"{_original_artifact_path}: resolved as revision {_revision_sha_display} "
                f"but that revision delivered no artifact under any of "
                f"{', '.join(LIVE_DIRS + ARCHIVE_DIRS)} "
                f"(basenames tried: {', '.join(repr(b) for b in tried_basenames)})"
            )
        if _revision_unresolvable:
            raise _ArtifactUnreadable(
                f"{_original_artifact_path}: looks like a git revision but does not resolve as "
                f"a commit in {repo_root} — pickup also searched filenames "
                f"(basenames tried: {', '.join(repr(b) for b in tried_basenames)})"
            )
        raise _ArtifactUnreadable(
            f"{artifact_path}: not found at the passed path and not in any of "
            f"{', '.join(LIVE_DIRS + ARCHIVE_DIRS)} "
            f"(basenames tried: {', '.join(repr(b) for b in tried_basenames)})"
        )

    if total_hits > 1:
        # A basename hitting BOTH a live dir and an archive dir is exactly
        # the ambiguity worth surfacing (plausibly a concurrent archival
        # sweep mid-flight) — never resolved by preferring the live hit.
        # `archive_paths` keeps its pre-existing key/shape for the
        # archive-only-multi-hit case (regression coverage:
        # `test_multi_hit_archive_fails_loud_not_first_wins`); `live_paths`
        # is new and additive, always present (possibly empty) so a
        # mixed/live-only multi-hit is equally inspectable.
        live_paths = sorted(rel_id(h, repo_root) for h in live_hits)
        archive_paths = sorted(rel_id(h, repo_root) for h in archive_hits)
        status = "archived" if archive_hits and not live_hits else "multi_hit"
        return _tag({
            "path": artifact_path,
            "classification": "ambiguous",
            "frontmatter": {},
            "resolution": {
                "status": status,
                "live_paths": live_paths,
                "archive_paths": archive_paths,
                "terminal_fields": None,
            },
        })

    if live_hits:
        # A single live hit is an ordinary, actionable pickup — classify it
        # exactly as if the caller had passed this literal path, never as a
        # terminal `archived` record.
        return _tag(_resolve_found_file(live_hits[0], repo_root))

    # 2026-07-26 defect fix (`pickup-assemble apply` commit crash): the
    # single archive-only hit's `artifact.path` MUST be the RESOLVED
    # archive-fallback path, never the raw (possibly bare-basename)
    # `artifact_path` the caller passed in. `_build_archived_resolution`'s
    # `display_path` parameter becomes `apply()`'s `artifact["path"]` ->
    # `artifact_path_value` -> the exact pathspec `_scoped_commit` hands to
    # `git add`/`git commit`; passing the unresolved basename through here
    # reproduced literally on a shared repo root (`git add
    # 2026-07-26-....md failed (rc=128): fatal: pathspec ... did not match
    # any files`) because the file lives under `cross-repo/archive/`, not
    # at the repo root. Mirrors `_resolve_found_file`'s own
    # `display_path` computation (line ~1485) exactly — one resolver, one
    # resolved-path computation, never a second parallel one.
    resolved_archive_path = (
        rel_id(archive_hits[0], repo_root)
        if _is_relative(archive_hits[0], repo_root)
        else str(archive_hits[0])
    )
    return _tag(_build_archived_resolution(resolved_archive_path, archive_hits[0], repo_root))


def _is_relative(p: Path, base: Path) -> bool:
    try:
        p.relative_to(base)
        return True
    except ValueError:
        return False


class _ArtifactUnreadable(Exception):
    """Business failure (exit 1) — artifact absent at path and not archived."""


# ---------------------------------------------------------------------------
# preflight.* (evidence, never a verdict — contract § preflight)
# ---------------------------------------------------------------------------

def compute_staleness(repo_root: Path) -> dict[str, Any]:
    """Step 1.3 — MECHANICAL: fixed-threshold branch-age computation."""
    branch = _current_branch(repo_root)
    days = _branch_age_days(repo_root, branch) if branch else None
    return {"branch": branch, "days": days}


#: A `scope:` entry naming a sibling repo is `<repo-id>:<path>` — real plans
#: write this WITHOUT a space after the colon (`- claude-klabauter:coordinator_core/x.py`),
#: because `- repo: path` with a space parses as a YAML mapping, not the
#: plain string a `scope:` list entry needs; `- repo:path` is the form
#: authors can structurally write. Whitespace after the colon is therefore
#: OPTIONAL here (`\s*`), not mandatory — the prior `\s+` required a shape
#: no real plan ever produces, which left this prefix permanently unmatched
#: and the sibling-repo branch in `compute_tree_quiescence` permanently dead.
#:
#: Windows drive-letter safety does NOT depend on the whitespace requirement:
#: `[A-Za-z][A-Za-z0-9_-]+` requires the repo-id to be TWO OR MORE
#: characters, so a single-letter drive (`C:\Users\...`, `D:/foo`) can never
#: satisfy the first group regardless of what follows the colon — see
#: `test_pickup_assemble.py` for pinned drive-letter regression coverage.
#:
#: URL safety: a `(?!//)` negative lookahead immediately after the colon
#: excludes any `scheme://...` shape (`https://example.com/x` would
#: otherwise satisfy `[A-Za-z][A-Za-z0-9_-]+` — `https` is 5 characters,
#: well past the drive-letter floor). A URL's scheme is always followed by
#: `//`; a real sibling-repo path never is, so this lookahead costs nothing
#: on real scope entries.
#:
#: A second group that itself contains whitespace (a bare path plus trailing
#: prose, e.g. `"claude-klabauter: coordinator/bin/ (apply entrypoint ...)"`)
#: is deliberately still captured here and rejected downstream in
#: `compute_tree_quiescence` — matched-then-rejected, not unmatched, so the
#: two "not a real path" shapes (no prefix at all vs. prefix-plus-prose)
#: funnel through one classification instead of two independent regexes.
_SCOPE_SIBLING_PREFIX_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]+):(?!//)\s*(.+)$")


def _porcelain_dirty_paths(repo_root: Path, paths: list[str]) -> list[str]:
    """`git status --porcelain -- <paths>` scoped to exactly the given
    pathspecs, returning the dirty paths git reports (never the full
    untargeted worktree diff) — pathlib throughout, no manual `/`/`\\`
    splitting, so a Windows sibling repo root and forward-slash-relative
    scope paths resolve the same way `git` itself would join them."""
    if not paths:
        return []
    result = _run_git(["status", "--porcelain", "--", *paths], repo_root)
    if result.returncode != 0:
        return []
    dirty: list[str] = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        entry = line[3:] if len(line) > 3 else line.strip()
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        entry = entry.strip().strip('"')
        if entry:
            dirty.append(entry)
    return dirty


def compute_tree_quiescence(root: Path, scope_entries: list[str]) -> dict[str, Any]:
    """`preflight.tree_quiescence` (AC3/AC8) — replaces the `dirty_paths`
    scope-echo with a real `git status --porcelain` intersection per repo
    named in `scope:`, this repo AND every sibling repo, each sibling
    resolved through the machine-local registry (`repos.<repo-id>`,
    `coordinator_core.machine_resolver.registry_get`) rather than a
    hardcoded path.

    A `scope:` entry that is prose rather than a bare `<repo-id>: <path>`
    pair (or names a repo-id the registry cannot resolve) is
    detect-then-fail-loud into `unparseable_scope_entries` — never silently
    counted as dirty (the defect this function replaces) and never silently
    dropped.

    Negative-spec: does NOT run `git fetch` (AC3's read-only guarantee —
    see module docstring) and does NOT report every dirty file in a repo,
    only the intersection with the paths named in `scope:`.
    """
    local_paths: list[str] = []
    local_unparseable: list[str] = []
    sibling_paths: dict[str, list[str]] = {}

    for entry in scope_entries:
        match = _SCOPE_SIBLING_PREFIX_RE.match(entry)
        if match is None:
            local_paths.append(entry)
            continue
        repo_id, rest = match.group(1), match.group(2).strip()
        if not rest or " " in rest:
            local_unparseable.append(entry)
            continue
        sibling_paths.setdefault(repo_id, []).append(rest)

    dirty_found = False
    local_dirty = _porcelain_dirty_paths(root, local_paths)
    if local_dirty:
        dirty_found = True
    repos: list[dict[str, Any]] = [
        {"repo": ".", "dirty": local_dirty, "unparseable_scope_entries": local_unparseable}
    ]

    for repo_id, rel_paths in sibling_paths.items():
        resolved = registry_get(f"repos.{repo_id.replace('-', '_')}")
        if not resolved:
            repos[0]["unparseable_scope_entries"].extend(f"{repo_id}: {p}" for p in rel_paths)
            continue
        sibling_root = Path(resolved)
        sibling_dirty = _porcelain_dirty_paths(sibling_root, rel_paths)
        if sibling_dirty:
            dirty_found = True
        repos.append({"repo": str(sibling_root), "dirty": sibling_dirty, "unparseable_scope_entries": []})

    return {"verdict": "dirty" if dirty_found else "quiet", "repos": repos}


def compute_coast(
    judgment_points: list[dict[str, Any]],
    claim_grant: Optional[dict[str, Any]] = None,
    tree_quiescence: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """`gates.coast` (AC8) — reports what the EM is holding; never gates the
    claim (that is `gates.claim_grant`'s job — see § The `coast` verdict).

    `verdict` is `"clear"` iff zero unresolved `judgment_points` AND
    `claim_grant.verdict != "denied"`. Every other signal — a dirty tree, a
    `granted-with-warning` claim — appends a human-readable string to
    `notes` and never flips the verdict.
    """
    blocking_jps = [jp for jp in judgment_points if jp.get("id")]
    blocked_by = [jp["id"] for jp in blocking_jps]
    notes: list[str] = []
    verdict = "blocked" if blocked_by else "clear"

    grant = claim_grant or {}
    cg_verdict = grant.get("verdict")
    if cg_verdict == "denied":
        verdict = "blocked"
    elif cg_verdict == "granted-with-warning":
        notes.append("claim granted with warning: " + str(grant.get("reason", "stale claim")))

    if tree_quiescence and tree_quiescence.get("verdict") == "dirty":
        notes.append("tree not quiet: uncommitted changes in scoped paths")

    result: dict[str, Any] = {"verdict": verdict, "notes": notes, "blocked_by": blocked_by}

    # AC8 self-sufficiency (gap-sweep amendment): a caller reading ONLY
    # `gates.coast` must learn WHY it is blocked and what unblocks it,
    # without cross-referencing `judgment_points[]` — so `reason`/`remedy`
    # are derived here from the actual blocking judgment_point(s)' bodies.
    # A handoff branch can carry 2+ simultaneous blocking judgment_points
    # (e.g. liveness `j1` + gate check `jgate`); every blocking point's
    # question/remedy is enumerated below, never just the first.
    if blocking_jps:
        reason_parts: list[str] = []
        remedy_parts: list[str] = []
        for jp in blocking_jps:
            jp_id = jp.get("id", "?")
            question = jp.get("question")
            if question:
                reason_parts.append(f"{jp_id}: {question}")
            unblocking_values = [
                d.get("value") for d in jp.get("dispositions", []) if d.get("resolves") and d.get("value")
            ]
            if unblocking_values:
                remedy_parts.append(f"{jp_id}: resolve via {' or '.join(unblocking_values)}")
        if reason_parts:
            result["reason"] = "; ".join(reason_parts)
        if remedy_parts:
            result["remedy"] = "; ".join(remedy_parts)

    return result


# ---------------------------------------------------------------------------
# Pending-item extraction + closure/deliverable/premise/stealth-skip evidence
# (Step 3.1/3.4a/3.4b/3.4e/3.4f — MECHANICAL per contract § preflight; every
# function below computes candidate evidence and never a keep/drop verdict —
# "does this commit close this item" stays a JUDGMENT entry the EM resolves).
# ---------------------------------------------------------------------------

#: Handoff-body headings whose bullets are candidate pending items (contract
#: dispatch brief, function 1). "Blockers and Issues" is the spelling observed
#: in `templates/handoffs/*.md`; "Blockers or Issues" is tolerated too since
#: the dispatch brief cites it verbatim — read-tolerant on both, never a
#: reason to drop a section's bullets.
_PENDING_SECTION_HEADINGS = frozenset({
    "In-Progress Work",
    "Recommended Next Steps",
    "Blockers or Issues",
    "Blockers and Issues",
    "Task Spine",
})


def _is_table_row(stripped_line: str) -> bool:
    return stripped_line.startswith("|") and stripped_line.endswith("|")


def _is_table_separator_row(stripped_line: str) -> bool:
    inner = stripped_line.strip("|")
    return bool(inner) and all(ch in "-: " for ch in inner)


def _first_table_cell(stripped_line: str) -> str:
    inner = stripped_line.strip("|")
    cells = inner.split("|")
    return cells[0].strip() if cells else ""


def _parse_pending_items(body_text: str) -> list[dict[str, Any]]:
    """Function 1 — MECHANICAL per-bullet candidate extraction.

    Extracts one candidate item per bullet under `## In-Progress Work`,
    `## Recommended Next Steps`, and `## Blockers or Issues` (tolerating the
    `Blockers and Issues` template spelling too), plus one candidate item per
    non-header row of a `## Task Spine` table when present. Deliberately does
    NOT decide whether an extracted bullet is genuinely still-pending prose —
    that semantic call stays the EM's (contract § MIXED reclassification,
    "Overall keep/drop verdict per pending item" is JUDGMENT).
    """
    items: list[dict[str, Any]] = []
    current_heading: Optional[str] = None
    in_target_section = False

    for line in body_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            current_heading = stripped[3:].strip()
            in_target_section = current_heading in _PENDING_SECTION_HEADINGS
            continue
        if stripped.startswith("# ") and not stripped.startswith("## "):
            in_target_section = False
            continue
        if not in_target_section or current_heading is None:
            continue

        if current_heading == "Task Spine":
            if _is_table_row(stripped) and not _is_table_separator_row(stripped):
                cell = _first_table_cell(stripped)
                if cell and cell.lower() not in ("id", "task", "description", "item"):
                    items.append({"text": cell, "source_section": current_heading})
            continue

        if stripped.startswith(("- ", "* ")):
            text = stripped[2:].strip()
            if text:
                items.append({"text": text, "source_section": current_heading})

    return items


#: Small stopword list for `_key_nouns` — filters connective prose that would
#: otherwise generate meaningless commit-subject overlap. Not a general NLP
#: tool; sufficient for the noun-overlap heuristic named in the dispatch brief.
_STOPWORDS = frozenset({
    "the", "and", "or", "to", "of", "for", "in", "on", "with", "this", "that",
    "from", "is", "are", "was", "were", "be", "been", "it", "its", "as", "at",
    "by", "into", "not", "still", "need", "needs", "then", "than", "also",
    "once", "when", "will", "should", "would", "could", "does", "done",
})

_CITED_PATH_RE = re.compile(r"[\w./-]+\.(?:md|py|sh)\b")


def _key_nouns(text: str) -> list[str]:
    """Lowercased, deduped, stopword-filtered word candidates (>=4 chars) —
    the noun-overlap heuristic function 2 scans commit subjects against."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", text)
    seen: list[str] = []
    for word in words:
        lowered = word.lower()
        if lowered in _STOPWORDS or lowered in seen:
            continue
        seen.append(lowered)
    return seen


def _extract_cited_path(text: str) -> Optional[str]:
    """First-match-only (contract § "ls/Read cited paths" is exhaustive in
    prose; this extraction is not — a bullet citing two paths surfaces only
    the first as a premise). Judged out of scope for this POC (Finding 12);
    full multi-path extraction is a future enhancement, not a defect."""
    match = _CITED_PATH_RE.search(text)
    return match.group(0) if match else None


def _extract_plan_status(file_text: str) -> Optional[str]:
    """Read a plan/stub's body-level `**Status:** ...` line (NOT frontmatter —
    plan status lives in prose per `coordinator/agents/executor.md § Plan-body
    immutability` disambiguation; this is a read, never a write, of that
    convention)."""
    match = re.search(r"^\*\*Status:?\*\*\s*(.*)$", file_text, re.MULTILINE)
    return match.group(1).strip() if match else None


def _git_log_oneline(repo_root: Path, args: list[str]) -> list[tuple[str, str]]:
    result = _run_git(["log", "--oneline", *args], repo_root)
    if result.returncode != 0:
        return []
    pairs: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        sha, _, subject = line.partition(" ")
        pairs.append((sha, subject))
    return pairs


def compute_closure_signals(
    repo_root: Path, since_date: str, pending_items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Function 2 — MECHANICAL candidate-closing-commit evidence per pending
    item (Step 3.4a/b). Never a verdict: "does this commit close this item"
    is the JUDGMENT entry (contract § JUDGMENT checklist, row 1) this evidence
    feeds — this function only narrows the search space.
    """
    subjects = _git_log_oneline(repo_root, [f"--since={since_date}"])
    signals: list[dict[str, Any]] = []

    for item in pending_items:
        text = item.get("text", "")
        nouns = _key_nouns(text)
        candidates = [
            {"sha": sha, "subject": subject}
            for sha, subject in subjects
            if nouns and any(noun in subject.lower() for noun in nouns)
        ]
        entry: dict[str, Any] = {"item_text": text, "candidate_commits": candidates}

        cited_path = _extract_cited_path(text)
        if cited_path:
            entry["cited_path"] = cited_path
            file_path = repo_root / cited_path
            plan_status = None
            if file_path.is_file():
                plan_status = _extract_plan_status(
                    file_path.read_text(encoding="utf-8", errors="replace")
                )
            entry["plan_status"] = plan_status
            entry["plan_chunk_commits"] = [
                {"sha": sha, "subject": subject}
                for sha, subject in _git_log_oneline(repo_root, ["--", cited_path])
                if re.match(r"^[\w][\w.\-]*:\s", subject)
            ]

        signals.append(entry)

    return signals


def compute_deliverable_evidence(
    repo_root: Path, scope_paths: list[str], since_date: str
) -> list[dict[str, Any]]:
    """Function 3 — MECHANICAL deliverable-scope evidence (Step 3.4b(iii)).

    present-on-disk AND commit-referenced -> "strong" shipped signal;
    present-without-commit -> "weak"; absent with no deleting commit in
    range -> "not-shipped"; absent AND a commit in `commits` deleted the
    path -> "deleted-shipped" (a scope path whose deliverable IS its own
    removal — a de-bash wave, a port-out, a consolidation — reads its own
    successful outcome as positive evidence, not as "not-shipped"; kept
    distinct from "strong" so a consumer can still tell presence-shipped
    from deletion-shipped). A signal string, never a shipped/not-shipped
    verdict — the EM weighs it alongside closure_signals and candidate
    commits (contract § "Overall keep/drop verdict per pending item").

    One bounded commit walk resolves HEAD once and tests every scope path
    per popped commit, instead of calling `_git_log_oneline` once per path
    (each call re-entering `_walk_commits` from a freshly-resolved HEAD).
    Results are appended per path in walk order, exactly as the old
    per-path walks produced them. This also closes a live correctness gap:
    resolving HEAD separately per path let two scope paths be evaluated
    against different tips on a branch with concurrent committers,
    producing one evidence object internally inconsistent about what
    history it describes.

    This walk-sharing restructure is order- and content-preserving on its
    own. It is bundled with one other change that is NOT purely a
    refactor: `_walk_commits_since` replaces the old `--since` handling's
    plain `if ts < since_epoch: break` with a slop-windowed walk, because
    committer dates are not monotonic along parent edges and a plain
    `break` could silently truncate results (see `_walk_commits_since`'s
    docstring). That is a bug fix, not a no-op — it will change this
    function's `commits` list for any artifact whose history hits the
    clock-skew/rebase pattern the slop window exists to catch. "Output
    unchanged" therefore holds only for content this repo's git history
    doesn't exercise the SLOP window on (verified empirically: 0/37 corpus
    diffs on `state/handoffs/`, consistent with this repo having no
    clock-skew edges in its history — not proof none exist elsewhere).
    A future corpus-diff run against a different repo's history that shows
    a divergence must be attributed to one of these three changes before
    being called a pass or a regression — do not assume "unchanged" and
    revert the SLOP fix (or the deletion-awareness fix below) on a diff
    that is actually one of them working correctly. The third: absent
    scope paths whose own `commits` list contains a commit that deleted
    the path now classify as "deleted-shipped" instead of "not-shipped"
    (see the signal-value docstring above) — a corpus diff against a repo
    with any deletion-shaped deliverable in its scope lists will show this
    as an expected divergence, not a regression.
    """
    per_path_commits: dict[str, list[dict[str, Any]]] = {path: [] for path in scope_paths}
    deleted_paths: set[str] = set()
    try:
        discovered = _discover_git_dirs(repo_root)
        if discovered is not None:
            _root, dirs = discovered
            common_dir = dirs.common_dir
            head_sha = _resolve_revision(dirs, "HEAD")
            since_epoch = _parse_since_date(since_date)
            if head_sha is not None:
                for sha, commit in _walk_commits_since(common_dir, head_sha, since_epoch):
                    for path in scope_paths:
                        if _commit_touches_path(common_dir, sha, commit, path):
                            per_path_commits[path].append({"sha": sha, "subject": commit["subject"]})
                            if _commit_deletes_path(common_dir, sha, commit, path):
                                deleted_paths.add(path)
    except (
        _GitReadModelError,
        OSError,
        zlib.error,
        struct.error,
        UnicodeDecodeError,
        IndexError,
        ValueError,
        RecursionError,
    ):
        per_path_commits = {path: [] for path in scope_paths}
        deleted_paths = set()

    evidence: list[dict[str, Any]] = []
    for path in scope_paths:
        exists = (repo_root / path).exists()
        commits = per_path_commits[path]
        if exists and commits:
            signal = "strong"
        elif exists:
            signal = "weak"
        elif path in deleted_paths:
            signal = "deleted-shipped"
        else:
            signal = "not-shipped"
        evidence.append({"path": path, "exists": exists, "commits": commits, "signal": signal})
    return evidence


#: Subtrees that can never legitimately hold a committed repo artifact a
#: handoff premise would cite — VCS internals, bytecode caches, and
#: ephemeral scratch — pruned from the premise-witness walk below
#: (2026-08-13 hot-path-over-acquisition fix). Deliberately NOT a
#: narrower "known artifact roots" allowlist (state/docs/cross-repo/...):
#: a path premise can cite ANY tracked file, including source under
#: `coordinator_core/`, so excluding by what a directory categorically
#: cannot hold is the only narrowing that cannot turn a real hit into a
#: miss.
#:
#: This is an empirical claim about THIS repo's tracked corpus, not a
#: categorical one about the directory name — `dist` was removed from
#: this set (2026-08-13) after it was found to hold 33 tracked files
#: here, which is exactly the false-negative failure mode this set
#: exists to avoid. Before adding any name to this set, verify it holds
#: zero tracked files by running `git ls-files <dirname>` and confirming
#: empty output — a directory name being conventionally build-output is
#: not sufficient; only zero tracked files under it, verified against
#: this repo, is. Re-verify periodically, since a name safe today can
#: gain tracked content later.
#:
#: Measured against this repo's tree (2026-08-13, after removing `dist`):
#: full unpruned walk 86,058 entries; pruned walk 25,225 entries — a
#: 3.41:1 reduction, `.git/` alone accounting for 45,844 of the excluded
#: entries.
_PREMISE_WALK_PRUNE_DIRNAMES = frozenset({
    ".git", "__pycache__", "build", "scratch", "scratchpad",
})


def _premise_walk_prune(dirname: str) -> bool:
    """True when `dirname` (a bare directory name, not a path) must never be
    descended into by `_search_repo_for_basename` — see
    `_PREMISE_WALK_PRUNE_DIRNAMES` for what and why.

    The `.egg-info` check is a deliberate suffix match, not an exact/pattern
    match — it prunes any dirname ending in the literal `.egg-info`
    (`foo.egg-info`, `x.egg-info`), which is safe because nothing tracked is
    ever named that way; a name that merely contains but doesn't end with
    `.egg-info` (e.g. `notegg-infowhatever`) is not pruned."""
    return dirname in _PREMISE_WALK_PRUNE_DIRNAMES or dirname.endswith(".egg-info")


def _search_repo_for_basename(repo_root: Path, basename: str) -> list[Path]:
    """Repo-wide basename search for `compute_premise_checks`' path-premise
    miss arm, narrowed to prune non-artifact subtrees (`_premise_walk_prune`)
    rather than capped — see that helper's docstring for the ratio this
    earns and why an allowlist of artifact roots would risk a correctness
    regression here. `os.walk` (not `Path.rglob`) so pruning happens via
    `dirnames[:]` BEFORE descent, not after a wasted enumeration."""
    hits: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if not _premise_walk_prune(d)]
        if basename in filenames:
            hits.append(Path(dirpath) / basename)
    return hits


def compute_premise_checks(repo_root: Path, premises: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Function 4 — MECHANICAL premise-witness evidence (Step 3.4e).

    `premises` is a list of `{"type": "path" | "sha" | "pathspec", "value": str}`.
    Path premises: `is_file`/`is_dir` first; on a miss, a pruned repo-wide
    walk for the basename BEFORE concluding absent (a single failed direct
    check is not "absent" — contract dispatch brief); see
    `_search_repo_for_basename` for what is pruned and why. SHA premises:
    `git cat-file -e` + `git branch --contains`. Pathspec premises: glob;
    empty = surface.
    """
    results: list[dict[str, Any]] = []
    for premise in premises:
        ptype = premise.get("type")
        value = premise.get("value", "")
        entry: dict[str, Any] = {"type": ptype, "value": value}

        if ptype == "path":
            candidate = Path(value)
            full = candidate if candidate.is_absolute() else (repo_root / value)
            if full.is_file() or full.is_dir():
                entry["witness"] = "present"
            else:
                basename = Path(value).name
                hits = _search_repo_for_basename(repo_root, basename) if basename else []
                rel_hits = [rel_id(h, repo_root) for h in hits if _is_relative(h, repo_root)]
                entry["found_elsewhere"] = rel_hits
                entry["witness"] = "found-elsewhere" if rel_hits else "absent"

        elif ptype == "sha":
            cat = _run_git(["cat-file", "-e", value], repo_root)
            if cat.returncode == 0:
                branches_result = _run_git(["branch", "--contains", value], repo_root)
                branches = (
                    [b.strip().lstrip("* ").strip() for b in branches_result.stdout.splitlines() if b.strip()]
                    if branches_result.returncode == 0
                    else []
                )
                entry["contains_branches"] = branches
                entry["witness"] = "present"
            else:
                entry["witness"] = "absent"

        elif ptype == "pathspec":
            matches = [
                rel_id(m, repo_root)
                for m in repo_root.glob(value)
                if _is_relative(m, repo_root)
            ]
            entry["matches"] = matches
            entry["witness"] = "matched" if matches else "empty-surface"

        else:
            entry["witness"] = "unknown-premise-type"

        results.append(entry)

    return results


#: Function 5 — a `shipped_in:` value is trusted only when it is a resolvable
#: git SHA or the sanctioned `substantively-shipped-no-commit:<date>` token.
#: `_SHIPPED_SHA_RE`/`_SHIPPED_NO_COMMIT_RE` are re-exported imports of
#: `coordinator_core.shipped_in_tokens`'s `_SHA_HEX_RE`/`_NO_COMMIT_TOKEN_RE`
#: — this module previously carried its own drifting copy of the grammar (a
#: looser `\S+`-suffixed no-commit token, no upper hex-length bound, no
#: uppercase-hex acceptance); DR-096 makes `shipped_in_tokens` the single
#: choke point for this shape (moved out of `archive_stamp` per
#: state/debt-backlog/DSR-2026-08-13-archive-stamp-import-order-drops-an-op-
#: from-the-registry.yaml to break an import cycle), so the copy is gone and
#: this is an import, not a redefinition.


def compute_stealth_skip_flags(pending_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Function 5 — MECHANICAL pattern match, never a verdict on whether the
    item is genuinely done. Flags any item carrying a `shipped_in` value that
    is neither shape — the item stays "still-pending" evidence for the EM,
    per `pickup/SKILL.md`'s stealth-skip-detection note (Step 1)."""
    flags: list[dict[str, Any]] = []
    for item in pending_items:
        shipped_in = item.get("shipped_in")
        if shipped_in is None:
            continue
        value = str(shipped_in).strip()
        if _SHIPPED_SHA_RE.fullmatch(value) or _SHIPPED_NO_COMMIT_RE.fullmatch(value):
            continue
        flags.append({"item_text": item.get("text"), "shipped_in": value, "flag": "stealth-skip-suspect"})
    return flags


#: `shipped_in: <value>` inline in a pending-item bullet (e.g. "- foo bar
#: (shipped_in: deadbeef)") — MECHANICAL pattern extraction so
#: `compute_stealth_skip_flags` (function 5) has real per-item input to flag
#: against, mirroring the frontmatter-level `shipped_in:` shape it already
#: pattern-matches (contract § Step 3.4f).
_SHIPPED_IN_INLINE_RE = re.compile(r"shipped_in:\s*([^\s)]+)")

_SHA_TOKEN_RE = re.compile(r"\b[0-9a-f]{7,40}\b")


def _augment_pending_items_with_shipped_in(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Non-mutating: returns new dicts carrying `shipped_in` when the bullet
    text embeds one inline, so `compute_stealth_skip_flags` has real input
    rather than permanently seeing "no field present -> nothing to flag"."""
    augmented: list[dict[str, Any]] = []
    for item in items:
        match = _SHIPPED_IN_INLINE_RE.search(item.get("text", ""))
        if match:
            augmented.append({**item, "shipped_in": match.group(1)})
        else:
            augmented.append(item)
    return augmented


def _premises_from_pending_items(pending_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Function 4 input assembly — MECHANICAL: a `path` premise per pending-
    item bullet's FIRST cited `.md`/`.py`/`.sh` path (`_extract_cited_path`
    is first-match-only, per Finding 12 — a bullet citing two paths surfaces
    only one premise, not one per path), and a `sha` premise per 7-40 hex
    token in the bullet text (contract § Step 3.4e: "ls/Read cited paths,
    cat-file -e + branch --contains SHAs"). Never invents a premise the
    bullet text doesn't literally carry."""
    premises: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in pending_items:
        text = item.get("text", "")
        cited_path = _extract_cited_path(text)
        if cited_path and ("path", cited_path) not in seen:
            premises.append({"type": "path", "value": cited_path})
            seen.add(("path", cited_path))
        for sha_match in _SHA_TOKEN_RE.finditer(text):
            sha = sha_match.group(0)
            if ("sha", sha) not in seen:
                premises.append({"type": "sha", "value": sha})
                seen.add(("sha", sha))
    return premises


def _artifact_since_date(artifact_path: str) -> str:
    """Best-effort "evidence window start" for the git-log-since-date scans
    (Step 3.4a/b) — the handoff's own filename date prefix
    (`state/handoffs/YYYY-MM-DD_HHMMSS_slug.md`), falling back to the epoch
    when the basename doesn't carry one (never fabricates a date)."""
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", Path(artifact_path).name)
    return match.group(1) if match else "1970-01-01"


def compute_handoff_preflight(
    root: Path, artifact: dict[str, Any], fm: dict[str, Any], scope_paths: list[str]
) -> dict[str, Any]:
    """Wires Function 1-5 (`_parse_pending_items` + `compute_closure_signals`
    / `compute_deliverable_evidence` / `compute_premise_checks` /
    `compute_stealth_skip_flags`) into one `preflight` reconcile-evidence
    bundle for the handoff branch (contract § preflight; dispatch brief
    defect 1). Reads the live artifact body once; every sub-field is real
    computed evidence, never a hardcoded `[]` when a body exists to analyze.

    `prereq_reverify` has no backing MECHANICAL function in this module yet
    (Step 3.4g "prereq-table command re-run" is unimplemented) — the key is
    present (never silently omitted) with an honest empty list rather than
    fabricated content.
    """
    live_path = root / artifact["path"]
    if not live_path.is_file():
        return {
            "closure_signals": [],
            "deliverable_evidence": [],
            "premise_checks": [],
            "stealth_skip_flags": [],
            "prereq_reverify": [],
        }

    text = live_path.read_text(encoding="utf-8", errors="replace")
    split = split_frontmatter(text)
    body_text = split.body_with_leading_newline if split is not None else text
    pending_items = _augment_pending_items_with_shipped_in(_parse_pending_items(body_text))
    since_date = _artifact_since_date(artifact["path"])

    closure_signals = compute_closure_signals(root, since_date, pending_items) if pending_items else []
    deliverable_evidence = compute_deliverable_evidence(root, scope_paths, since_date) if scope_paths else []
    premises = _premises_from_pending_items(pending_items)
    premise_checks = compute_premise_checks(root, premises) if premises else []
    stealth_skip_flags = compute_stealth_skip_flags(pending_items)

    return {
        "closure_signals": closure_signals,
        "deliverable_evidence": deliverable_evidence,
        "premise_checks": premise_checks,
        "stealth_skip_flags": stealth_skip_flags,
        "prereq_reverify": [],
    }


# ---------------------------------------------------------------------------
# gates.* (deterministic facts only — contract § gates)
# ---------------------------------------------------------------------------

def compute_gate_notes(fm: dict[str, Any]) -> dict[str, Any]:
    """`gates.gate_notes` — the PM's third pickup-brief requirement: an EM
    must be able to answer "have we passed this gate?" from the brief alone,
    without opening the file (AC8, 2026-08-19-gate-notes-are-advisory-
    blocked-by-derives-readiness).

    Reads ONLY this record's own `blocking_notes` frontmatter field (no
    corpus walk, no graph read) and reports it verbatim. `passed` is
    deliberately always `null` — nothing on the graph can clear a gate note,
    so this function must not pretend to adjudicate one; it is a prompt to
    the reading EM, not a verdict. This shape MUST NOT enter any
    pickup-blocking computation (`coast`, `claim`, `aging_verdict`) —
    advisory means advisory.
    """
    text = fm.get("blocking_notes")
    return {"present": bool(text), "text": text if text else None, "passed": None}


def compute_branch_gate(
    repo_root: Path, *, classification: Optional[str] = None
) -> dict[str, Any]:
    """Step 1.2 — MECHANICAL: resume vs. create vs. already-current.

    `classification` discriminates the memo disposition path from
    handoff/spinoff: `apply` on a memo stamps frontmatter and commits on
    whatever branch it's invoked from — the engine never creates or resumes
    a work branch for it. Reporting `create` there (the handoff/spinoff
    default) prescribes an action nobody takes, on every memo pickup, which
    trains the reader to stop trusting the field. `in_place` is reported
    rather than folded into `unknown`/`not_applicable` because
    `current_branch` stays load-bearing here — the EM still needs to see
    they're sitting on `main` or a shared branch before frontmatter mutates.

    The concurrent-peer case is the harder version of the same problem:
    this repo routinely runs 4-5 concurrent EM sessions in ONE shared
    working tree (project CLAUDE.md § Coordinator Operating Doctrine).
    Reporting `create` while a live peer shares this checkout doesn't just
    prescribe an action nobody takes — it prescribes an action that BREAKS
    peers, by switching the shared checkout out from under every live
    session's in-progress work. So whenever the mechanical branch-name
    check alone would answer `create` (never for `memo`'s `in_place` path,
    never for `unknown`, never for a `work/` `resume`), this function asks
    `coordinator_core.session.worktree_safety.branch_mutation_verdict`
    whether any other live session shares this worktree right now, and
    downgrades to `in_place` — naming the peers and what branch each is
    on — rather than green-lighting the cut. This is advisory, not a
    refusal: the function still always returns a plain dict, never raises,
    and never blocks the caller from proceeding.

    `unknown` liveness (identity or live-set unresolvable) is NOT degraded
    to `create` — it maps to the same cautious `in_place` answer as an
    affirmatively-observed peer, with a `reason` saying the live set was
    indeterminate. "Fail closed" here means the CAUTIOUS branch answer:
    reporting `create` on an indeterminate read risks prescribing exactly
    the peer-breaking action this exists to prevent.
    """
    branch = _current_branch(repo_root)
    if branch is None:
        return {"action": "unknown", "current_branch": None}
    if classification == "memo":
        return {
            "action": "in_place",
            "current_branch": branch,
            "reason": (
                "memo disposition stamps frontmatter and commits on the "
                "current branch; the engine creates no work branch"
            ),
        }
    if branch.startswith("work/"):
        return {"action": "resume", "current_branch": branch}

    # UNQUALIFIED_BRANCH_CUT, not FRESH_CUT_AT_HEAD: this function PRESCRIBES
    # a cut to a caller that controls how it is performed, so it cannot
    # establish content-neutrality from its own inputs and must not inherit
    # the boot path's narrowing.
    verdict = _worktree_safety.branch_mutation_verdict(
        str(repo_root), operation=_worktree_safety.UNQUALIFIED_BRANCH_CUT
    )
    if verdict.outcome == "ok":
        return {"action": "create", "current_branch": branch}
    return {
        "action": "in_place",
        "current_branch": branch,
        "reason": (
            f"cutting a new branch would switch the shared worktree under "
            f"live peer session(s): {verdict.reason}"
        ),
    }


def compute_aging_verdict(handoff_path: Path) -> str:
    """Step 3.4d — MECHANICAL: the existing `handoff-gate-aging` predicate,
    consumed in-process (AC16) rather than re-derived.

    Deliberately retained even though workday-start.md's standalone batch
    invocation of the same predicate was retired 2026-07-27 (docs/plans/
    2026-07-26-gate-resolution-widen-and-migrate.md § C16): this call site
    feeds `gates.gate_check.aging_verdict`, evidence for the `jgate`
    judgment point ("Has this awaiting_gate handoff's gate actually
    cleared?") that is asked for EVERY `awaiting_gate` handoff on every
    `/pickup`, regardless of what this predicate says. Raw calendar age is
    supplementary color for a judgment call that already happens
    unconditionally — not a second surfacing mechanism competing with
    `coordinator_core.reconcile.gate_eval`'s own continuous surfacing,
    which is what made the workday-start.md batch nag redundant."""
    stdout_line, rc = _gate_aging_check_one(handoff_path, date.today())
    if rc == 2:
        return "parse_error"
    return "stale" if stdout_line else "ok"


def compute_claim_gate(repo_root: Path, class_: str, basename: str) -> dict[str, Any]:
    """Pre-mutation gates 1+2 (Step 5) — read-only dual-read of the claim dir.

    This module never fetches or writes the claim dir (READ-ONLY, AC3) — it
    reports the CURRENT local claim state; the freshness gap between this read
    and dispatch time is covered by `apply` re-resolving `compute_claim_grant`
    fresh immediately before mutating (`apply.py:_resolve_claim_grant`), never
    by the liveness judgment point — that point no longer carries
    `revalidate_at_dispatch` (chunk C7 Part A4; see `build_liveness_judgment_
    point`'s docstring).
    """
    claims_dir = repo_root / ".git" / "coordinator-sessions" / f"{class_}-claims" / basename
    if not claims_dir.is_dir():
        # "ok" never appears from this module — this is a READ-ONLY producer
        # (AC3, no `git fetch`); it's reserved for a future fetch-capable
        # producer per the contract's illustrative example (DoE-claude
        # commit c12825a5).
        return {"fetch_state": "not_performed", "holder": None}
    try:
        holder_live = _liveness.claim_holder_live(str(claims_dir), str(repo_root))
    except (OSError, ValueError):
        holder_live = False
    holder_sid = None
    sid_file = claims_dir / "session_id"
    if sid_file.is_file():
        try:
            holder_sid = sid_file.read_text(encoding="utf-8").strip() or None
        except OSError:
            holder_sid = None
    return {
        "fetch_state": "not_performed",
        "holder": holder_sid if holder_live else None,
    }


#: Claim-staleness settling window (AC3b-i) — deliberately a SEPARATE named
#: constant from `coordinator_core.session.liveness`'s own 30-minute
#: recency window (`_liveness._THIRTY_MIN`), even though the two share a
#: magnitude today. They answer different questions: liveness asks "is this
#: session alive?"; this constant asks "has a dead holder been gone long
#: enough that taking over is safe?" Aliasing one to the other would couple
#: two independently-tunable decisions; a test instead asserts
#: `CLAIM_STALE_AFTER_MINUTES >= <the liveness window, in minutes>` so a
#: future tuning of either cannot silently drift them apart. Env override:
#: COORDINATOR_CLAIM_STALE_AFTER_MINUTES.
CLAIM_STALE_AFTER_MINUTES = int(
    os.environ.get("COORDINATOR_CLAIM_STALE_AFTER_MINUTES", "30")
)


def _claim_age_minutes(claims_dir: Path) -> Optional[int]:
    """Minutes elapsed since ``claims_dir``'s ``claimed_at`` file (written by
    ``session.claims._write_claim_meta`` at claim time, ISO-8601 UTC,
    second resolution). Returns ``None`` when the file is missing or its
    content fails to parse — an unreadable claim timestamp is an evidence
    gap, never fabricated as fresh (age 0) or stale."""
    claimed_at_file = claims_dir / "claimed_at"
    if not claimed_at_file.is_file():
        return None
    try:
        raw = claimed_at_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    claimed_epoch = _session_core.iso_to_epoch(raw)
    if claimed_epoch <= 0:
        return None
    elapsed = _session_core.now_epoch() - claimed_epoch
    if elapsed < 0:
        elapsed = 0
    return elapsed // 60


def _claim_grant_denied_live_reason(
    holder_sid: Optional[str], evidence: dict[str, Any]
) -> str:
    """Compose the AC3b row-3 `denied` reason from whatever `holder_evidence()`
    fields resolved — a decidable one-liner instead of the former bare
    `"held by <sid> — that session is live"` (the unfalsifiable-evidence
    incident this module exists to close: a bare boolean plus a stale-
    looking claim age gets overridden by reflex). Omits any clause whose
    underlying field is `None` rather than printing "None" — evidence
    gaps are silent, not fabricated. Takes an already-resolved `evidence`
    dict (rather than re-resolving it) so the caller's single
    `holder_evidence()` call — already merged into the returned grant dict
    — is not duplicated into a second transcript read."""
    basis = evidence.get("liveness_basis")
    age_sec = evidence.get("last_activity_age_sec")
    recent_paths = evidence.get("recent_paths") or []
    scope_overlap = evidence.get("scope_overlap")

    # A live claim dir CAN name no holder — `claim_holder_live` falls back to
    # the ephemeral-pid test for a legacy dir carrying no `session_id` file,
    # and a concurrent takeover can remove that file between two reads.
    # Rendering the gap beats printing a literal "None" into a reason line an
    # EM is meant to act on.
    holder_display = holder_sid or "an unidentified session"

    if basis in ("stable-pid", "harness-registry"):
        header = f"held by {holder_display} — live ({basis})"
        clauses = []
        if age_sec is not None:
            clauses.append(f"last activity {age_sec}s ago")
        if recent_paths:
            clauses.append(f"last touched {recent_paths[0]}")
            if scope_overlap is True:
                clauses.append("intersects this handoff's scope")
            elif scope_overlap is False:
                clauses.append("does not intersect this handoff's scope")
        if clauses:
            return header + ", " + ", ".join(clauses)
        return header

    # Weak or unknown basis — hedge explicitly rather than asserting liveness
    # with a confidence the evidence doesn't support.
    basis_note = "recency-window only" if basis == "recency-window" else "basis unknown"
    inner_clauses = []
    if age_sec is not None:
        minutes = age_sec // 60
        inner_clauses.append(f"last activity {minutes}m ago")
    if recent_paths:
        inner_clauses.append(f"last touched {recent_paths[0]}")
    else:
        inner_clauses.append("no recent file activity found")
    inner = ", ".join(inner_clauses)
    return f"held by {holder_display} — live ({basis_note}: {inner}) — may be a stale claim"


def compute_claim_grant(
    repo_root: Path,
    class_: str,
    basename: str,
    artifact_path: str,
    cwd: Optional[str] = None,
    fm: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """`gates.claim_grant` — the resolved verdict a `/pickup <path>` claim
    attempt answers (contract § "`/pickup <path>` is a claim attempt").
    READ-ONLY (AC2/AC3): reads the claim dir and consults
    `coordinator_core.session.liveness` (AC16); never fetches, writes, or
    takes over a claim — mutation is `directives[]`' job, executed only by
    `apply` (C2b), which re-resolves this same verdict immediately before
    mutating rather than trusting a brief-time snapshot.

    R4 claim rule (ratified, replacing the former age-keyed settling-window
    split — docs/plans/2026-08-21-rebuild-the-three-ceremony-assemblers.md
    C2): resolved in this order:
      1. No claimant (no claim dir at all, or a claim dir with no recorded
         `session_id`) -> `granted`.
      2. A claimant is recorded and IS this session -> `granted`,
         `held_by_self: True`, narrated as "you already hold this" (the Director of Engineering
         review, F2) — without this row, re-typing `/pickup <path>` after a
         `hold` verdict or a compaction would resolve to `denied: held by a
         live peer` naming the EM's OWN session, the amnesiac-facing
         contradiction the contract's "Speaking to an amnesiac" section
         exists to prevent. Identity, not part of the R4 outcome table
         itself — a session can never be in contention with itself.
      2b. Holder is a DIFFERENT session holding an EXPIRED `brief`-stage
         lease (`session.claims.brief_lease_expired`) -> `granted-with-
         warning`, EVEN IF that session reads live. Untouched by the R4
         rewrite: it answers a different question than claim contention
         does (a fixed lease duration on an unappliеd reservation, not a
         liveness read), and `session_live`'s Layer 1 is PPID-authoritative
         and ignores recency, so a session that briefed the artifact and
         walked away without exiting would otherwise hold it forever. An
         `apply`-stage claim never reaches this row.
      3. A DIFFERENT claimant is recorded and that session is LIVE (resolved
         via `coordinator_core.session.liveness.claim_holder_live`, which
         keys on `coordinator_core.session.core.stable_pid_alive` against
         the holder's `meta.json` — not the harness registry's raw pid or
         status field) -> `denied` (REJECT), UNLESS (AC3e) that holder is
         lineage-related to this artifact via `fm`
         (`_lineage_related_sessions` — this artifact's own author, or a
         predecessor artifact's live claimant) — then it is a HANDOVER, not
         contention, and resolves `granted` instead. Untouched by the R4
         rewrite for the same reason as 2b: it is an identity/relationship
         question, not an age proxy.
      4. A DIFFERENT claimant is recorded and that session is NOT live, OR
         liveness could not be resolved -> `granted-with-warning`
         (GRANT_WITH_WARN). Not-live and unresolvable collapse to the SAME
         outcome — an evidence gap is never treated as proof of liveness,
         and (this is the R4 change) neither is claim age: the former
         table's separate settling-window `denied` cell for a *recently*
         not-live claimant is gone. `claimed_at`/`claim_age_minutes` is
         never read to reach this row.

    Age is NOT an input to the row-4 decision and `claimed_at` is never read
    for it: the prior table's settling-window/staleness split (a live
    claimant treated as takeable once its claim was old enough; a not-live
    claimant's takeability keyed on how long it had been not-live) granted a
    baton away from a demonstrably running session, which the PM killed.
    Liveness is decidable on its own; age was a proxy that could disagree
    with the thing it proxied.

    `unclean_prior_holder` (bool, always present) — the fact pickup/SKILL.md's
    "recovery banner" step expects to read (contract § "Classify, Load,
    Reconcile -> Report briefly"): "a recovery means the prior session died
    uncleanly, so verify on-disk state against the body before resuming."
    `True` ONLY on row 4 (a not-live/unresolvable claimant is being granted
    over) — `session.claims`' `drop()` always `shutil.rmtree`s the whole
    claims dir on a clean release, so a claims dir that still exists and
    names a not-live holder can only mean the holder went away without
    releasing it. Every other row is `False`.

    `claim_age_minutes` is retained on the returned dict for shape stability
    with older callers but is no longer used to resolve `verdict` — it is
    always `None` here since age is not read as part of this decision.
    `holder_evidence()`-derived narration fields (`liveness_basis`,
    `last_activity_age_sec`, `recent_paths`, `scope_overlap`, ...) are no
    longer merged into this dict (F6, see the chunk's completion report):
    this rewrite implements the literal ratified R4 read for the
    liveness/contention question — dropping `holder_evidence()` entirely
    rather than keeping it for narration-only enrichment. `claim_stage` is
    still resolved and returned (needed by row 2b above).
    """
    cwd_str = cwd if cwd is not None else str(repo_root)
    drop_invocation = f"pickup-assemble drop {artifact_path}"
    claims_dir = repo_root / ".git" / "coordinator-sessions" / f"{class_}-claims" / basename

    def _no_claimant() -> dict[str, Any]:
        return {
            "verdict": "granted",
            "reason": "no competing claim",
            "holder": None,
            "holder_live": False,
            "held_by_self": False,
            "claim_age_minutes": None,
            "claim_stage": None,
            "drop_invocation": drop_invocation,
            "unclean_prior_holder": False,
        }

    if not claims_dir.is_dir():
        return _no_claimant()

    holder_sid = None
    sid_file = claims_dir / "session_id"
    if sid_file.is_file():
        try:
            holder_sid = sid_file.read_text(encoding="utf-8").strip() or None
        except OSError:
            holder_sid = None

    if not holder_sid:
        return _no_claimant()

    fm = fm or {}
    stage = _claims.claim_stage(claims_dir)

    try:
        self_holder = _liveness.claim_held_by_me(str(claims_dir), cwd=cwd_str)
    except (OSError, ValueError):
        self_holder = False

    if self_holder:
        return {
            "verdict": "granted",
            "reason": "you already hold this",
            "holder": holder_sid,
            "holder_live": True,
            "held_by_self": True,
            "claim_age_minutes": None,
            "claim_stage": stage,
            "drop_invocation": drop_invocation,
            "unclean_prior_holder": False,
        }

    if stage == _claims.CLAIM_STAGE_BRIEF and _claims.brief_lease_expired(claims_dir):
        return {
            "verdict": "granted-with-warning",
            "reason": (
                f"{holder_sid} reserved this at brief and never applied it — the "
                f"{_claims.BRIEF_CLAIM_LEASE_MINUTES}-minute brief-stage lease has "
                "elapsed, so the reservation is takeable"
            ),
            "holder": holder_sid,
            "holder_live": None,
            "held_by_self": False,
            "claim_age_minutes": None,
            "claim_stage": stage,
            "drop_invocation": drop_invocation,
            "unclean_prior_holder": False,
        }

    try:
        holder_live = _liveness.claim_holder_live(str(claims_dir), cwd_str)
    except (OSError, ValueError):
        holder_live = False

    if holder_live:
        related_sessions = _lineage_related_sessions(repo_root, fm)
        if holder_sid in related_sessions:
            return {
                "verdict": "granted",
                "reason": (
                    f"held by {holder_sid} — that session authored this artifact or "
                    "holds/consumed one of its predecessors; a clean handover, not "
                    "contention (AC3e)"
                ),
                "holder": holder_sid,
                "holder_live": True,
                "held_by_self": False,
                "claim_age_minutes": None,
                "claim_stage": stage,
                "drop_invocation": drop_invocation,
                "unclean_prior_holder": False,
            }
        return {
            "verdict": "denied",
            "reason": f"held by {holder_sid} — live",
            "holder": holder_sid,
            "holder_live": True,
            "held_by_self": False,
            "claim_age_minutes": None,
            "claim_stage": stage,
            "drop_invocation": drop_invocation,
            "unclean_prior_holder": False,
        }

    return {
        "verdict": "granted-with-warning",
        "reason": f"held by {holder_sid}; that session is not live",
        "holder": holder_sid,
        "holder_live": False,
        "held_by_self": False,
        "claim_age_minutes": None,
        "claim_stage": stage,
        "drop_invocation": drop_invocation,
        "unclean_prior_holder": True,
    }


def _adopt_into_baton(
    repo_root: Path, artifact_path: str, fm: Optional[dict] = None
) -> None:
    """C3 (docs/plans/2026-08-18-a-session-always-has-a-baton.md § C3,
    "a pickup adopts the session baton as a fan-in edge"): record the
    artifact this pickup just claimed into THIS session's baton record's
    ``adopted_artifacts[]`` (``session_baton.store.merge_baton`` —
    dedup-extends, never replaces, so a second `brief` of the same artifact
    within the same session is a no-op here).

    AC14 (C6): also NAME the baton from the artifact being adopted. A
    pickup session's baton otherwise carries a null `title`/`intent` while
    the operator-named handoff sits right here in `adopted_artifacts[0]` —
    this is a DERIVATION off that already-claimed artifact, not a new
    prompt-shape discrimination (the mint op's first-wins `first_prompt`
    policy is untouched; it does not run on this path). Only stamps when
    the baton doesn't already carry a title, so a later, different
    adoption in the same session never clobbers the first one. `title`
    comes straight off the schema-required field; `intent` is derived from
    `session_goal` (forward-looking, unlike the retrospective `summary`)
    when the frontmatter carries one — otherwise left unset.

    Called only from the `claim_at_brief` branches immediately after
    :func:`acquire_brief_claim` — i.e. only when this `brief()` is actually
    the one taking the pickup lock (contract § two-phase-stateless
    protocol's single-shot CLI shape), never from a survey brief.

    Fail-open, mirroring `quick_wrap_assemble._print_commits_into_baton`
    and `session_baton.store`'s own posture throughout: any failure to
    resolve this session's id, or to read/write the baton store, is
    swallowed here — an advisory fan-in edge must never block a pickup.
    The naming derivation shares that posture: a malformed or
    frontmatter-less `fm` must still let the artifact adopt.
    """
    try:
        sid = _session_core.resolve_session_id(str(repo_root))
    except Exception:  # noqa: BLE001 — advisory write must never raise into brief()
        return
    if not sid:
        return

    kwargs: dict[str, Any] = {
        "adopted_artifacts": [artifact_path],
        # The journal closes here. A session's birth baton is live only while
        # the session is the thing accruing work; once a pickup adopts an
        # artifact, that artifact is what the work belongs to, and this record
        # is its ancestor rather than a second live claimant. `closed_at`/
        # `closed_into` are first-wins in the store, so a second adoption in
        # the same session leaves the closure naming the one that ended it.
        #
        # No artifact is minted and nothing under `state/handoffs/` is touched:
        # closure is a fact about the JSON record, which is why it rides the
        # `merge_baton` call already being made here rather than promoting the
        # journal into the corpus. The store write is the whole cost.
        "closed_into": artifact_path,
    }
    if fm:
        try:
            already_titled = bool(read_baton(sid, cwd=str(repo_root)).get("title"))
        except Exception:  # noqa: BLE001 — naming is best-effort, never load-bearing
            already_titled = True  # err toward not stamping over unknown state
        if not already_titled:
            title = fm.get("title")
            if title:
                kwargs["title"] = title
            intent = fm.get("session_goal")
            if intent:
                kwargs["intent"] = intent

    try:
        # Review: reviewer(wsc-slice-5) — now_iso() must be called inside this
        # try/except, not while building kwargs above, so a future non-trivial
        # now_iso() can never raise uncaught into brief()'s claim path.
        kwargs["closed_at"] = _session_core.now_iso()
        merge_baton(sid, cwd=str(repo_root), **kwargs)
    except Exception:  # noqa: BLE001 — advisory write must never raise into brief()
        pass


def acquire_brief_claim(
    repo_root: Path, class_: str, basename: str
) -> Optional[dict[str, Any]]:
    """Take the `brief`-stage claim on `<class>-claims/<basename>` — the fix
    for the 2026-08-10 duplicate-memo incident (`state/bug-backlog/
    2026-08-10-pickup-claim-lands-at-apply-not-at-brief-36f1446e3e4b.yaml`).

    Before this, the claim landed only during `apply`, so everything between
    `brief` and `apply` — reading the artifact, verifying it against HEAD,
    drafting the reply, and, in the incident, SENDING a counter memo to a
    sibling repo — ran with no mutual exclusion at all. Two sessions each took
    a clean no-claim brief, each did the work, and each shipped a memo; the
    second `apply` failed correctly, but the externally-visible effect had
    already left the repo. The claim has to land at the START of that window
    for the exclusion to mean anything.

    Returns a record of what the acquisition DISPLACED, or `None` when it
    displaced nothing (a fresh lock, a re-brief of a lock this session already
    holds, or a failed acquisition). Shape when non-`None`:

        {"holder": <the sid we took it from>,
         "basis": "expired-brief-lease" | "dead-holder" | "holder-absent"
                  | "holder-liveness-unknown",
         "claim_age_minutes": <age of the claim we displaced>,
         "liveness_basis": <the session.liveness basis behind "basis", or None>,
         "liveness_live": <the session_verdict boolean behind "basis", or None
                  when no verdict was available (see "holder-absent") --
                  Review: coordinator:code-reviewer nit. Surfaced alongside
                  "liveness_basis" so an incident-report reader can tell a
                  confirmed-dead "stable-pid" record from a "stable-pid"
                  record that structurally DISAGREED (live=True, folded into
                  "holder-liveness-unknown" for the label) after the fact --
                  before this field, both cases discarded the boolean at the
                  return boundary and were indistinguishable post hoc.>}

    `basis` is `"dead-holder"` ONLY when a process-identity check (Layer 1 /
    `"stable-pid"`) both ran AND confirmed the prior holder's process gone
    (`session_verdict`'s liveness boolean reads `False`) — Review: staff-eng
    F1, a `"stable-pid"` basis with the boolean reading `True` means the
    check confirmed the holder ALIVE and must not be reported as a
    confirmed death. `session_verdict` finding no evidence for the holder
    at all — `None` (no local session dir, no harness-registry record, a
    charset-rejected sid, or any exception from the call itself) — reports
    `"holder-absent"` instead (Review: staff-eng F2): none of those arms ran
    a process check, so labelling that `"dead-holder"` asserts a
    confirmation that never happened, on the SAME "absence of evidence"
    shape the `session_live` half of this fix already treats as not proof
    of death. A takeover that instead rests on Layer 2's recency inference
    alone (`"recency-window"` / `"recency-window-mtime"` — a holder simply
    hasn't refreshed `last_activity` inside the 30-minute window, which this
    module's own `BRIEF_CLAIM_LEASE_MINUTES` docstring documents as a
    routine occurrence for a session mid-dispatch), the fail-open
    `"unknown"` arm, or the `"stable-pid"`-but-live-`True` structural
    disagreement above, reports `"holder-liveness-unknown"` instead — none
    of those is proof of death (2026-08-11 incident, cross-repo/inbox/
    2026-08-11-example-market-data-repo-em-reclaim-labels-a-live-session-dead-
    without-checking.md).

    That record exists so a reclaim stays DISTINGUISHABLE from a fresh
    pickup in the emitted brief. Without it the two collapse: the moment this
    function takes over an abandoned reservation, `compute_claim_grant`
    re-reads the dir, finds this session holding it, and reports the row-2
    "you already hold this" that a first-ever brief of an unclaimed artifact
    also reports. An EM would have no way to tell "nobody had this" from "I
    just took this off session X" — and the second one it needs to know
    about, because X may still be mid-work with an artifact it believes it
    holds.

    A failed acquisition (a live peer holds it) is NOT an error and is not
    raised: the `compute_claim_grant` call that follows re-reads the same dir
    and resolves the contention into the existing `denied` / stand-down
    narration, which is the surface the EM already knows how to read.
    Acquiring is an offer to take the lock, never a second gate.

    `basis` derivation truth table (Review: coordinator:code-reviewer nit —
    the branches below stay terse and cite back here rather than each
    re-explaining the same reasoning inline):

        prior_lease_expired | liveness_basis      | liveness_live | basis
        --------------------|----------------------|---------------|------------------------
        True                | (any)                | (any)         | expired-brief-lease
        False               | "stable-pid"          | False         | dead-holder
        False               | None                  | (n/a)         | holder-absent
        False               | "recency-window(-mtime)" / "unknown" | (any) | holder-liveness-unknown
        False               | "stable-pid"          | True          | holder-liveness-unknown

    The last row is the structural-disagreement case (F1): `session_verdict`
    (computed here, label-only) and `session_live` (what the takeover
    actually acted on) can disagree under
    `COORDINATOR_SESSION_LAYER1_DISABLE`, so a "stable-pid" basis is proof of
    death only when its OWN boolean also reads False — never on the basis
    string alone.
    """
    claims_dir = repo_root / ".git" / "coordinator-sessions" / f"{class_}-claims" / basename

    # Snapshot the incumbent BEFORE the mkdir race — after a successful
    # takeover the dir names us, and what we displaced is unrecoverable.
    prior_holder: Optional[str] = None
    prior_age: Optional[int] = None
    prior_lease_expired = False
    prior_liveness_basis: Optional[str] = None
    prior_liveness_live: Optional[bool] = None
    if claims_dir.is_dir():
        try:
            prior_holder = (
                (claims_dir / "session_id").read_text(encoding="utf-8").strip() or None
            )
        except OSError:
            prior_holder = None
        prior_age = _claims.claim_age_minutes(claims_dir)
        prior_lease_expired = _claims.brief_lease_expired(claims_dir)
        if prior_holder:
            # Snapshotted for the SAME reason prior_holder/prior_age are —
            # after a successful takeover the dir names us, and the
            # evidence behind why the takeover was even ALLOWED is gone.
            # `claim_artifact`'s own takeover decision (session/claims.py)
            # only ever asks the boolean `liveness.claim_holder_live`,
            # which throws away exactly the distinction this needs: a
            # process-identity-confirmed dead holder ("stable-pid" basis,
            # live=False) vs. a holder merely inferred dead by NOT having
            # refreshed `last_activity` inside the 30-minute Layer-2 window
            # ("recency-window"/"recency-window-mtime", live=False) — a
            # basis this module's own `BRIEF_CLAIM_LEASE_MINUTES` docstring
            # admits routinely under-covers real work ("measured dispatches
            # on this surface run past 30 minutes routinely"). Fixes the
            # 2026-08-11 incident (cross-repo/inbox/2026-08-11-market-
            # intelligence-em-reclaim-labels-a-live-session-dead-without-
            # checking.md): a live, heads-down-executing holder was
            # narrated "that session reads dead" on evidence that was only
            # ever a stale recency read, never a confirmed-dead process
            # check.
            try:
                verdict = _liveness.session_verdict(prior_holder, cwd=str(repo_root))
            except Exception:
                verdict = None
            if verdict is not None:
                # Review: staff-eng F1 — the live boolean (slot 0) must be
                # consulted, not just the basis string (slot 1). `session_live`
                # (which `claim_artifact`'s takeover actually acted on) and
                # `session_verdict` (computed here, for the label only) are
                # NOT the same computation — `session_live` honours the
                # `COORDINATOR_SESSION_LAYER1_DISABLE` rollback lever,
                # `_verdict_for_sdir` deliberately does not (its own
                # docstring) — so a "stable-pid" basis here can still mean
                # CONFIRMED ALIVE (`verdict[0] is True`) even though the
                # takeover already happened on a different, disabled-Layer-1
                # read. Dropping the boolean rendered that structural
                # disagreement as a confirmed death.
                prior_liveness_live = verdict[0]
                prior_liveness_basis = verdict[1]

    try:
        # A re-brief of a lock this session already holds refreshes the lease
        # rather than contending for it — active work must not age out.
        if _claims.touch_brief_claim(class_, basename, cwd=str(repo_root)):
            return None
        took_it = _claims.claim_artifact(
            class_,
            basename,
            cwd=str(repo_root),
            stage=_claims.CLAIM_STAGE_BRIEF,
        )
    except (OSError, ValueError):
        # A brief that cannot take the lock still owes the EM a decision
        # object — the grant computation below reports the claim state either
        # way, so a failed acquisition degrades to today's behaviour rather
        # than losing the whole brief.
        return None

    if not took_it or prior_holder is None:
        return None
    try:
        now_holder = (claims_dir / "session_id").read_text(encoding="utf-8").strip()
    except OSError:
        now_holder = ""
    if now_holder == prior_holder:
        return None
    # See the truth table in this function's docstring — branches below cite
    # back to it rather than re-deriving the reasoning inline.
    if prior_lease_expired:
        basis = "expired-brief-lease"
    elif prior_liveness_basis == "stable-pid" and prior_liveness_live is False:
        # Row 2 (Review: staff-eng F1) — confirmed dead: Layer 1 ran AND the
        # boolean itself reads False.
        basis = "dead-holder"
    elif prior_liveness_basis is None:
        # Row 3 (Review: staff-eng F2) — no verdict at all (no local dir, no
        # registry record, charset-rejected sid, or the call raised): no
        # process check ran, so this is not "dead-holder".
        basis = "holder-absent"
    else:
        # Rows 4-5 — a verdict WAS available but is not proof of death: a
        # recency-only inference ("recency-window(-mtime)"), the fail-open
        # "unknown" arm, or a "stable-pid" basis whose boolean structurally
        # DISAGREES (live=True — F1, the 2026-08-11 incident shape).
        basis = "holder-liveness-unknown"
    return {
        "holder": prior_holder,
        "basis": basis,
        "claim_age_minutes": prior_age,
        "liveness_basis": prior_liveness_basis,
        "liveness_live": prior_liveness_live,
    }


def _claim_already_self_held(repo_root: Path, class_: str, basename: str) -> bool:
    """True iff THIS session is the recorded holder of the ``<class>-claims/
    <basename>`` lock dir — the same identity predicate ``compute_claim_grant``'s
    self-holder row (AC3b row 2, "you already hold this") already answers
    internally via ``liveness.claim_held_by_me``. Exposed as its own predicate
    so a ``directives[]`` builder can mark its own ``claim-artifact`` directive
    ``already_satisfied`` for idempotent same-session re-entry.

    Fixes: same-session ``pickup-assemble apply`` re-entry on a memo whose
    prior partial run already landed the `d1` claim-artifact directive used to
    hard-fail on re-apply — `apply` is explicitly designed to be re-runnable
    (module docstring § "the hold-path residue"; `directives[].already_
    satisfied` is skipped, never re-dispatched), but memo's `d1` was
    unconditionally `already_satisfied: False`, so a second `apply` in the
    SAME session called `claims.claim_artifact` again, which REJECTS a
    same-session reclaim for the memo class by design (`session.claims`
    module negative-spec) and raised, halting the whole run at `d1` — even
    though every OTHER directive gated behind it (including the terminal
    `d-action-memo`) was otherwise ready to land.

    NEVER broadens to a different session, live or dead — ``claims.
    claim_artifact``'s own EEXIST branch is untouched by this predicate and
    still denies/takes-over exactly as it does today; this exists purely so
    ``apply``'s directive layer never re-asks that primitive a question the
    brief-time snapshot already answered for THIS session, not to change what
    the primitive decides for anyone else.
    """
    claims_dir = repo_root / ".git" / "coordinator-sessions" / f"{class_}-claims" / basename
    if not claims_dir.is_dir():
        return False
    try:
        return _liveness.claim_held_by_me(str(claims_dir), cwd=str(repo_root))
    except (OSError, ValueError):
        return False


def _predecessor_artifact_paths(fm: dict[str, Any]) -> list[str]:
    """`predecessor` / `additional_predecessors` / `forked_from` (AC3e) —
    every lineage-ancestor path this artifact's frontmatter names, filtering
    the `predecessor: none`/empty sentinel spinoffs carry (schema §
    handoff.schema.json). Order is primary-predecessor first, then fan-in
    `additional_predecessors`, then `forked_from` — irrelevant to callers
    (all three feed one unordered relatedness set) but kept stable for
    deterministic test fixtures."""
    paths: list[str] = []
    predecessor = fm.get("predecessor")
    if predecessor and predecessor != "none":
        paths.append(str(predecessor))
    for entry in fm.get("additional_predecessors") or []:
        if entry and entry != "none":
            paths.append(str(entry))
    forked_from = fm.get("forked_from")
    if forked_from and forked_from != "none":
        paths.append(str(forked_from))
    return paths


def _compute_artifact_chain(
    abs_artifact_path: Path,
    repo_root: Path,
    classification: str,
) -> Optional[dict[str, Any]]:
    """Computes `artifact.chain` (D1-D3,
    docs/plans/2026-08-18-the-pickup-brief-computes-its-own-contin.md) — the
    transitive continuation-edge walk quick-wrap's conclusion gate needs,
    computed once here instead of re-derived from three raw frontmatter
    reads (`predecessor`/`additional_predecessors`/`forked_from`) on every
    pickup.

    Walks `dag.CONTINUATION_EDGE_KINDS` BY REFERENCE (D1) — never
    `forked_from`: a spinoff is a niece, not a descendant (see that
    constant's docstring). Do NOT derive this from `_predecessor_artifact_
    paths` above — that helper unions the ARCHIVAL set (including
    `forked_from`) for a different question (relatedness/claim-contention)
    and would invert this gate for a spinoff.

    Returns `None` for a non-handoff-family *classification* (a memo, AC6)
    — an explicit null, never a fabricated zero-block a reader could
    mistake for "walked, found nothing".

    Keyed on *classification*, NOT on whether *fm* happens to carry a
    lineage field. Those two differ for a real and load-bearing case: 3 of
    585 handoff-family artifacts in this corpus (2 of them live) omit
    `predecessor`/`additional_predecessors`/`forked_from` entirely. Under a
    presence-check proxy each would emit `chain: null` — indistinguishable,
    to the conclusion gate consuming this field, from "not a handoff", when
    the truthful answer is `ancestor_count: 0`, chain root. The walk
    handles the no-edge case correctly on its own (`orderedPaths == [self]`
    -> 0), so there is nothing a presence check buys and one wrong answer
    it costs.

    Never raises (AC7): `dag.walk_forward` already degrades an
    unresolvable or cyclic edge to a `terminatedEarly` verdict rather than
    raising, and this function does not wrap it in anything that could
    swallow that verdict along with an error.

    `repo_root` is passed explicitly to `walk_forward` (AC8) — never left
    to its own two-dirs-up inference from `handoff_dir`, which is correct
    only for `<repo_root>/state|archive/handoffs` and breaks for a
    month-nested `archive/handoffs/YYYY-MM/` artifact, which pickup
    resolves routinely via its archive fallback.
    """
    if classification not in ("handoff", "spinoff", "archived"):
        return None

    walk = dag.walk_forward(
        str(abs_artifact_path),
        edge_kinds=set(dag.CONTINUATION_EDGE_KINDS),
        handoff_dir=str(repo_root / "state" / "handoffs"),
        repo_root=str(repo_root),
    )
    ordered = walk["orderedPaths"]
    ancestor_count = max(len(ordered) - 1, 0)
    ancestor_paths = ordered[1:]
    root_abs = ordered[-1] if ancestor_count else None
    walk_verdict = walk["terminatedEarly"] or "clean"

    def _display(raw: str) -> str:
        p = Path(raw)
        return rel_id(p, repo_root) if _is_relative(p, repo_root) else str(p)

    return {
        "ancestor_count": ancestor_count,
        "paths": [_display(p) for p in ancestor_paths],
        "root": _display(root_abs) if root_abs is not None else None,
        "walk": walk_verdict,
    }


def _resolve_lineage_artifact_path(repo_root: Path, relative_path: str) -> Optional[Path]:
    """Archive-aware resolution of a lineage-ancestor reference (AC3e),
    following `_compute_artifact_chain`'s worked example: routed through
    `dag.resolve_target` (the same path/basename/archive/month-foldered
    tiers every sibling consumer already uses) rather than a bare
    `repo_root / relative_path` join, which only ever hits a still-live
    file — an ancestor archived to `archive/handoffs/YYYY-MM/` resolves to
    None under the bare join and silently drops out of relatedness.

    Returns `None` when the reference is absent, unresolvable, or resolves
    only to the `'git-history'` sentinel (present in history, absent on
    disk — nothing here to read).
    """
    from coordinator_core.dag import resolve_target as _dag_resolve_target

    resolved = _dag_resolve_target(
        relative_path,
        str(repo_root / "state" / "handoffs"),
        str(repo_root),
        include_history_tier=False,
    )
    if not resolved or resolved == "git-history":
        return None
    return Path(resolved)


def _read_lineage_artifact_fm(candidate: Path) -> Optional[dict[str, Any]]:
    """Best-effort frontmatter read of an already archive-aware-resolved
    lineage-ancestor path (AC3e) — see `_resolve_lineage_artifact_path`.
    Returns `None` on any read/parse failure — an unreadable ancestor
    contributes no relatedness, never a crash and never a false relation."""
    try:
        if not candidate.is_file():
            return None
        text = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    split = split_frontmatter(text)
    if split is None:
        return None
    return _parse_fm_dict(split.fm_text)


_LOG = logging.getLogger(__name__)


def _lineage_related_sessions(repo_root: Path, fm: dict[str, Any]) -> "frozenset[str]":
    """The AC3e lineage-relatedness set for one artifact: every session id a
    live-holder check must treat as a HANDOVER, never a competing peer.

    Two sources, both resolved from frontmatter the assembler already
    parses (no new evidence source, per the contract's "Lineage-aware
    liveness" section):
      1. This artifact's own `authoring_session` — the session that wrote
         it is, by construction, handing it off, not contending for it.
      2. The `claimed_by`/`consumed_by`/`picked_up_by`/`authoring_session`
         of every artifact named in `predecessor` / `additional_predecessors`
         / `forked_from` — a predecessor's still-live claimant is the
         session handing THIS artifact over, most commonly the plan-
         authoring session at the plan->execute seam (AC3e, Defect A).

    Applied identically by `competing_claim`, `compute_liveness_signal`, and
    `compute_claim_grant`'s holder check (contract: "leaving one unfiltered
    reintroduces the defect through whichever field the EM happens to read
    first") — this is the single function all three call, so they cannot
    drift apart on what counts as related.
    """
    related: set[str] = set()
    authoring_session = fm.get("authoring_session")
    if authoring_session:
        related.add(str(authoring_session))

    for lineage_path in _predecessor_artifact_paths(fm):
        resolved_path = _resolve_lineage_artifact_path(repo_root, lineage_path)
        if resolved_path is None:
            continue
        lineage_fm = _read_lineage_artifact_fm(resolved_path)
        if lineage_fm is None:
            continue
        authoring_session = lineage_fm.get("authoring_session")
        if authoring_session:
            related.add(str(authoring_session))
        # Ledger-first (C11, row 18): the predecessor's claim holder resolves
        # through `_resolve_ledger_first_holder` (claimed_by/consumed_by via
        # `claim_state`, picked_up_by as the last-resort mirror-only
        # fallback) instead of a raw frontmatter-only scan, so a predecessor
        # whose mirror reverted but whose ledger still holds a live claim
        # still counts as lineage-related. Threaded through the SAME
        # archive-aware `resolved_path` as the frontmatter read above (not
        # the raw `lineage_path`) — otherwise an archived predecessor
        # contributes its `authoring_session` but not its ledger-held
        # claimant, and AC5 is only half met.
        lineage_holder = _resolve_ledger_first_holder(repo_root, resolved_path, lineage_fm)
        if lineage_holder:
            related.add(lineage_holder)

    return frozenset(related)


def compute_liveness_signal(
    repo_root: Path,
    fm: dict[str, Any],
    artifact_path: Optional[str] = None,
    self_session_id: Optional[str] = None,
) -> bool:
    """AMENDMENT 2026-07-24 (pickup-as-a-fully-assembled-decision-surface,
    chunk C7 Part A) — SUPERSEDES the reviewed three-signal positive-liveness
    OR. Collapses to an explicit frontmatter CLAIM-STAMP STATE MACHINE, not
    inference: the class-appropriate durable frontmatter field
    (`claimed_by`/`consumed_by`/`picked_up_by`) IS the "is-this-picked-up"
    record, and it is author-un-mintable by construction — only the
    pickup/claim/claim-stamp path (`claim-handoff`, C8's
    `claim-memo-stamp`) ever writes it; `/handoff` and the memo authoring
    path never do. That property is what dissolves the deadlock this
    amendment exists to fix (PM-witnessed transcript, this plan's own
    execution-authorization-stamp commit `3fd41a1a` false-fired the deleted
    commit-recency signal on the very commit authorizing its own pickup):
      - stamp ABSENT (a just-authored, never-picked-up artifact) -> no
        liveness inference needed at all, proceed.
      - stamp present & this session, or lineage-related
        (`_lineage_related_sessions` — AC3e) -> a handover, not contention,
        proceed.
      - stamp present & a DIFFERENT, non-lineage session -> THE REAPER: a
        single narrow `session.liveness.session_live` check on the STAMPED
        id, never a scan, never a commit-recency proxy. Live -> a genuine
        peer/handover, fire (the caller surfaces the stand-down judgment
        point). Dead -> the stamp is stale; the read side simply ignores it
        (reap-by-proceeding) — no separate write, since `d2`/
        `claim-memo-stamp` re-stamps on the next successful claim anyway.

    DELETED (this amendment): signal (b) commit-recency
    (`_commit_recency_signal`) and signal (c) active-handoff-scan
    (`_active_handoff_scan_signal`) — both were un-lineage-filtered proxies
    for "is someone iterating on the cited plan," and (b) is exactly what
    fired on the plan's own authorization commit. `_cited_plan_path` had no
    other consumer and was pruned with them. `_lineage_related_sessions` is
    KEPT — it was the one filter that got the deadlock right.

    REPLACE-VS-COMPLEMENT (resolved, COMPLEMENT): this collapse of the
    liveness READ does not touch the side-file atomic claim
    (`compute_claim_grant`/`session-claim-cli`) — that stays the ephemeral,
    mkdir-atomic mutual-exclusion primitive `apply` re-resolves immediately
    before mutating (AC9f). The two layers answer different questions
    (atomic grab-exclusion vs. durable "who holds this baton") and are
    deliberately not unified — see this function's call sites for the
    detail.

    Self-session exclusion (2026-07-29, defect 1 of the doe-claude-em
    self-claim-reads-as-live-peer memo): the docstring above has always
    promised "stamp present & this session ... -> a handover, not
    contention, proceed", but `_lineage_related_sessions` never contains the
    CALLING session's own id — it is derived purely from `authoring_session`
    plus the predecessor chain, never from who is running right now. Without
    the caller's own id in the exclusion set, a session's own
    `claimed_by`/`consumed_by`/`picked_up_by` stamp reads as a live foreign
    peer on any re-`brief`/re-`apply` of an artifact this session already
    claimed — exactly the error-recovery path, and exactly where `j1`
    ("Any peer live on this handoff/plan? Stand down?") must NOT fire
    against the caller itself. `self_session_id` closes that gap: when
    omitted, resolves via `_session_core.resolve_session_id(str(repo_root))`
    (same resolver `session.liveness.claim_held_by_me` uses for its own
    `my_sid` parameter) so the one production call site needs no change;
    passing it explicitly lets tests and TOCTOU-sensitive callers pin one
    resolved id across a multi-check sequence instead of re-resolving.
    Resolution failure degrades to "nothing extra excluded" (current
    behaviour), never a raised exception — mirrors the
    `except (OSError, ValueError): continue` tolerance already applied to
    the per-stamp liveness check below.
    """
    related_sessions = set(_lineage_related_sessions(repo_root, fm))

    self_sid = self_session_id
    if self_sid is None:
        try:
            self_sid = _session_core.resolve_session_id(str(repo_root))
        except (OSError, ValueError):
            self_sid = ""
    if self_sid:
        related_sessions.add(str(self_sid))

    # Ledger-first (C11, row 17): the class-appropriate stamp is now resolved
    # via `_resolve_ledger_first_holder` when `artifact_path` is available
    # (the one production call site always passes it) — a ledger-held claim
    # whose mirror reverted still surfaces as the stamped holder. Falls back
    # to the raw frontmatter scan only when no `artifact_path` was given
    # (tests exercising this function against a bare `fm` dict).
    stamped_sid: Optional[str] = None
    if artifact_path is not None:
        stamped_sid = _resolve_ledger_first_holder(repo_root, artifact_path, fm)
    if stamped_sid is None:
        for key in ("claimed_by", "consumed_by", "picked_up_by"):
            sid = fm.get(key)
            if sid:
                stamped_sid = str(sid)
                break

    if stamped_sid and stamped_sid not in related_sessions:
        try:
            if _liveness.session_live(stamped_sid, str(repo_root)):
                return True
        except (OSError, ValueError):
            pass

    return False


def _parse_scope_entry(entry: str) -> tuple[Optional[str], str]:
    """Split one `scope:` entry into `(repo_id, path)` — `(None, path)` for a
    bare local-repo entry, mirroring `compute_tree_quiescence`'s own parse of
    the `<repo-id>: <path>` sibling shape (`_SCOPE_SIBLING_PREFIX_RE`)."""
    match = _SCOPE_SIBLING_PREFIX_RE.match(entry)
    if match is None:
        return None, entry.strip()
    return match.group(1), match.group(2).strip()


def _paths_overlap(path_a: str, path_b: str) -> bool:
    """Directory-prefix overlap between two scope paths in the SAME repo —
    equal, or one is an ancestor directory of the other."""
    a = path_a.strip().rstrip("/")
    b = path_b.strip().rstrip("/")
    if not a or not b:
        return False
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _scopes_intersect(scope_a: list[str], scope_b: list[str]) -> bool:
    """Whether two artifacts' `scope:` lists name any overlapping path —
    the code-computed replacement for the EM eyeballing "is this sibling
    claim actually about my files?" Conservative by construction: an empty
    list on EITHER side can't prove non-overlap, so it counts as
    intersecting (the caller falls back to the pre-existing blocking
    behavior rather than a false all-clear)."""
    if not scope_a or not scope_b:
        return True
    parsed_a = [_parse_scope_entry(e) for e in scope_a]
    parsed_b = [_parse_scope_entry(e) for e in scope_b]
    for repo_a, path_a in parsed_a:
        for repo_b, path_b in parsed_b:
            if repo_a != repo_b:
                continue
            if _paths_overlap(path_a, path_b):
                return True
    return False


# ---------------------------------------------------------------------------
# C4 (docs/plans/2026-08-19-batons-unify-into-one-successor.md § C4): the
# held-set + four-arm refusal DECISION for baton unification. Split from the
# routing (C5) at the decision/action seam — this section computes a verdict
# and writes NOTHING, so it is testable in isolation and cannot half-mutate
# anything. See `compute_baton_unification_verdict`'s own docstring for the
# full contract; this banner exists only to mark the boundary for a reader
# scanning the file.
# ---------------------------------------------------------------------------


def _role_axis_is_unknown(role_raw: Any) -> bool:
    """Is `baton_role` UNKNOWN on this record — i.e. absent, or present
    carrying a null-ish placeholder?

    Exists because this repo's frontmatter reader is a lightweight string
    parser, not a YAML loader: `baton_role: null` comes back as the literal
    STRING `'null'`, never Python `None`. A plain `is None` check therefore
    misses every explicitly-nulled record, and since `'null'` is also not
    `'work'`, such a record would fall between the two arms and be neither
    inherited nor COUNTED — the silent gap the unstamped count exists to
    close.

    DoE's ruling is `absence ≡ unknown, and unknown is NOT inheritable`
    (`handoff.schema.json :: baton_role`). A null-ish value asserts nothing,
    so it is unknown for that rule's purposes — it is emphatically NOT a
    stamped `record`, which is a deliberate not-work decision somebody made.
    Never widen this to treat an UNRECOGNISED value as unknown: a future
    third enum member must surface as unhandled, not read as legacy.
    """
    if role_raw is None:
        return True
    return str(role_raw).strip().lower() in ("", "null", "none", "~")


def _primary_held_disposition(
    root: Path,
    primary_held_path: str,
    target_fm: dict[str, Any],
) -> str:
    """One-candidate slice of `compute_competing_claim`'s classification
    (ledger-first holder resolution, `session.liveness` verdicts,
    `_lineage_related_sessions`, `_scopes_intersect`), scoped to D-F's
    PRIMARY held baton evaluated against the frontmatter of the artifact
    newly being picked up — `target_fm`, whose `scope` and lineage fields
    are the whole of what this consults. The target's PATH is deliberately
    NOT a parameter: nothing here reads it, and a signature advertising an
    input it ignores is the same doc/behaviour divergence D-H exists to end.

    Deliberately NOT a call to `compute_competing_claim` itself, for one
    reason: that function's `related_sessions` (`_lineage_related_sessions`
    alone) never includes the CALLING session's own id — correct for its
    own verdict, which is informational-only and never gates anything
    (PM ruling 2026-07-24), so omitting self-inclusion there is harmless.
    It is not harmless here: this disposition gates a REFUSAL, and the
    ordinary case is a session's own already-held baton, which must read as
    a handover, not a live foreign peer. This function applies the SAME
    self-inclusion `compute_liveness_signal` already does (2026-07-29
    self-claim fix) before classifying.

    Returns one of `stale-claim` / `handover` / `live-unrelated` /
    `live-peer` — never a fifth value. A primary held baton with no
    resolvable holder at all (should not occur for a ledger-sourced held
    claim, but this function does not assume the ledger and the frontmatter
    mirror never diverge — D-F's own rationale for reading the ledger in
    the first place) reads as `stale-claim`: nothing live to refuse
    against.
    """
    resolved = _resolve_lineage_artifact_path(root, primary_held_path)
    primary_fm = _read_lineage_artifact_fm(resolved) if resolved is not None else None
    primary_fm = primary_fm or {}

    holder_sid = _resolve_ledger_first_holder(root, resolved, primary_fm) if resolved is not None else None
    if not holder_sid:
        return "stale-claim"

    verdicts = _liveness.live_session_verdicts(str(root))
    entry = verdicts.get(holder_sid)
    holder_live = entry[0] if entry is not None else False
    if not holder_live:
        return "stale-claim"

    related_sessions = set(_lineage_related_sessions(root, target_fm))
    try:
        self_sid = _session_core.resolve_session_id(str(root))
    except (OSError, ValueError):
        self_sid = ""
    if self_sid:
        related_sessions.add(str(self_sid))

    if holder_sid in related_sessions:
        return "handover"

    target_scope = target_fm.get("scope", []) or []
    primary_scope = primary_fm.get("scope", []) or []
    if not _scopes_intersect(target_scope, primary_scope):
        return "live-unrelated"
    return "live-peer"


def _is_same_artifact(held_path: str, artifact_path: str) -> bool:
    """Whether a ledger-derived held path and the artifact being picked up
    name the SAME baton. Compared by BASENAME, deliberately: the claim
    ledger is basename-keyed, while `artifact_path` arrives repo-relative
    and a held leg may resolve through `state/handoffs/` or
    `archive/handoffs/<YYYY-MM>/` depending on whether a boot sweep has run
    — a path-equality test would read the same baton as two different ones
    across that move and re-open the defect this predicate closes.

    An empty `artifact_path` (`unify_run_batons`, which is not picking
    anything up) matches nothing, so that caller's held set is never
    narrowed by this.
    """
    if not artifact_path:
        return False
    return (
        held_path.replace("\\", "/").rsplit("/", 1)[-1]
        == artifact_path.replace("\\", "/").rsplit("/", 1)[-1]
    )


def compute_baton_unification_verdict(
    root: Path, fm: dict[str, Any], artifact_path: str
) -> dict[str, Any]:
    """C4: the held-set + four-arm refusal DECISION for a second pickup
    while this session already holds a baton. Computes a verdict; writes
    NOTHING — C5 (not this function) is the only thing allowed to act on
    it, behind C5's own predicate (D-I), which is ON as of `c09345b56`.

    THIS IS A PRE-CONDITION CHECK, NOT A RESUME ORACLE. Re-deriving this
    verdict after a partial unification action does NOT reproduce the
    original held set: stamping a parent `continued` (terminal, D-F's own
    disposed-not-held rule) or releasing its claim both shrink what a
    fresh call to this function would return. C5's crash-recovery path
    must resume by following a minted successor's `continued_into`
    back-edge, never by calling this function again — nothing here is
    shaped for that reuse, deliberately.

    (a) Held set (D-F): `baton_assemble._resolve_held_handoff_for_session`
    off the DURABLE CLAIM LEDGER — never re-derived from the `claimed_by`
    frontmatter mirror, which goes stale the instant a shipped baton is
    swept to `archive/handoffs/` (exactly the shipped-but-unarchived
    window this must survive). Imported locally (not at module scope):
    `baton_assemble` imports `compute_repo_identity_gate` from THIS module
    at ITS module scope, so a module-level import here would be circular —
    deferring it to call time is what makes the two-way reference safe.

    Called with `allow_standalone=True`: a session holding ZERO handoff
    claims (most commonly one that picked up via a cross-repo memo) reads
    as "nothing held", not a raised `ValueError` — the resolver's own
    documented distinction between a legitimate zero-claims shape and a
    genuine environment failure (no session id resolvable at all, which
    this function does NOT catch and lets propagate: that is a real
    failure, not a decision this function should paper over).

    A `degraded` set (the ORDERING contains a position decided by the
    arbitrary basename tiebreak — e.g. `/pickup x y`, two batons claimed
    in the same instant) still PROCEEDS. `degraded` is a set-level
    ordering signal, per the resolver's own docstring it "does NOT change
    which claim is picked" — it is never consulted as an input to this
    verdict.

    Each held baton is then filtered twice, in order:
      - DISPOSED exclusion: a baton whose `deployment_state` is one of
        `lifecycle_constants.HANDOFF_TERMINAL_DEPLOYMENT` (most commonly
        `shipped`, in the window before the boot sweep archives it) is
        disposed, not held, and drops out of the inheritable set.
      - Role-axis exclusion (`baton_role`, DoE-ratified `work | record`,
        C7 not yet landed): POSITIVE match only — `fm.get("baton_role")
        == "work"`, never a defaulted `.get`. Every on-disk record is
        absent the field today, so a naive positive-match filter alone
        would make the held set permanently, silently empty. A candidate
        skipped because the field is ABSENT (as opposed to present and
        `"record"`) is counted in `unstamped_skipped`, so the verdict can
        say whether "no unification" means nothing was held at all, or
        held batons existed but none were role-stamped yet — the same
        silent-zero shape AC10 names for the `/mise-en-place` fallback,
        one level down. No backfill and no default is invented here to
        dodge the count.

    (a2) CARDINALITY GATE — the inheritable set must contain a baton that
    is NOT the artifact being picked up. `brief()` claims the target
    (`acquire_brief_claim`) BEFORE routing here, so by the time the held
    set is resolved off the ledger the target is already in it: a first-
    and-only pickup therefore reads as "this session already holds an
    inheritable baton" and unifies a set of ONE with itself. That minted an
    empty `pickup_ready: true` successor two seconds after the claim, which
    then held the genuine successor's `deliverable_id` and advertised
    itself to the pickup index as available work (DoE-claude 2026-08-20,
    `cross-repo/inbox/2026-08-20-doe-claude-em-pickup-mints-a-phantom-
    successor.md`; session `b1578cac`, claim `14:14:56Z`, mint `98b95686e`
    at `14:15:00Z`).

    The gate is on the PRE-EXISTING set, not on the parent set: when a
    genuine fan-in exists, the target is still absorbed as a leg, which is
    C5's whole point. `verdict: "no-unification"` / `reason:
    "only-target-held"` falls through to `_adopt_into_baton`'s plain
    advisory append — the pre-C5 behaviour for the only-one-baton case.

    (b) Four-arm refusal (`_primary_held_disposition`), evaluated against
    D-F's PRIMARY held baton — "the extant baton", the primary input to
    every unification — relative to the artifact now being picked up:

      live-peer      -> REFUSE (a live, non-lineage, scope-overlapping
                         holder — genuine contention)
      live-unrelated -> REFUSE (a genuinely live foreign holder is the one
                         thing the predecessor handoff's anti-scope
                         forbids absorbing outright, scope-overlap or not)
      handover       -> PROCEED (this session's own claim, or a lineage
                         handover — the common, expected case)
      stale-claim    -> PROCEED (no live holder; nothing to refuse
                         against)

    Read-only (mirrors the rest of this module's AC2b/AC3 contract): reads
    disk/ledger state only, never mutates, never raises for a business
    outcome (only a genuine environment failure propagates).
    """
    from coordinator_core.baton_assemble import _resolve_held_handoff_for_session
    from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT

    primary, additional, degraded = _resolve_held_handoff_for_session(
        root, allow_standalone=True
    )

    if primary is None:
        return {
            "target": artifact_path,
            "verdict": "no-unification",
            "reason": "nothing-held",
            "held": {"primary": None, "additional": [], "degraded": degraded},
            "inheritable": [],
            "disposed_skipped": [],
            "unstamped_skipped": 0,
            "disposition": None,
            "message": "No held handoff claim on this session — nothing to unify.",
        }

    held_paths = [primary, *additional]
    inheritable: list[str] = []
    disposed_skipped: list[str] = []
    unstamped_skipped = 0

    for held_path in held_paths:
        resolved = _resolve_lineage_artifact_path(root, held_path)
        held_fm = _read_lineage_artifact_fm(resolved) if resolved is not None else None
        if held_fm is None:
            # Unreadable held claim — neither disposed nor role-matched;
            # excluded from inheritance but NOT counted as an unstamped
            # skip (that bucket is specifically the absent-axis case, not
            # "couldn't read the file at all").
            continue
        if held_fm.get("deployment_state") in HANDOFF_TERMINAL_DEPLOYMENT:
            disposed_skipped.append(held_path)
            continue
        role_raw = held_fm.get("baton_role")
        if role_raw == "work":
            inheritable.append(held_path)
        elif _role_axis_is_unknown(role_raw):
            unstamped_skipped += 1

    preexisting = [p for p in inheritable if not _is_same_artifact(p, artifact_path)]

    if not inheritable or not preexisting:
        if inheritable and not preexisting:
            no_unify_reason = "only-target-held"
        elif unstamped_skipped:
            no_unify_reason = "unstamped-role-skipped"
        elif disposed_skipped:
            no_unify_reason = "all-held-disposed"
        else:
            no_unify_reason = "nothing-inheritable"
        return {
            "target": artifact_path,
            "verdict": "no-unification",
            "reason": no_unify_reason,
            "held": {"primary": primary, "additional": additional, "degraded": degraded},
            "inheritable": [],
            "disposed_skipped": disposed_skipped,
            "unstamped_skipped": unstamped_skipped,
            "disposition": None,
            "message": (
                "The only inheritable held baton IS the artifact being picked "
                "up — a set of one is not a fan-in; nothing to unify."
                if no_unify_reason == "only-target-held"
                else (
                    f"{unstamped_skipped} held baton(s) skipped — role axis absent, "
                    "not yet stamped (C7 pending)."
                    if unstamped_skipped
                    else (
                        f"{len(disposed_skipped)} held baton(s) already disposed — "
                        "nothing left to unify."
                        if disposed_skipped
                        else "No inheritable held baton — nothing to unify."
                    )
                )
            ),
        }

    disposition = _primary_held_disposition(root, primary, fm)
    proceed = disposition in ("handover", "stale-claim")

    return {
        "target": artifact_path,
        "verdict": "proceed" if proceed else "refuse",
        "reason": disposition,
        "held": {"primary": primary, "additional": additional, "degraded": degraded},
        "inheritable": inheritable,
        "disposed_skipped": disposed_skipped,
        "unstamped_skipped": unstamped_skipped,
        "disposition": disposition,
        "message": (
            f"Held baton {primary!r} unifies cleanly ({disposition})."
            if proceed
            else (
                f"Held baton {primary!r} is held by a live, unrelated peer "
                f"({disposition}) — refusing to absorb it. Ask the holder to "
                "release or stand down first."
            )
        ),
    }


# ---------------------------------------------------------------------------
# C5 (docs/plans/2026-08-19-batons-unify-into-one-successor.md § C5): THE
# ACTION HALF. Routes a second pickup into `baton_assemble`'s EXISTING
# multi-leg mint (kind="handoff", self-resolved held set -> d1 scaffolds the
# successor with `additional_predecessors`, d6/d6* stamps every parent leg
# `continued` + `continued_into` in the SAME `apply()` call) instead of
# `_adopt_into_baton`'s plain append. Consumes `compute_baton_unification_
# verdict` (C4) — this section makes exactly ONE decision of its own (the
# routing predicate immediately below, ON as of `c09345b56`) and re-derives
# nothing else.
# ---------------------------------------------------------------------------


def _baton_unification_routing_enabled() -> bool:
    """D-I: the SINGLE named predicate gating C5's routing. NOW ON.

    It shipped OFF so no window opened where this engine's behaviour
    contradicted DoE's still-live pickup doctrine
    (`skills/pickup/SKILL.md`'s N-independent-dispositions contract). Their
    reply (`cross-repo/inbox/2026-08-19-doe-claude-em-baton-schema-8-2-0-
    landed-role-axis-named-doctrine-held.md`) set the release condition
    exactly: schema 8.2.0 landed at `3fb4a1053` carrying `baton_role`, the
    two doctrine files are held as ONE commit on their side, and "signal us
    at the flip-on commit and our side lands same-session". This commit is
    that signal.

    This function stays the ONLY place the decision is made — nothing else
    in this module tests an env var, config flag, or other proxy for it. A
    reader wanting the behaviour off does not add one: they flip this
    literal, in this function, reviewed as such.

    What changes for an operator, in one line: a pickup taken while this
    session already holds an inheritable baton no longer appends to a list
    — the held batons and the new one become ONE successor carrying them as
    fan-in legs. `_reassemble_bullet_lines`' docstring carries the reversed
    contract text.
    """
    return True


def _unification_parents(held: dict[str, Any]) -> list[str]:
    """`held["primary"]` + `held["additional"]`, `None`/empty entries
    dropped — the exact set C4's verdict already resolved via
    `_resolve_held_handoff_for_session`, reused verbatim rather than
    re-derived."""
    primary = held.get("primary")
    additional = held.get("additional") or []
    return [p for p in ([primary] + list(additional)) if p]


def _continued_into_target(root: Path, held_path: str) -> Optional[str]:
    """Reads `held_path`'s OWN `continued_into` field, archive-aware (the
    predecessor may already have been git-mv'd to `archive/handoffs/` by
    the mint this function is checking the result of). `None` when the
    artifact is unreadable or carries no `continued_into` yet — i.e. this
    leg was never (or not yet) superseded into a successor."""
    resolved = _resolve_lineage_artifact_path(root, held_path)
    fm = _read_lineage_artifact_fm(resolved) if resolved is not None else None
    if not fm:
        return None
    return fm.get("continued_into") or None


def _finish_unification_claims(root: Path, parents: list[str], successor_path: str) -> None:
    """Step 3 — the non-atomic half of unification, deliberately split from
    the mint+stamp transaction (step 2, `baton_assemble.apply`): claim the
    successor, THEN release every parent's DURABLE claim-ledger entry.

    ORDER IS LOAD-BEARING, not incidental. `_resume_pending_unification`'s
    only signal for "a mint already landed, finish the bookkeeping" is a
    parent STILL on this session's held-claims ledger, stamped `continued`
    with `continued_into` set. Releasing a parent BEFORE the successor is
    claimed would, on a crash after the LAST parent's release, erase that
    signal entirely — the ledger would show nothing held at all, and a
    retry would fall through to a FRESH (nothing-held) verdict rather than
    resuming, leaving the successor permanently unclaimed. Claiming first
    means every reachable crash point still has at least one parent claim
    on the ledger to resume from, right up to the point where cleanup is
    genuinely complete.

    Both halves are individually idempotent, so a retry after a crash
    anywhere in this function converges rather than double-acting:
    `claims.release_artifact` no-ops on an already-released or
    not-self-held claim (its own docstring: "Returns True on every no-op
    path"). The successor claim is guarded by hand: this session's own
    claim-ledger listing is checked FIRST and the claim call skipped when
    it already shows the successor held — handoff/memo claims REJECT a
    same-session re-claim by design (`claims.claim_artifact`'s own
    docstring), so a bare retry of `claim_handoff` on a resume would
    misread as a failure on the second pass without this guard.

    Deliberately NOT wrapped in a blanket `except` — see
    `_unify_into_successor`'s docstring for why unification failure must
    surface rather than being swallowed the way `_adopt_into_baton`'s
    advisory append is.
    """
    successor_basename = Path(successor_path).name
    if successor_basename:
        try:
            sid = _session_core.resolve_session_id(str(root))
        except (OSError, ValueError):
            sid = None
        already_held = False
        if sid:
            held_claims = _claims.list_claims_by_session(sid, cwd=str(root))
            already_held = ("handoff", successor_basename) in held_claims
        if not already_held:
            _claims.claim_handoff(successor_basename, cwd=str(root))

    for parent_path in parents:
        basename = Path(parent_path).name
        if basename:
            _claims.release_artifact("handoff", basename, cwd=str(root))


def _resume_pending_unification(root: Path) -> bool:
    """Crash-resume detection, run BEFORE any fresh C4 verdict is consulted
    — THE RESUME RULE. `compute_baton_unification_verdict`'s own docstring
    names why re-deriving it after a partial unification is unsound:
    stamping a parent `continued` (terminal) excludes it as disposed on
    any FRESH verdict, and releasing its claim shrinks the ledger-derived
    held set too — the exact seam that would unify a SUBSET and mint a
    SECOND, orphaned successor on a retried run.

    Reads this session's OWN still-held claims off the ledger
    (`_resolve_held_handoff_for_session`, `allow_standalone=True`) — a
    mechanical fact ("what does this session's claim ledger currently
    show"), never a re-derivation of C4's inheritable-set DECISION. A held
    leg already stamped `continued` with a `continued_into` set is proof
    step 2 (the mint+stamp transaction) already landed for it in a PRIOR
    run — step 3 (release + claim) is what crashed. Resumes by finishing
    step 3 against that SAME `continued_into` target, following the
    existing `_supersede_continued` up-edge, never by re-minting.

    Returns True when a resume ran (whether or not anything was actually
    left to do — `_finish_unification_claims` is itself idempotent), False
    when nothing here was pending and the caller should fall through to a
    fresh C4 verdict.

    REFUSES A DANGLING `continued_into` rather than acting on it. The stamp
    loop is one directive PER PARENT (`d6`, `d6-2`, …), so a failure part
    way through stamps some parents and not others; that run exits
    `APPLY_EXIT_PARTIAL_MUTATION`, and d1's compensator then deletes the
    successor scaffold it minted — correctly, since the scaffold is
    pristine. What survives is a parent stamped terminal pointing at a
    successor that no longer exists. Acting on that signal is worse than
    ignoring it: `_finish_unification_claims` would claim the phantom
    basename and then RELEASE every real parent claim, costing the session
    both batons and leaving a claim on a path nothing can resolve. So the
    successor is resolved on disk first and a dangling edge raises, named,
    with the parents' claims untouched — a loud stop an operator can act
    on, never a silent claim swap."""
    from coordinator_core.baton_assemble import _resolve_held_handoff_for_session
    from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT

    primary, additional, _degraded = _resolve_held_handoff_for_session(
        root, allow_standalone=True
    )
    held_paths = [p for p in ([primary] + list(additional)) if p]
    if not held_paths:
        return False

    successor: Optional[str] = None
    stamped_parents: list[str] = []
    for held_path in held_paths:
        resolved = _resolve_lineage_artifact_path(root, held_path)
        held_fm = _read_lineage_artifact_fm(resolved) if resolved is not None else None
        if not held_fm:
            continue
        if held_fm.get("deployment_state") in HANDOFF_TERMINAL_DEPLOYMENT:
            continued_into = held_fm.get("continued_into")
            if continued_into:
                stamped_parents.append(held_path)
                successor = successor or continued_into

    if not successor:
        return False

    if _resolve_lineage_artifact_path(root, successor) is None:
        raise RuntimeError(
            "baton unification resume: parent leg(s) "
            f"{stamped_parents} are stamped continued_into {successor!r}, "
            "but that successor does not resolve on disk — a prior run's "
            "parent-stamp loop failed part way and its scaffold was "
            "compensated away. Claims are untouched. Re-stamp or clear the "
            "parents' continued_into before picking up again."
        )

    _finish_unification_claims(root, stamped_parents, successor)
    return True


def _unify_into_successor(root: Path, verdict: dict[str, Any]) -> str:
    """The mint half of C5's action — reaches `baton_assemble.apply`'s
    kind="handoff", self-resolving self-brief (no `artifact_path`
    supplied), which mints the multi-parent successor (d1,
    `additional_predecessors`) and stamps every held parent leg `continued`
    + `continued_into` in the SAME call (d6/d6*).

    Deliberately NOT wrapped in a blanket `except` — unlike
    `_adopt_into_baton`'s advisory `adopted_artifacts` append (which must
    never block a pickup), unification mints a handoff, stamps parents
    terminal, and moves claims: a failed mint/stamp swallowed here would
    tell the operator nothing while the tree sits half-moved, the worst
    available outcome. Let it raise.

    Imports `baton_assemble.apply` at call time, matching
    `compute_baton_unification_verdict`'s own precedent: `baton_assemble.
    apply` imports `compute_repo_identity_gate` from THIS module at ITS
    module scope, so a module-level import here would be circular.

    RETURNS the successor path it resolved. It has to derive that value
    anyway (it is what proves the mint actually landed, and raises when no
    parent leg shows `continued_into`), so handing it back spares every
    caller a second archive-aware walk over the same parents to recompute a
    value already known here."""
    from coordinator_core.baton_assemble.apply import apply as _baton_assemble_apply
    from coordinator_core.contract import apply_base as _apply_base

    exit_code, report = _baton_assemble_apply(
        "handoff", "", session_id=None, repo_root=root, decisions=None, title=None
    )
    if exit_code != _apply_base.APPLY_EXIT_OK:
        raise RuntimeError(
            "baton unification mint failed "
            f"(exit_code={exit_code}, held={verdict.get('held')}): {report!r}"
        )

    parents = _unification_parents(verdict.get("held") or {})
    successor: Optional[str] = None
    for parent_path in parents:
        successor = _continued_into_target(root, parent_path)
        if successor:
            break
    if not successor:
        raise RuntimeError(
            "baton unification: mint reported success but no parent leg "
            f"shows continued_into (parents={parents})"
        )

    _finish_unification_claims(root, parents, successor)
    return successor


def route_baton_adoption(
    root: Path, artifact_path: str, fm: Optional[dict[str, Any]]
) -> None:
    """The C5 routing seam — called from BOTH `claim_at_brief` call sites
    in place of a bare `_adopt_into_baton`.

    Predicate OFF (default, D-I) is a TRUE no-op: falls straight through to
    `_adopt_into_baton`, byte-identical to pre-C5 behaviour — no verdict
    computed, no mint, no claim move, no parent stamped.

    Predicate ON:
      - a pending unification from a prior crashed run resumes FIRST
        (`_resume_pending_unification`) — never re-derived from a fresh
        verdict, see that function's docstring;
      - otherwise a fresh `compute_baton_unification_verdict` (C4) decides:
        `"proceed"` mints/unifies (`_unify_into_successor`); `"refuse"`
        writes NOTHING at all — not even the advisory append, since a live
        foreign holder must not gain an `adopted_artifacts` entry either;
        `"no-unification"` (nothing held, or nothing inheritable) falls
        through to the ordinary `_adopt_into_baton` append, unchanged from
        today.
    """
    if not _baton_unification_routing_enabled():
        _adopt_into_baton(root, artifact_path, fm)
        return

    if _resume_pending_unification(root):
        return

    verdict = compute_baton_unification_verdict(root, fm or {}, artifact_path)
    outcome = verdict.get("verdict")
    if outcome == "proceed":
        _unify_into_successor(root, verdict)
        return
    if outcome == "refuse":
        return
    _adopt_into_baton(root, artifact_path, fm)


# ---------------------------------------------------------------------------
# AC10's mutation half (docs/plans/2026-08-19-batons-unify-into-one-successor.md
# § C10 remainder): the ONE entry point `/mise-en-place` reaches unification
# through. It adds no unification implementation of its own — the anti-scope's
# "do not rebuild what exists" — it composes the SAME three pieces
# `route_baton_adoption` composes, in the same order, and differs from it only
# in having no artifact being picked up.
# ---------------------------------------------------------------------------


def unify_run_batons(root: Path, run_legs: Optional[Sequence[str]] = None) -> dict[str, Any]:
    """Unify this session's held batons into ONE successor, for a caller
    that is not picking an artifact up — `/mise-en-place`'s per-RUN
    unification (`backlog_grind_assemble.apply :: _dispatch_unify_batons`),
    called at most once per run by construction of its directive.

    Returns a report; raises only what `_unify_into_successor` raises (a
    genuine half-moved tree, never a business outcome). `unified` is the
    single field a caller should branch on.

    `run_legs` IS NOT AN INPUT TO THE MUTATION — it is the caller's own
    resolved inheritable set, carried into the report verbatim so the
    operator can see what the run believed it held next to what actually
    unified. The held set stays `_resolve_held_handoff_for_session` off the
    durable claim ledger (AC7), never a caller-supplied path list: a
    caller-supplied set would let a run's inventory table decide which
    batons get stamped terminal, which is precisely the authority the
    ledger exists to hold. A leg named by the run but absent from the
    ledger therefore does not unify, and reads that way in `run_legs` vs
    `parents`.

    A live peer's baton is out of reach here for a STRUCTURAL reason, not
    because the four-arm refusal catches it: the held set resolves through
    `list_claims_by_session(self_sid)`, so a foreign-held claim never
    enters it and the verdict reads `nothing-held`. The refusal arms stay
    the backstop (and would fire if that resolver ever widened); do not
    read them as what protects this path today.

    The verdict is computed with an EMPTY target frontmatter, which is the
    safe direction rather than an omission — but NOT for the reason it
    first looks like. `_scopes_intersect` is conservative by construction:
    an empty list on either side cannot prove non-overlap, so it counts as
    intersecting. An empty target therefore always intersects, and a live
    holder reaching `_primary_held_disposition` classifies `live-peer`, not
    `live-unrelated`. Both are refusal arms (`proceed` is only `handover`
    or `stale-claim`), so the safety property holds either way and no
    scope-overlap coin-flip occurs — the empty target buys a guaranteed
    refusal rather than a computed one.
    """
    legs = [str(leg) for leg in (run_legs or [])]
    base: dict[str, Any] = {"unified": False, "run_legs": legs, "parents": []}

    if not _baton_unification_routing_enabled():
        return {
            **base,
            "reason": "routing-disabled",
            "message": (
                "Baton unification routing is disabled (D-I) — "
                "resolved this run's inheritable set, mutated nothing."
            ),
        }

    if _resume_pending_unification(root):
        return {
            **base,
            "unified": True,
            "reason": "resumed-pending",
            "message": (
                "A prior run's unification was already mid-flight — resumed it "
                "to its own successor rather than minting a second one."
            ),
        }

    verdict = compute_baton_unification_verdict(root, {}, "")
    outcome = verdict.get("verdict")
    parents = _unification_parents(verdict.get("held") or {})
    report = {
        **base,
        "parents": parents,
        "reason": verdict.get("reason"),
        "message": verdict.get("message"),
        "verdict": verdict,
    }
    if outcome != "proceed":
        return report

    report["successor"] = _unify_into_successor(root, verdict)
    report["unified"] = True
    return report


#: `_resolve_send_message_addresses` relocated (2026-08-19, chunk C1a) to
#: `coordinator_core.session.work_state` — imported above.


#: C7's `_resolve_sent_by` sentinel (`coordinator_core/ops/fleet/memo_send.py`)
#: -- a memo whose sender was never resolvable AT SEND TIME. Mirrored here as
#: a literal, not imported: `memo_send.py` is the send-path module and this
#: is the pickup-path module; importing across that seam for one string
#: constant would create a coupling neither side's docstring documents, and
#: the sentinel's value (`"unresolved"`) is part of C7's frozen schema
#: contract, not something this module owns or may drift independently of.
_SENT_BY_UNRESOLVED = "unresolved"


def compute_sender_reachability(sent_by: Optional[str]) -> dict[str, Any]:
    """`gates.sender_reachability` (C8, docs/plans/2026-08-13-session-
    identity-earns-its-keep.md) -- renders the reachability of a memo's
    `sent_by` (C7) sender at THIS brief's render time, for the EM deciding
    whether a live reply is possible.

    Different render from the claim-holder path (`_resolve_send_message_
    addresses`/`compute_competing_claim`): a claim holder is resolved to
    offer contact for a CONTESTED artifact; a memo sender is resolved to
    offer a REPLY. Same mechanism (`reachability.resolve_address`/
    `resolve_advisory_address`), different wording -- this function never
    reuses the competing-claim strings.

    Returns `{}` when `sent_by` is falsy (a memo composed before C7, or one
    whose `sent_by` was never set) -- there is nothing to render.

    Sentinel arm: `sent_by == _SENT_BY_UNRESOLVED` (C7's `_resolve_sent_by`
    substitutes this when SEND-time resolution failed) is NOT one of
    `ResolveResult`'s four outcomes -- a sender that was never resolvable is
    a different fact from a sender who has since gone away, so it renders
    its own `"sender_unresolved"` outcome rather than folding into
    `not_reachable`, and `reachability.resolve_address` is never called for
    it (an `"unresolved"` string is not a session id).

    ALL FOUR `ResolveResult.outcome` arms get a render:
      - `own_session`: this session sent the memo to itself -- a
        self-receipt, named explicitly, not a reply target.
      - `reachable`: the exception, not the baseline -- the sender's live
        `SendMessage` address is taken directly from `result.address`
        (`resolve_address`'s own return already carries it -- one
        registry snapshot, not a second live query; see 68f8c14ce) and
        rendered inline.
      - `not_reachable`: the DOMINANT arm. At the `/workday-start` inbox
        grind the sender is gone by definition -- overnight memos. Rendered
        as an ordinary statement of fact, never a warning: no "warning",
        "unavailable", "could not", "failed", "unfortunately", "⚠", or "!"
        (docs/wiki/guard-messaging.md § Register; pinned by a negative-spec
        test, not by prose assertion alone).
      - `ambiguous`: real (`resolve_candidates` returns a list) and must
        NOT collapse into `not_reachable` -- rendered as its own outcome.

    Advisory only, mirroring `_resolve_send_message_addresses`'s discipline
    exactly: the local `from coordinator_core.session import reachability`
    import stays INSIDE the `try:` so an import-time failure degrades
    identically to a runtime one, and any resolution failure degrades to the
    `not_reachable` render rather than raising -- this function never
    touches a directive, judgment point, verdict, or disposition.

    Negative-spec: `address` is NOT durable identity and must never be
    persisted/reused past the instant this call computed it -- `sent_by`
    (the UUID, C7) is the one durable identity a caller may hold onto.
    `resolved_at` (one UTC ISO-8601 stamp) is stamped alongside so a reader
    of a PERSISTED copy of this dict (`.git/coordinator-sessions/decisions/
    *.json`, the one sanctioned exception per 68f8c14ce) can tell a stale
    address apart from a fresh one instead of trusting it silently. This
    function itself never writes anywhere -- persistence, if any, is entirely
    the caller's (the decision-object write path's) doing.
    """
    if not sent_by:
        return {}
    resolved_at = datetime.now(timezone.utc).isoformat()
    if sent_by == _SENT_BY_UNRESOLVED:
        return {
            "outcome": "sender_unresolved",
            "message": (
                "This memo's sender identity was never resolved at send time "
                "— no reachability to compute."
            ),
            "address": "",
            "resolved_at": resolved_at,
        }
    try:
        from coordinator_core.session import reachability

        result = reachability.resolve_address(sent_by)
    except Exception:
        result = None

    outcome = result.outcome if result is not None else "not_reachable"
    address = ""

    if outcome == "own_session":
        message = "This memo was sent by this same session — a self-receipt, not a reply target."
    elif outcome == "reachable":
        # Review: coordinator:code-reviewer — `result.address` already
        # carries the answer from the one `resolve_address` call above;
        # calling `resolve_advisory_address` here was a second, independent
        # live registry scan for data already in hand (one-snapshot
        # discipline per 68f8c14ce), and the two scans could disagree since
        # they weren't taken atomically.
        address = result.address or ""
        if address and address != "<this session>":
            message = f"Sender is reachable — reply via SendMessage to {address}."
        else:
            # Under resolve_address's current contract this arm cannot fire:
            # a "reachable" outcome only comes from a Candidate built with a
            # truthy record.name and record.messaging_socket_path, which
            # always yields a non-empty, non-"<this session>" address. This
            # guards against a future contract change to resolve_address, not
            # an observed live case — degrade to the ordinary not_reachable
            # render rather than claim an address this function cannot
            # actually produce.
            outcome = "not_reachable"
            address = ""
            message = "Sender's session has ended — action this the normal way."
    elif outcome == "ambiguous":
        message = (
            "Sender's session id matches more than one live session — "
            "reachability is ambiguous, not confirmed."
        )
    else:
        outcome = "not_reachable"
        message = "Sender's session has ended — action this the normal way."

    return {
        "outcome": outcome,
        "message": message,
        "address": address,
        "resolved_at": resolved_at,
    }


def build_competing_claim_judgment_point(
    competing_claim: dict[str, Any], evidence_pointer: str, resolves: list[str]
) -> Optional[dict[str, Any]]:
    """RETIRED (PM ruling 2026-07-24, pickup-skill-code-driven-branch-result
    spinoff) — always returns `None` now. A sibling handoff being held by a
    live peer on the SAME BRANCH is not contention: the tree is always noisy
    with concurrent-EM activity on a shared branch, and gating THIS
    artifact's pickup on THAT unrelated fact ("several sibling handoffs in
    this workstream are held by live peers, though none claims this file")
    produced exactly the noise/false-alarm the AC3/AC3e design was trying to
    avoid, just one layer up — the EM had to read and dismiss a stand-down
    question for handoffs it never touched. The only claim question that
    matters is whether THIS artifact itself is claimed — that's
    `gates.claim`/`gates.claim_grant`, computed and gated independently of
    this function. `compute_competing_claim`'s `gates.competing_claim` data
    is kept as non-blocking informational context (never removed, never
    surfaced as a judgment point) for the rare case an EM wants to look."""
    return None


def compute_addressee_gate(repo_root: Path, to_value: Optional[str]) -> dict[str, Any]:
    """M-addr — MECHANICAL: IN-PROCESS consult of the `memo.check_addressee`
    op's compute core (AC16 — consumed, not reimplemented).

    2026-07-26 subprocess-elision spinoff: the prior implementation shelled
    out to `cross-repo-memo --check-addressee`, itself a thin presentation
    shell over `memo.check_addressee` — a full child-interpreter round trip
    (~1098ms of a ~1192ms `brief()` end-to-end) back into code already
    importable in THIS process, and a violation of claude-klabauter's own "0
    subprocess resolution rungs on the hot path" budget. Now calls
    `compute_check_addressee_candidate` + `format_addressee_message`
    directly — no subprocess, no event loop (the op handler's body has no
    `await`, so `asyncio.run()` would be pure overhead over calling the sync
    core these two functions ARE, not a real async boundary).

    Captures the verdict text into `message` (contract § Exit-code contract:
    "the returned object on a `1` exit carries ... that gate's full offer
    text" — a bare exit code alone drops the specific self-EM/to-EM values
    the MISMATCH message names) — byte-for-byte the same `self:`/`to:`/
    `verdict:` three lines the CLI used to print to stdout, and the same
    0/3/4 (MATCH/MISMATCH/UNRESOLVED) exit-code ladder
    (`cross-repo-memo:4088-4105`).

    `repo_root` is normalized to the main worktree root before use —
    `compute_check_addressee_candidate`'s own docstring requires callers to
    pass a self_root already resolved this way
    (`memo_check_addressee.py:118-121`), and the OLD subprocess path got
    this normalization transitively and for free from the engine's own
    request dispatch (every op handler is `common_dir`-scoped server-side).
    Calling in-process bypasses that dispatch layer, so this function must
    do the normalization itself or silently mis-resolve self-identity for
    any linked-worktree invocation (2026-07-26 review finding 2).

    Normalization has a zero-subprocess fast path: `repo_root/.git` is a
    real DIRECTORY for a regular repo (the overwhelming common case — this
    IS the main worktree already, no normalization needed) and a FILE
    (`gitdir: ...` pointer) only for a linked worktree
    (`lifecycle.git_common_dir`'s own docstring). Only the linked-worktree
    case pays for `lifecycle.git_common_dir`'s `git rev-parse` subprocess
    (itself `lru_cache`d per `repo_root` past the first call) — this keeps
    the hot path's "0 subprocess resolution rungs" invariant
    (`TestComputeAddresseeGateNoSubprocess`) intact for every non-worktree
    invocation, the shape this function is actually called with on every
    `/pickup` run.

    Returns `{"exit_code": None, "checked": False}` when `to_value` is falsy,
    on a genuine registry-read failure (`RegistryReadError`/
    `AmbiguousReceiverError` — the in-process analog of the old "CLI missing
    at settings_home" / `OSError`/`SubprocessError` failure path: the gate
    must never take down `brief()`), or when `repo_root` is not inside a git
    repository (`git_common_dir` raises `RuntimeError` — same never-take-
    down-`brief()` posture). Otherwise
    `{"exit_code": <int>, "checked": True, "message": <str>}`.
    """
    if not to_value:
        return {"exit_code": None, "checked": False}
    if (repo_root / ".git").is_dir():
        # Already the main worktree root (standard-layout assumption
        # `main_worktree_root` itself documents) — skip the subprocess
        # entirely rather than pay to confirm what's already true.
        self_root = repo_root
    else:
        try:
            self_root = main_worktree_root(lifecycle.git_common_dir(repo_root))
        except RuntimeError:
            return {"exit_code": None, "checked": False}
    try:
        candidate = _compute_check_addressee_candidate(self_root, to_value)
    except (RegistryReadError, AmbiguousReceiverError):
        return {"exit_code": None, "checked": False}
    self_em = _resolve_self_em_id(self_root)
    message, exit_code = _format_addressee_message(self_em, self_root, to_value, candidate)
    return {"exit_code": exit_code, "checked": True, "message": message}


#: `compute_repo_identity_gate` verdict vocabulary — mirrors
#: `memo_check_addressee`'s existing three-valued ladder rather than
#: inventing a fourth string.
_REPO_IDENTITY_MATCH = "MATCH"
_REPO_IDENTITY_MISMATCH = "MISMATCH"
_REPO_IDENTITY_UNRESOLVED = "UNRESOLVED"


def _repo_identity_plausible_cwd(raw_cwd: Optional[str]) -> Optional[Path]:
    """Plausibility band gating MISMATCH (staff-eng re-review finding 0).

    A registry `cwd` value can be well-formed and type-correct while still
    being useless as positive evidence of a *different* real repo — the
    measured case is harness issue #27627, a version that wrote `cwd=/`.
    Before a failed containment check may produce MISMATCH, the anchor
    `cwd` must: exist as a directory, not be a filesystem root, and have a
    `.git` in itself or some ancestor. A `cwd` failing this band is not
    positive evidence of a different real repo — the rule this function
    exists to state: MISMATCH requires positive evidence of a different
    real repo; absence of that evidence is UNRESOLVED, never MISMATCH.

    Returns the resolved `Path` when the band is cleared, else `None`.
    """
    if not raw_cwd:
        return None
    try:
        candidate = Path(raw_cwd).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if not candidate.is_dir():
        return None
    if candidate.parent == candidate:
        # Filesystem root (POSIX `/`, or a Windows drive root like `C:\`).
        return None
    if not any((p / ".git").exists() for p in (candidate, *candidate.parents)):
        return None
    return candidate


def compute_repo_identity_gate(repo_root: Path, sid: Optional[str]) -> dict[str, Any]:
    """C1 — the shared repo-identity gate: MECHANICAL comparison of the
    session's harness-level launch anchor against the ceremony's suspect
    `repo_root`, zero-spawn.

    Spec backlink: `pln-a-ceremony-must-not-be-able-to-5e9421`
    § C1 (spike-verified anchor:
    `docs/research/spike-verdicts/2026-08-11-harness-session-registry-as-repo-identity-anchor.md`).
    Fixes the "sender closed a ceremony against the wrong repo after an
    uncaught tool-subprocess `cd`" incident this plan exists for.

    **Definition of "anchor":** the session's CURRENT harness-level `cwd` as
    recorded in the harness session registry — the value `/cd` moves, not a
    launch-immutable fact. **Coverage boundary, stated explicitly:**
    tool-subprocess `cd` drift IS caught (the reported incident); harness-
    level `/cd` is NOT caught, by design — the session genuinely moved, so a
    ceremony closed there afterward is a correct MATCH, not a gap.

    Composition:
      1. anchor — `session.harness_registry.self_record()` (the O(1)
         pid-keyed leg), falling back to a `snapshot()` lookup keyed by
         `sid` when `self_record()` returns `None` or its `sessionId` does
         not equal `sid` (per `docs/reference/harness-session-registry.md`
         § Two measured traps: "do not key on the filename PID alone" —
         this fallback is a directory scan, not zero-cost, but stays
         zero-spawn, which is all AC1 requires).
      2. trust check (AC10) — whichever record resolved must have
         `sessionId == sid` AND `session.core.stable_pid_alive(pid,
         stored_start_epoch)` must hold. On the `snapshot()`-fallback leg
         the `sessionId` check is tautological (the lookup key IS `sid`) —
         only `stable_pid_alive` is live there; the equality check has
         real bite only on the pid-keyed leg, where a stale or reused pid
         could otherwise carry an unrelated session's record. Either check
         failing on both legs is UNRESOLVED, never MATCH — the
         wrongful-takeover shape `harness_registry`'s own negative-spec
         defends against (`DoE-claude@642195ba` / `88929bea`).
      3. compare — CONTAINMENT, not equality: is the anchor `cwd` (at
         arbitrary depth — a launch from `<repo_root>/coordinator_core`
         measures exactly that) contained within `repo_root`? Resolved via
         `Path.resolve()` + `Path.is_relative_to`, the shape already used in
         `ops/fleet/archive_plans.py` and `ops/fleet/memo_send.py`.
         Deliberately NOT `_memo_resolver.same_repo_path` — that is exact
         directory equality and would refuse a subdirectory-launched
         session, a false positive worse than the fail-open it replaces.
      4. plausibility band (`_repo_identity_plausible_cwd`) — gates
         MISMATCH; see that function's docstring. A `cwd` failing the band
         falls to UNRESOLVED, never MISMATCH.
      5. verdict — MATCH | MISMATCH | UNRESOLVED, the same ladder
         `memo_check_addressee` already established.

    UNRESOLVED is a first-class outcome, not an error, and this function
    NEVER refuses on its own — refusal is entirely the caller's decision
    (see C2). UNRESOLVED is produced by: no registry record for this pid
    (`self_record()` miss with no `snapshot()` hit either), a record
    failing the AC10 cross-check on both legs, an unreadable/malformed
    record, an unresolvable `<claude-config>`, or an anchor `cwd` failing
    the plausibility band. Note for a future reader: the SUBAGENT case
    resolves through the dispatcher's own inherited `CLAUDE_PID`/`sid`
    rather than a registry record of its own — subagents never register,
    by harness design — so a subagent's gate call routes identically to
    its dispatcher's and is not a hole to "fix".

    **Known limitation, stated honestly, not engineered away:** the
    `snapshot()` fallback is lossy, not total. `snapshot()` is
    `sessionId`-keyed and documents last-writer-wins on a duplicate
    `sessionId` (unspecified `Path.glob` iteration order). A crash-then-
    resume reuses the same `sessionId` under a new pid, so a stale dead-pid
    record and the live one can share a key; if the stale one wins,
    `stable_pid_alive` correctly rejects it and the gate concludes
    UNRESOLVED — silently inert in this reachable, coin-flip-by-directory-
    order case. Accepted as a limitation of the fallback leg, not fixed
    here; the test surface constructs this case so it is observed.

    Never spawns a subprocess: `self_record()`/`snapshot()` are pure-Python
    file reads and `stable_pid_alive` is `psutil`-only, no `git`/`ps`/`wmic`
    shell-out anywhere in this call graph.

    Returns a dict carrying `verdict`, `session_root` (the anchor `cwd`,
    or `None` when unresolved), `resolved_root` (`repo_root`, stringified),
    `sid`, and a rendered `message` naming all three so a MISMATCH refusal
    is auditable (AC3) and an UNRESOLVED entry states plainly that the
    check could not run (AC4).
    """
    resolved_root = str(repo_root)

    def _verdict(verdict: str, session_root: Optional[str], detail: str) -> dict[str, Any]:
        message = (
            f"repo-identity: sid={sid} session_root={session_root} "
            f"resolved_root={resolved_root} verdict={verdict} — {detail}"
        )
        return {
            "verdict": verdict,
            "session_root": session_root,
            "resolved_root": resolved_root,
            "sid": sid,
            "message": message,
        }

    if not sid:
        return _verdict(_REPO_IDENTITY_UNRESOLVED, None, "no sid supplied")

    # --- 1. anchor: self_record() (O(1) pid-keyed leg), falling back to a
    # snapshot() scan by sid on a miss or sessionId mismatch.
    record_session_id: Optional[str] = None
    record: Optional[_harness_registry.RegistryRecord] = None

    self_hit = _harness_registry.self_record()
    if self_hit is not None and self_hit[0] == sid:
        record_session_id, record = self_hit
    else:
        fallback = _harness_registry.snapshot().get(sid)
        if fallback is not None:
            record_session_id, record = sid, fallback

    if record is None or record_session_id is None:
        # A registry that holds files but parses to nothing is a DIFFERENT
        # condition from one that parses fine and simply has no row for this
        # sid — the first is a parser/shape defect (see `harness_registry`'s
        # `procStart` note: an integer-only parser read every POSIX record as
        # unparseable and left this gate silently inert fleet-wide), the
        # second is the ordinary miss this arm was written for. Reporting
        # both as "0 parsed" would restate the defect's own camouflage.
        detail = "no registry record for this session"
        try:
            registry_dir = _harness_registry.registry_dir()
            if registry_dir is not None and registry_dir.is_dir():
                file_count = sum(1 for _ in registry_dir.glob("*.json"))
                if file_count > 0:
                    parsed_count = len(_harness_registry.snapshot())
                    detail = (
                        f"no registry record for this session "
                        f"(registry holds {file_count} file(s), {parsed_count} parsed)"
                    )
        except Exception:
            pass
        return _verdict(_REPO_IDENTITY_UNRESOLVED, None, detail)

    # --- 2. trust check (AC10) — sessionId equality (tautological on the
    # snapshot() fallback leg, live on the pid-keyed leg) AND
    # stable_pid_alive. Either failing is UNRESOLVED, never MATCH.
    if record_session_id != sid:
        return _verdict(_REPO_IDENTITY_UNRESOLVED, None, "registry record sessionId does not match sid")
    if not _session_core.stable_pid_alive(record.pid, stored_start_epoch=str(int(record.start_epoch))):
        return _verdict(_REPO_IDENTITY_UNRESOLVED, None, "registry record failed the stable_pid_alive trust check")

    # --- 3/4. compare by containment, gated by the plausibility band.
    plausible_cwd = _repo_identity_plausible_cwd(record.cwd)
    session_root_display = record.cwd

    try:
        resolved_repo_root = repo_root.resolve()
    except (OSError, RuntimeError):
        return _verdict(_REPO_IDENTITY_UNRESOLVED, session_root_display, "repo_root did not resolve")

    if plausible_cwd is not None:
        if plausible_cwd.is_relative_to(resolved_repo_root):
            return _verdict(_REPO_IDENTITY_MATCH, session_root_display, "anchor cwd is contained within repo_root")
        return _verdict(
            _REPO_IDENTITY_MISMATCH,
            session_root_display,
            "anchor cwd is a real, plausible directory outside repo_root",
        )

    # cwd absent or failed the plausibility band: absence of positive
    # evidence of a different real repo is UNRESOLVED, never MISMATCH.
    return _verdict(_REPO_IDENTITY_UNRESOLVED, session_root_display, "anchor cwd failed the plausibility band")


# ---------------------------------------------------------------------------
# Reply-closure check (2026-07-25 defect)
#
# `cross-repo/archive/2026-07-25-doe-claude-em-test-red-record-contract-
# consult.md`: an inbound `kind: consult` memo reached `status: actioned`
# with an `actioned_note` reading "Replied in full under the '## EM
# Response' heading in the memo body" — but the reply was written into
# CLAUDE-KLABAUTER'S OWN archived copy of the sender's memo, a file the sender
# (`doe-claude-em`) has no way to read. The sender's memo explicitly asked
# for a reply naming Q1/Q2/Q3's answers and their plan was blocked on it.
# Both terminal-memo emit sites in `brief()` reported `coast=clear`,
# `judgment_points=0`, "Nothing further to do" — the loop was wide open and
# the engine said it was closed. A human had to notice.
#
# The invariant: for an inbound memo whose `kind` is `consult`/`ask` (or
# absent — readers apply an `ask` default per `bin/cross-repo-memo.md`),
# `status: actioned` is necessary but NOT sufficient for closure. Closure
# additionally requires that an outbound memo from us reached the sender's
# repo. `kind: fyi` needs no reply and is genuinely terminal on `actioned`.
#
# 2026-07-25 re-entry: the first fix landed `from`+`created` co-occurrence
# as its "evidenced" bar. Run against the live memo above it returned
# `evidenced` with 28 candidates — every memo we sent `doe-claude-em` that
# day, because sender-id + date is not a discriminator on a busy fleet day.
# `evidenced` SUPPRESSES the judgment point, so on any day we sent that
# sender anything at all, an unanswered consult rendered as closed — the
# exact defect re-entering through the matching rule that was supposed to
# fix it. The correction: date is now a pre-filter only (and a cost
# control — it lets the full-text read below skip everything outside the
# window), never itself sufficient. `evidenced` additionally requires
# LINKAGE — an `in_reply_to` frontmatter match, or a textual citation of
# the inbound memo's basename/date-stripped-basename/tail-stem (see
# `_memo_reply_stems`) — via `_candidate_is_linked`. A date-filtered
# candidate that isn't linked no longer counts as evidence; it demotes the
# verdict to `open` and rides along as `unconfirmed_candidates` so
# `_render_reply_closure` can still cite it as "possible but unconfirmed"
# in the judgment point.
# ---------------------------------------------------------------------------

#: `compute_reply_closure` verdicts where the terminal "nothing further to
#: do" narration stands unchanged — `open`/`unknown` must never join this
#: set (that is the exact polarity inversion the 2026-07-25 defect was).
_REPLY_CLOSURE_TERMINAL_VERDICTS = frozenset({"not_required", "evidenced"})


def _parse_memo_date(value: Optional[str]) -> Optional[date]:
    """Best-effort `created:`-field parse (`YYYY-MM-DD`, extra trailing text
    ignored) — `None` on anything that doesn't parse, so a malformed date
    degrades to "can't compare" rather than a raised exception."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


#: 2026-07-25 second-pass defect (the FIRST fix's own live-fire check on the
#: memo it documents): `created`-date-only matching treats every memo we
#: happened to send the SAME sender on the SAME day as evidence — on the
#: originating case that was 28 same-day candidates, exactly one of which
#: was the real reply. `evidenced` then suppressed the judgment point,
#: which is the identical failure this whole check exists to close, now
#: re-entering through the matching rule instead of the kind/status check.
#: Date is therefore a FILTER (narrows the search), never itself evidence —
#: a candidate must additionally be LINKED to the inbound memo, either via
#: an `in_reply_to` frontmatter field (emitted since the 2026-07-25
#: write-side addition — `memo_draft._compose_memo` renders it and
#: `memo_send._validate_in_reply_to_exists` gates it at send time) or via
#: the candidate's own text citing the inbound memo's filename. The text-
#: citation path stays load-bearing for the pre-2026-07-25 corpus and for
#: replies sent without the flag.
_LEADING_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")

#: Narration/evidence-string display cap for unconfirmed (date-matched but
#: unlinked) candidates — a busy fleet day can produce dozens; the reason
#: string names the true total and truncates the cited list, never the
#: other way around.
_UNCONFIRMED_CITE_CAP = 5


def _inbound_link_stems(memo_path: str, from_id: str) -> tuple[str, str, str]:
    """The three basename-shaped strings a candidate reply must carry (via
    `in_reply_to`) or cite (in its own text) to count as LINKED rather than
    merely same-day.

    Returns `(basename, basename_no_ext, tail_stem)` — e.g. for
    `2026-07-25-doe-claude-em-test-red-record-contract-consult.md` sent by
    `doe-claude-em`: `("...consult.md", "...consult", "test-red-record-
    contract-consult")`. `tail_stem` strips the leading `YYYY-MM-DD-` date
    AND the `<from_id>-` sender segment — real replies often cite an
    ELIDED filename (the genuine reply that closed the originating defect
    opens "Reply to your `2026-07-25-…-test-red-record-contract-consult.md`",
    eliding the middle), and the tail stem is the part that survives that
    elision style.
    """
    basename = Path(memo_path).name
    basename_no_ext = basename[:-3] if basename.endswith(".md") else basename
    tail_stem = _LEADING_DATE_RE.sub("", basename_no_ext, count=1)
    sender_prefix = f"{from_id}-"
    if tail_stem.startswith(sender_prefix):
        tail_stem = tail_stem[len(sender_prefix):]
    return basename, basename_no_ext, tail_stem


#: 2026-07-25 third-pass defect: `_candidate_is_linked`'s substring scan had
#: no length floor on its needles. `tail_stem` strips the leading
#: `YYYY-MM-DD-` date AND the sender-id prefix off the basename, so a short
#: `--topic` slug degrades to a near-empty stem — found empirically when an
#: agent writing THIS feature's own tests used `2026-07-25-doe-claude-em-
#: m.md` as a fixture basename: `tail_stem` came out `"m"`, and `"m" in
#: candidate_text.lower()` is true of almost any prose, so deliberately
#: unrelated candidate memos matched as LINKED. Ten characters is the floor:
#: `basename`/`basename_no_ext` always clear it for free (the `YYYY-MM-DD-`
#: prefix alone is 10 chars before any slug), so in practice this only ever
#: excludes `tail_stem`, and only when the topic slug itself is under 10
#: chars — implausible for a real `--topic` (compare: the originating
#: memo's own tail stem, `test-red-record-contract-consult`, is 33 chars).
#: A 10-char needle is also long enough that an accidental substring match
#: in unrelated prose is implausible, while short enough that no realistic
#: topic slug gets excluded by it.
_MIN_LINK_STEM_LENGTH = 10


def _candidate_is_linked(candidate_text: str, candidate_fm_text: str, link_stems: tuple[str, str, str]) -> bool:
    """True when a same-sender, same-window candidate is actually LINKED to
    the inbound memo — `in_reply_to` naming it (by basename or
    basename-minus-`.md`), or the candidate's own text citing its basename
    or its date-and-sender-stripped tail stem. Case-insensitive throughout;
    deliberately an exact-substring test, not fuzzy/token-overlap scoring —
    a stem this specific needs no scoring to stay explainable. Any stem
    shorter than `_MIN_LINK_STEM_LENGTH` is skipped as a needle entirely
    (never treated as a match, never an error) — see that constant's
    comment for why a short stem is a false-positive risk, not evidence."""
    basename, basename_no_ext, tail_stem = link_stems
    in_reply_to = read_fm_field_unquoted(candidate_fm_text, "in_reply_to")
    if in_reply_to is not None:
        normalized = in_reply_to.strip().lower()
        if normalized in (basename.lower(), basename_no_ext.lower()):
            return True
    lowered = candidate_text.lower()
    return any(
        needle and len(needle) >= _MIN_LINK_STEM_LENGTH and needle.lower() in lowered
        for needle in (basename, basename_no_ext, tail_stem)
    )


def _format_unconfirmed_reason(unconfirmed: list[str], self_em_id: str, from_id: str, since: date, basename: str) -> str:
    total = len(unconfirmed)
    cited = unconfirmed[:_UNCONFIRMED_CITE_CAP]
    tail_note = f" (+{total - len(cited)} more)" if total > len(cited) else ""
    return (
        f"{total} memo(s) from '{self_em_id}' to '{from_id}' dated on/after {since.isoformat()}, "
        f"none citing '{basename}' (one of these may in fact BE the reply, sent without "
        f"--in-reply-to): {'; '.join(cited)}{tail_note}"
    )


def compute_reply_closure(frontmatter: dict[str, Any], memo_path: str, repo_root: Path) -> dict[str, Any]:
    """Reply-closure predicate for a terminal (`status: actioned`) inbound
    memo — the fix for the 2026-07-25 defect documented above, hardened
    against the date-only-matching second-pass defect documented just
    above `_inbound_link_stems`.

    Returns `{"verdict": ..., "reason": Optional[str], "candidates": [...],
    "unconfirmed_candidates": [...]}` with exactly four possible verdicts:

      - `not_required` — `kind: fyi`. No reply is expected; the terminal
        verdict stands.
      - `evidenced` — one or more candidate reply memos found in the
        sender's own `cross-repo/inbox/` or `cross-repo/archive/` tree,
        stamped `from: <this repo's EM id>`, `created` on or after the
        inbound memo's `created`, AND LINKED to the inbound memo (see
        `_candidate_is_linked`). `candidates` names only the linked ones
        (repo-relative to the SENDER's tree, with that tree's absolute root
        carried alongside as `sender_root` so a rendered citation can
        qualify them); `unconfirmed_candidates` is empty in this verdict.
      - `open` — a reply is required (`kind` is `consult`/`ask`/absent) and
        either (a) zero same-sender/same-window candidates exist at all, or
        (b) one or more exist but NONE are linked — `unconfirmed_candidates`
        carries those in case (b) so the EM can eyeball them; `reason`
        distinguishes the two sub-cases in wording.
      - `unknown` — a reply is required but the check could not run: the
        sender repo is unresolvable/absent on this machine, the sender repo
        has no `cross-repo/` tree at all, the inbound memo's own `from`/
        `created` fields are missing, or `created` does not parse.

    `unknown` is deliberately NOT folded into `open` or `not_required` —
    this is a closure check on a *suppression* path (it decides whether
    "nothing further to do" gets printed), so fail-open-to-noise is the
    correct polarity: an `unknown` that silently renders as closed
    reproduces the exact defect this function exists to fix. Callers
    (`_render_reply_closure`) MUST surface `unknown` as a judgment point,
    worded distinctly from `open`.
    """
    kind = frontmatter.get("kind")
    if kind == "fyi":
        return {"verdict": "not_required", "reason": None, "candidates": [], "unconfirmed_candidates": []}

    from_id = frontmatter.get("from")
    created_raw = frontmatter.get("created")
    inbound_created = _parse_memo_date(created_raw)
    if not from_id or not created_raw:
        return {
            "verdict": "unknown",
            "reason": f"'{memo_path}' frontmatter is missing 'from' and/or 'created' — cannot search for a reply.",
            "candidates": [],
            "unconfirmed_candidates": [],
        }
    if inbound_created is None:
        return {
            "verdict": "unknown",
            "reason": f"'{memo_path}' has an unparseable 'created' value ({created_raw!r}) — cannot date-filter replies.",
            "candidates": [],
            "unconfirmed_candidates": [],
        }

    try:
        _inbox_dir, sender_root, _all_repos = resolve_receiver_inbox(from_id)
    except (RegistryReadError, AmbiguousReceiverError) as exc:
        return {
            "verdict": "unknown",
            "reason": f"machine-local registry lookup for '{from_id}' failed: {exc}",
            "candidates": [],
            "unconfirmed_candidates": [],
        }
    if sender_root is None or not sender_root.is_dir():
        return {
            "verdict": "unknown",
            "reason": f"sender repo for '{from_id}' is not registered (or not present) on this machine.",
            "candidates": [],
            "unconfirmed_candidates": [],
        }

    cross_repo_dir = sender_root / "cross-repo"
    if not cross_repo_dir.is_dir():
        return {
            "verdict": "unknown",
            "reason": f"'{sender_root}' has no cross-repo/ tree — cannot search for a reply.",
            "candidates": [],
            "unconfirmed_candidates": [],
        }

    # This repo's own EM id, via THE ONE self-identity resolver
    # (`_memo_resolver.resolve_self_em_id` — registered-repo path match,
    # else the `basename + '-em'` convention fallback `compute_tree_
    # quiescence`'s sibling resolution also relies on). `compute_addressee_
    # gate`'s `self:` display line uses the same resolver — do not paste a
    # second copy of this derivation.
    self_em_id = _resolve_self_em_id(repo_root)
    link_stems = _inbound_link_stems(memo_path, from_id)
    inbound_basename = link_stems[0]

    linked: list[str] = []
    unconfirmed: list[str] = []
    for search_dir in (cross_repo_dir / "inbox", cross_repo_dir / "archive"):
        if not search_dir.is_dir():
            continue
        for candidate_path in sorted(search_dir.rglob("*.md")):
            try:
                text = candidate_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            split = split_frontmatter(text)
            if split is None:
                continue
            # Cheap frontmatter-only filters (sender, then date) BEFORE the
            # full-text citation scan below — the citation scan only ever
            # runs for a candidate that already passed both.
            candidate_from = read_fm_field_unquoted(split.fm_text, "from")
            if candidate_from != self_em_id:
                continue
            candidate_created = _parse_memo_date(read_fm_field_unquoted(split.fm_text, "created"))
            if candidate_created is None or candidate_created < inbound_created:
                continue
            rel = rel_id(candidate_path, sender_root)
            if _candidate_is_linked(text, split.fm_text, link_stems):
                linked.append(rel)
            else:
                unconfirmed.append(rel)

    if linked:
        return {
            "verdict": "evidenced",
            "reason": None,
            "candidates": linked,
            "unconfirmed_candidates": [],
            "sender_root": str(sender_root),
        }
    if unconfirmed:
        return {
            "verdict": "open",
            "reason": _format_unconfirmed_reason(unconfirmed, self_em_id, from_id, inbound_created, inbound_basename),
            "candidates": [],
            "unconfirmed_candidates": unconfirmed,
        }
    return {
        "verdict": "open",
        "reason": (
            f"no reply from '{self_em_id}' dated on/after {inbound_created.isoformat()} "
            f"found under '{sender_root}'/cross-repo/{{inbox,archive}}."
        ),
        "candidates": [],
        "unconfirmed_candidates": [],
    }


def _render_reply_closure(
    closure: dict[str, Any],
    memo_path: str,
    base_narration: str,
    base_next_move: str,
    status: Optional[str] = None,
) -> tuple[list[dict[str, Any]], str, str]:
    """Folds a `compute_reply_closure` verdict onto a terminal memo's
    narration/next_move/judgment_points triple.

    THE single rendering site both terminal-memo emit branches in `brief()`
    (archived-fallback and actioned-in-place) call — do NOT add a second
    copy of this rendering logic at either call site (the shape this
    module's brief calls out repeatedly to avoid); pass the pre-existing
    narration/next_move strings in as parameters when the two sites need
    different wording, per the 2026-07-25 dispatch brief.

    `not_required`/`evidenced` return the base narration/next_move
    unchanged (byte-identical for `not_required`; `evidenced` appends a
    citation of the candidate reply path(s)) and an empty judgment-points
    list — today's behavior is preserved. `open`/`unknown` replace
    `next_move` with an actionable one and append exactly one judgment
    point, so `judgment_points` is non-zero and `gates.coast` is no longer
    trivially `"clear"` (`compute_coast` flips to `"blocked"` on any id'd
    judgment point).
    """
    verdict = closure["verdict"]
    if verdict in _REPLY_CLOSURE_TERMINAL_VERDICTS:
        if verdict == "evidenced":
            # Candidates are repo-relative to the SENDER's tree, not this
            # one — an unqualified citation sends the reader looking in the
            # receiver's own cross-repo/ and finding nothing.
            sender_root = closure.get("sender_root")
            cited = "; ".join(
                f"{sender_root}/{c}" if sender_root else c for c in closure["candidates"]
            )
            return [], f"{base_narration} Reply evidenced at: {cited}.", base_next_move
        return [], base_narration, base_next_move

    status_display = status if status else "unknown"
    inbound_basename = Path(memo_path).name
    cli_hint = (
        'the cross-repo-memo CLI ("${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}'
        '/bin/cross-repo-memo") — never hand-write a memo file into the receiver\'s tree, which '
        "silently bypasses the summary cap and frontmatter shape every engine path enforces."
    )
    in_reply_to_note = (
        f"pass --in-reply-to {inbound_basename} — the linkage scan cannot confirm the reply "
        "otherwise and this same judgment point will re-fire on the next pickup."
    )
    # `required_content_keys` is stamped EXPLICITLY empty rather than left
    # absent (defect 2, 2026-07-29): neither disposition maps to a
    # `--decision` at all, so neither needs a content key — but an absent
    # field and an empty one read identically to an operator scanning the
    # decision object, which is the same "can't tell required-nothing from
    # declares-nothing" ambiguity the field exists to remove. These are
    # hand-built literals rather than `_KIND_DISPOSITIONS` entries, so
    # `_dispositions_with_required_keys` does not reach them.
    dispositions = [
        {"value": "send-reply", "resolves": [], "required_content_keys": []},
        {"value": "already-replied-elsewhere", "resolves": [], "required_content_keys": []},
    ]
    if verdict == "open":
        jp = build_judgment_point(
            "j-reply-closure",
            f"'{memo_path}' is status: {status_display} but no reply to the sender was found in their working tree — send one?",
            closure["reason"],
            dispositions,
            {
                "disposition": "send-reply",
                "rationale": (
                    "an ask/consult memo is not closed until the sender has a reply in their own "
                    f"tree — status: {status_display} only marks OUR side of the exchange done."
                ),
            },
        )
        narration = (
            f"{base_narration} status: {status_display} is necessary but NOT sufficient for an "
            f"ask/consult memo — {closure['reason']}"
        )
        next_move = (
            f"Send the reply via {cli_hint} — {in_reply_to_note}"
        )
        return [jp], narration, next_move

    # verdict == "unknown" — reply required but the check itself could not
    # run; render distinctly from "open" (a confirmed-missing reply) so the
    # EM reads why closure is uncertain rather than mistaking it for the
    # confirmed-open case.
    jp = build_judgment_point(
        "j-reply-closure",
        f"Could not confirm whether '{memo_path}' was actually replied to — reply-closure check did not run to completion.",
        closure["reason"],
        dispositions,
        None,
        reason="insufficient-evidence",
    )
    narration = (
        f"{base_narration} status: {status_display} is necessary but NOT sufficient for an ask/consult "
        f"memo, and the reply-closure check could not confirm a reply reached the sender: "
        f"{closure['reason']}"
    )
    next_move = (
        f"Confirm by hand whether the sender already has a reply, or send one via {cli_hint} — "
        f"{in_reply_to_note}"
    )
    return [jp], narration, next_move


# ---------------------------------------------------------------------------
# directives[] / judgment_points[] assembly
# ---------------------------------------------------------------------------

def build_handoff_directives(
    artifact_path: str,
    claim_holder: Optional[str],
    basename: str,
    self_claimed_in_frontmatter: bool = False,
) -> list[dict[str, Any]]:
    """`self_claimed_in_frontmatter` (2026-07-29 self-claim-idempotence fix)
    — True only when THIS session already holds the claim (`claim_grant.
    held_by_self`) AND the artifact's own frontmatter already carries
    `status: claimed` — i.e. `d2` (the archive-stamp-cli frontmatter
    mutation) already landed in a prior pass. Defaults to `False` so a
    caller that hasn't computed it yet degrades to the pre-existing
    unconditional-`False` behavior, never to a silently-assumed-satisfied
    directive."""
    directives: list[dict[str, Any]] = [
        {
            "id": "d1",
            "cli": "session-claim-cli",
            "args": ["claim-artifact", "handoff", basename],
            "depends_on": None,
            "already_satisfied": claim_holder is not None,
        },
        {
            "id": "d2",
            "cli": "archive-stamp-cli",
            "args": ["claim-handoff", artifact_path],
            # Review: code-reviewer — Finding 4: every brief() call site overwrites
            # this default explicitly, so an un-reassigned case now degrades to
            # "unconditional" (visible) rather than a stale "j1" literal that may
            # not even be in scope. Matches build_memo_directives's default.
            "depends_on": None,
            "already_satisfied": self_claimed_in_frontmatter,
        },
    ]
    return directives


#: C8 BUILD (1) — per-`(kind_resolved, disposition_value)` mapping from an
#: action-taking `j-kind` disposition to `cs_action_memo`'s `--decision` mode
#: (mirrors `ops/memo_transition.py::_action`'s own enum: accepted/partial/
#: declined). Not every `_KIND_DISPOSITIONS` entry that resolves
#: `d-action-memo` (C8 BUILD (3)) appears here: `fyi`/`ack-nil`,
#: `consult`/`reply-short`, `consult`/`reply-long`, and `proposal`/`negotiate`
#: also resolve `d-action-memo` but are deliberately absent from this map —
#: none of nil-impact / replying-in-place / negotiating is an
#: accepted/partial/declined outcome, so each takes `_build_action_memo_
#: args`'s `--actioned-note`-only path instead of a `--decision` mapping.
#: Every disposition that keeps `resolves: []` needs no entry here
#: (`d-action-memo` never fires for it). `accept-escalate-to-sizing` maps to
#: `partial` (the memo itself is only partially actioned in-line; the sizing
#: object it escalates to carries the rest) — `ask`/`decline` and
#: `proposal`/`decline` map to `declined` (`ops/memo_transition.py`'s decision
#: enum accepts `declined` and does not require `--realized-by` for it,
#: unlike accepted/partial) — every other mapped entry maps to `accepted`
#: (fully actioned in this pass).
#:
#: Renamed 2026-08-03 (PM-ratified, doe-claude-em cross-repo memo:
#: "escalate to sizing is better than escalate to plan"): the disposition
#: value formerly named `accept-escalate-to-plan` is now
#: `accept-escalate-to-sizing`, straight rename with no alias — nothing
#: validates a *stored* historical value against this map (it's consulted
#: only at decision time from a live disposition choice,
#: `_build_action_memo_args`), so an alias would be dead code. A landed
#: record containing the literal `accept-escalate-to-plan` (e.g.
#: `docs/plans/2026-07-25-install-surface-freshness-classification.md`,
#: `archive/specs/2026-07/2026-07-25-cockpit-contract-release-publish-directive.md`)
#: refers to this same former value.
_MEMO_ACTION_DECISION_MAP: dict[tuple[str, str], str] = {
    ("ask", "accept-mechanical-direct"): "accepted",
    ("ask", "accept-escalate-to-sizing"): "partial",
    ("ask", "decline"): "declined",
    ("proposal", "adopt"): "accepted",
    ("proposal", "decline"): "declined",
    ("fyi", "surgical-fix"): "accepted",
}


def _build_action_memo_args(artifact_path: str, kind_resolved: str, decisions: dict[str, Any]) -> list[str]:
    """C8 BUILD (5) — the EM-content channel: resolves `decisions["j-kind"]`
    (the EM's recorded disposition, plus whatever content keys accompany it
    — `decision_note`/`realized_by`/`actioned_note`/`distill_fate`/
    `in_repo_capture`) into
    `cs_action_memo`'s CLI-flag surface (`_DISPOSITION_FLAGS`,
    `archive_stamp.py:815-821`). The EM supplies only content; this function
    supplies the command syntax — mirrors `ops/memo_transition.py::_action`'s
    own contract verbatim (`--decision`/`--actioned-note` mutually exclusive,
    `--realized-by` required for accepted/partial).

    Degrades gracefully when `decisions` is empty/absent (the pre-decision
    `brief()` call) or when a content key the EM hasn't supplied yet is
    missing — the resulting directive is inert until `_KIND_DISPOSITIONS`
    actually resolves it (C8 BUILD (3)), so an incomplete arg list here never
    fires anything; it only fails loud, correctly, if dispatched anyway
    without the required content (`cs_action_memo`'s own precondition)."""
    jkind = decisions.get("j-kind") if isinstance(decisions, dict) else None
    jkind = jkind if isinstance(jkind, dict) else {}
    disposition = jkind.get("disposition")
    args = ["action-memo", artifact_path]
    # `disposition` is EM-supplied JSON, so it is only a lookup key when it is
    # actually a string — a non-string could never match this map's
    # `tuple[str, str]` keys anyway, so the guard changes no behaviour.
    decision_value = (
        _MEMO_ACTION_DECISION_MAP.get((kind_resolved, disposition))
        if isinstance(disposition, str)
        else None
    )
    if decision_value is not None:
        # Defect fix (2026-07-25, live repro this session): `--decision` and
        # `--actioned-note` are mutually exclusive on `cs_action_memo`'s own
        # contract (`ops/memo_transition.py:563-564`) — a decision-mapped
        # disposition takes content via `decision_note`, never
        # `actioned_note`. Previously an EM-supplied `actioned_note` on this
        # branch was silently never read, so the reasoning behind an
        # `accept-mechanical-direct` (etc.) decision vanished with no error.
        # Fail loud instead (mirrors the `--decisions` wrong-shape precedent,
        # `072ae91c`) rather than silently routing it to `--decision-note` —
        # the two note keys are genuinely different channels on
        # `cs_action_memo`'s contract, and aliasing one to the other would
        # hide the EM's key-choice mistake rather than surface it.
        if jkind.get("actioned_note"):
            raise ValueError(
                f"_build_action_memo_args: decisions['j-kind'] carries both a "
                f"decision-mapped disposition {disposition!r} (kind={kind_resolved!r}) "
                f"and 'actioned_note' — 'actioned_note' is for nil-impact dispositions "
                f"only (fyi/ack-nil-shaped, no --decision). Supply the reasoning via "
                f"'decision_note' instead for this disposition."
            )
        args += ["--decision", decision_value]
        realized_by = jkind.get("realized_by")
        if realized_by:
            args += ["--realized-by", realized_by]
        decision_note = jkind.get("decision_note")
        if decision_note:
            args += ["--decision-note", decision_note]
    elif disposition is not None:
        # Live path for `fyi`/`ack-nil` (and any future disposition that
        # resolves `d-action-memo` without a `_MEMO_ACTION_DECISION_MAP`
        # entry): records `actioned_note` alone, no `--decision` — nil-impact
        # is not an accepted/partial/declined outcome. `cs_action_memo`
        # itself fails loud if `actioned_note` is empty here (mutual
        # requirement with `--decision`, `ops/memo_transition.py:564-580`);
        # `ack-nil`'s guidance text tells the EM to supply it.
        actioned_note = jkind.get("actioned_note")
        if actioned_note:
            args += ["--actioned-note", actioned_note]
    distill_fate = jkind.get("distill_fate")
    if distill_fate:
        args += ["--distill-fate", distill_fate]
    in_repo_capture = jkind.get("in_repo_capture")
    if in_repo_capture:
        args += ["--in-repo-capture", in_repo_capture]
    return args


def build_memo_directives(
    artifact_path: str, kind_resolved: str = "ask", decisions: Optional[dict[str, Any]] = None
) -> list[dict[str, Any]]:
    """C8 BUILD (1)+(2)+(5)ii — emits the memo terminal-execution path:

    - `d1` (unchanged) — the lock-dir claim, unconditional grab mechanics
      (never `j-kind`-gated).
    - `claim-memo-stamp` — the memo-side write of C7 Part A's frontmatter
      claim-stamp state machine (`status: open -> in_progress`). Carries the
      SAME `depends_on` as `d1` (liveness-`j1`-or-`None`) — this is grab
      mechanics too, set by the caller alongside `d1["depends_on"]`, never
      disposition-gated. Idempotence (C8 BUILD (7)): relies on
      `cs_claim_memo_stamp`/`_claim`'s own no-op-on-self-reclaim behaviour
      (`memo_transition._claim:453-459`) rather than computing its own
      `already_satisfied` — stated choice, not an oversight.
    - `d-action-memo` — the disposition-gated terminal write
      (`archive-stamp-cli action-memo`). `depends_on: "j-kind"` always; only
      fires when the EM's `j-kind` disposition is one of the four
      `_MEMO_ACTION_DECISION_MAP`/`_KIND_DISPOSITIONS` action-taking values
      (C8 BUILD (3)). `args` are fully resolved from `decisions` via
      `_build_action_memo_args` — no command syntax is left for the EM to
      supply.
    """
    decisions = decisions if isinstance(decisions, dict) else {}
    return [
        {
            "id": "d1",
            "cli": "session-claim-cli",
            "args": ["claim-artifact", "memo", Path(artifact_path).name],
            "depends_on": None,
            "already_satisfied": False,
        },
        {
            "id": "claim-memo-stamp",
            "cli": "archive-stamp-cli",
            "args": ["claim-memo-stamp", artifact_path],
            "depends_on": None,
            "already_satisfied": False,
        },
        {
            "id": "d-action-memo",
            "cli": "archive-stamp-cli",
            "args": _build_action_memo_args(artifact_path, kind_resolved, decisions),
            "depends_on": "j-kind",
            "already_satisfied": False,
        },
    ]


#: The two `reason` values a null `recommendation` may carry (contract §
#: "recommend-for-you tier", the Director of Engineering review F4). `insufficient-evidence` means
#: the engine has no basis to narrow the call; `recommendation-forbidden`
#: means it is structurally barred from offering one regardless of evidence
#: quality — see `build_untrusted_gate_judgment_point`.
_NULL_RECOMMENDATION_REASONS = frozenset({"insufficient-evidence", "recommendation-forbidden"})


def build_judgment_point(
    id: str,
    question: str,
    evidence: str,
    dispositions: list[dict[str, Any]],
    recommendation: Optional[dict[str, str]],
    *,
    round_trip: str = "terminal",
    revalidate_at_dispatch: bool = False,
    reason: Optional[str] = None,
) -> dict[str, Any]:
    """The general `judgment_points[]` entry constructor (AC5b, the Director of Engineering review
    F4) — `recommendation` is a REQUIRED positional parameter with no
    default, so a call site that forgets to decide fails loud as a
    `TypeError` at authoring time rather than silently shipping a judgment
    point with an absent decision.

    This is ergonomics, not the enforcement mechanism: a raw dict literal
    still bypasses this constructor entirely, which is why `_emit()` (C1c)
    carries the actual backstop that validates every emitted
    `judgment_points[]` entry regardless of how it was built.

    A null `recommendation` must carry a `reason` of `insufficient-evidence`
    or `recommendation-forbidden` (contract § "recommend-for-you tier") — an
    engine-computed judgment point that recognizes it cannot narrow a call
    is a different thing from one structurally barred from trying, and both
    read differently to the EM resolving it. A non-null `recommendation`
    carries only `disposition`/`rationale` — no `confidence` (dropped from
    the schema entirely, the Director of Engineering review F10: the least-checkable token in the
    surface, with no consumer that may act on it — AC5d already forbids the
    one automated reader that would). The shared seam below is what
    enforces this shape; this module carries no field-set copy of its own.

    For evidence sourced from branch-writable content this engine did not
    itself compute (a memo/handoff body quoted verbatim into `evidence`),
    use `build_untrusted_gate_judgment_point` instead — that constructor has
    no `recommendation` parameter at all, closing off the call shape rather
    than trusting every caller to pass `None`.

    Composes `contract/decision_object/judgment.py::build_judgment_point`
    (the shared seam) for the actual dict construction and the
    `{disposition, rationale}`-shape/extra-field check on a non-null
    `recommendation` (that seam's own `_RECOMMENDATION_FIELDS` is the sole
    copy now — this wrapper does not duplicate it) -- this wrapper keeps
    only the `reason`-enum check the shared seam has no vocabulary for
    (2026-08-15 judgment-points plan,
    C6: brings this module's fork under the same census as every other
    assembler rather than leaving it duplicated).
    """
    if recommendation is None:
        if reason not in _NULL_RECOMMENDATION_REASONS:
            raise ValueError(
                "build_judgment_point: a null recommendation requires reason "
                f"'insufficient-evidence' or 'recommendation-forbidden', got {reason!r}"
            )
    elif reason is not None:
        raise ValueError("build_judgment_point: reason only accompanies a null recommendation")
    return _shared_build_judgment_point(
        recommendation,
        id=id,
        question=question,
        dispositions=dispositions,
        evidence=evidence,
        reason=reason,
        revalidate_at_dispatch=revalidate_at_dispatch,
        round_trip=round_trip,
    )


def build_untrusted_gate_judgment_point(
    id: str,
    question: str,
    evidence: str,
    dispositions: list[dict[str, Any]],
    *,
    round_trip: str = "terminal",
    revalidate_at_dispatch: bool = False,
) -> dict[str, Any]:
    """The judgment-point constructor for evidence sourced from
    branch-writable content this engine did not itself compute (a
    memo/handoff body quoted verbatim into `evidence`) — the Director of Engineering's
    discriminator: "can the thing being recommended about influence the
    recommendation?" Here the answer is yes, so recommending is structurally
    forbidden rather than merely discouraged, mirroring
    `build_completeness_checklist`'s existing `resolves: []`
    structural-unreachability shape for its probe-confirmation gate.

    Carries NO `recommendation` parameter — a caller cannot pass one even by
    mistake, a type-level guarantee rather than a runtime assertion on one
    gate. Always emits `recommendation: None`, `reason:
    "recommendation-forbidden"`.

    Composes the shared seam's own `build_untrusted_gate_judgment_point`
    (C6, see `build_judgment_point` above) for the dict construction --
    this module's positional signature and default `revalidate_at_dispatch`
    are preserved by this wrapper, not by a second implementation.
    """
    return _shared_build_untrusted_gate_judgment_point(
        id=id,
        question=question,
        dispositions=dispositions,
        evidence=evidence,
        reason="recommendation-forbidden",
        revalidate_at_dispatch=revalidate_at_dispatch,
        round_trip=round_trip,
    )


def build_liveness_judgment_point(liveness_signal_fired: bool, evidence_pointer: str, resolves: list[str]) -> Optional[dict[str, Any]]:
    """The single JUDGMENT entry every MECHANICAL liveness-signal computation
    feeds (contract § "One coherent model for positive-liveness") — a firing
    signal never auto-directs a stand-down. Returns None when the signal did
    not fire (nothing to offer a judgment on).

    Tier: `insufficient-evidence` — a live-signal firing is evidence a peer
    may be active, not a disposition on whether to stand down; the engine
    genuinely cannot narrow that call. Not `recommendation-forbidden`: the
    evidence pointer (`gates.liveness_signal`) is engine-computed session
    state, not branch-writable content quoted verbatim.

    AMENDMENT 2026-07-24 (chunk C7 Part A4) — no longer
    `revalidate_at_dispatch: true`. That flag existed to re-check NOISY
    signal-(b)/(c) inference right before mutating; `compute_liveness_signal`
    now reads a durable committed frontmatter stamp (see its own docstring),
    which is stable across the brief-to-apply gap, so a recorded `proceed`
    disposition on this judgment point is honored rather than discarded and
    recomputed at dispatch. `apply` already re-resolves `claim_grant`
    (`apply.py:_resolve_claim_grant`) immediately before mutating — the
    atomic side-file DENIED gate remains the hard mutual-exclusion backstop
    that catches a peer who grabbed the lock in the interim; this flag drop
    does not weaken that gate."""
    if not liveness_signal_fired:
        return None
    return build_judgment_point(
        "j1",
        "Any peer live on this handoff/plan? Stand down?",
        evidence_pointer,
        [
            {"value": "proceed", "resolves": resolves},
            {"value": "stand-down-and-surface", "resolves": []},
        ],
        None,
        reason="insufficient-evidence",
    )


def compute_gate_shipped_blocker_evidence(
    repo_root: Path, gate_evidence: Optional[dict[str, Any]]
) -> Optional[dict[str, Any]]:
    """Narrow `jgate` evidence enrichment (plan 2026-08-08-the-engine-asks-
    for-facts-it-already-holds, chunk C7) — reads ONLY this handoff's own
    `gate_evidence` frontmatter field (schema § `gate_evidence`), never a
    corpus walk of sibling handoffs (live or archived, contract negative
    spec 2). `gate_evidence` is authored directly onto THIS record by the
    same hand that wrote `gate_dependency`/`blocked_by`, so reading it costs
    nothing beyond the git read this module already performs elsewhere
    (`compute_premise_checks`'s `sha` premise kind is the same shape, same
    `_run_git` choke point — never raises).

    Returns evidence for the FIRST `legs[]` entry with `kind ==
    "commit-sha"` whose `ref` resolves via `git cat-file -e` in this repo,
    but ONLY when `covers_prose` is explicitly `True` (schema rule 0 — an
    absent or `False` `covers_prose` leaves every leg inert, so this
    function must not manufacture a recommendation the schema itself
    would treat as non-authoritative). Returns `None` on any of: no
    `gate_evidence`, `covers_prose` not `True`, no resolvable `commit-sha`
    leg — every one of those is a silent degrade to today's
    insufficient-evidence brief, never a dangling-ref assertion: this
    function only ever adds POSITIVE evidence when a sha genuinely
    resolves, and never claims a blocker is unshipped."""
    if not isinstance(gate_evidence, dict):
        return None
    if gate_evidence.get("covers_prose") is not True:
        return None
    legs = gate_evidence.get("legs")
    if not isinstance(legs, list):
        return None
    for leg in legs:
        if not isinstance(leg, dict) or leg.get("kind") != "commit-sha":
            continue
        ref = leg.get("ref")
        if not isinstance(ref, str) or not ref.strip():
            continue
        sha = ref.strip()
        result = _run_git(["cat-file", "-e", sha], repo_root)
        if result.returncode == 0:
            return {"leg_id": leg.get("leg_id", "<unknown>"), "sha": sha}
    return None


#: Piece B guidance strings for `jgate`'s two dispositions (message
#: register: one fact, once, plus a terse alternative — no reassurance, no
#: repetition). `blocked_by`/`blocking_notes` are the two fields a claim
#: strands unread if answered "cleared" without `gate-recheck` also
#: recording the clearance (Piece A) — naming them here is what converts
#: the silent orphaning the memo describes into a visible one.
# Review: staff-eng — replaced the obsolete/factually-wrong version (it
# described gate-recheck as needing a manual follow-on pass and as the
# owner of retiring blocked_by; neither is true post-Piece-A).
_JGATE_CLEARED_GUIDANCE = (
    "Answer from gates.gate_check.blocked_by / .blocking_notes / "
    ".gate_evidence, not gate_dependency prose. blocked_by is retired "
    "separately by reconcile-open, not by clearing."
)
_JGATE_NOT_CLEARED_GUIDANCE = (
    "Leave deployment_state:awaiting_gate — claim-handoff will not fire. "
    "Re-run pickup-assemble once the blocker resolves."
)


def build_gate_check_judgment_point(
    evidence_pointer: str, resolves: list[str], recommendation: Optional[dict[str, str]] = None
) -> dict[str, Any]:
    """The `awaiting_gate` JUDGMENT entry (contract § JUDGMENT checklist,
    "Has this `awaiting_gate` handoff's gate actually cleared?") — the engine
    surfaces the gate-dependency content and the aging verdict as evidence
    (`gates.gate_check`); whether the gate has genuinely cleared stays the
    EM's read, never a mechanized parse (dispatch brief defect 3).

    Tier: `insufficient-evidence` — the evidence pointer names engine-read
    gate-dependency/aging content, not a verbatim quote of untrusted body
    text, but reading whether the named gate has actually cleared is
    exactly the judgment this module never mechanizes.

    `recommendation` (plan 2026-08-08-the-engine-asks-for-facts-it-already-
    holds, chunk C7) is an OPTIONAL enrichment — `None` by default,
    preserving the exact `insufficient-evidence` shape every prior caller
    relied on. When the caller has resolved `compute_gate_
    shipped_blocker_evidence` to a positive hit, it passes a
    `disposition`/`rationale` dict here instead, so the EM reads a
    substantiated point rather than a bare ask. This is ALWAYS additive to
    the evidence, never a suppression: the judgment point is still emitted
    unconditionally either way (contract negative spec 1), the
    `dispositions`/`resolves` lists below are unchanged by which branch
    fires, and the EM still resolves it — a recommendation narrows what is
    read, never what is decided.

    Piece B (cross-repo/inbox/2026-08-04-example-market-data-repo-em-pickup-
    jgate-cleared-strands-gate-fields.md) — both dispositions carry a
    `guidance` string, `jshipped`'s in-repo model (`build_shipped_state_
    judgment_point`, above). The widened `gates.gate_check` bundle this
    evidence pointer resolves to now also carries `blocked_by`/
    `blocking_notes`/`gate_evidence` (see the `awaiting_gate` branch in
    `_brief_for_artifact` that builds it) — `cleared` is answered directly
    against the same two fields (`blocked_by`, `blocking_notes`) that a
    claim strands unless `gate-recheck` also runs (Piece A, this same
    disposition's `resolves` list)."""
    if recommendation is not None:
        return build_judgment_point(
            "jgate",
            "Has this awaiting_gate handoff's gate actually cleared?",
            evidence_pointer,
            [
                {"value": "cleared", "resolves": resolves, "guidance": _JGATE_CLEARED_GUIDANCE},
                {
                    "value": "not-cleared",
                    "resolves": [],
                    "guidance": _JGATE_NOT_CLEARED_GUIDANCE,
                },
            ],
            recommendation,
        )
    return build_judgment_point(
        "jgate",
        "Has this awaiting_gate handoff's gate actually cleared?",
        evidence_pointer,
        [
            {"value": "cleared", "resolves": resolves, "guidance": _JGATE_CLEARED_GUIDANCE},
            {"value": "not-cleared", "resolves": [], "guidance": _JGATE_NOT_CLEARED_GUIDANCE},
        ],
        None,
        reason="insufficient-evidence",
    )


def build_shipped_state_judgment_point(evidence_pointer: str, resolves: list[str]) -> dict[str, Any]:
    """The `deployment_state: shipped` JUDGMENT entry (2026-07-25 defect fix —
    a handoff already stamped shipped previously briefed as freely
    dispatchable, `gates.coast.verdict == "clear"`, `judgment_points: []`,
    telling a peer session to redo finished work). A shipped baton is
    presumptively done, but re-opening one is legitimate (a fix regressed,
    or the stamp landed prematurely) — so this never hard-blocks; it
    surfaces a judgment point the EM resolves, mirroring
    `build_gate_check_judgment_point`'s shape for the sibling
    `awaiting_gate` deployment_state. `compute_coast` blocks on ANY
    `judgment_points[]` entry carrying an `id` (this one always does), so
    `gates.coast.verdict` is never `"clear"` while this is unresolved —
    closing the exact silent-coast-is-clear gap the defect named.

    Tier: `insufficient-evidence` — `deployment_state`/`shipped_in` are
    engine-read frontmatter fields (not a quote of untrusted body text), but
    whether the shipped stamp still reflects reality — has the fix since
    regressed, was the stamp premature — is exactly the read this module
    never mechanizes."""
    return build_judgment_point(
        "jshipped",
        "This handoff is already stamped deployment_state: shipped — reopen "
        "it, or stand down and leave it closed?",
        evidence_pointer,
        [
            {
                "value": "reopen-and-proceed",
                "resolves": resolves,
                "guidance": (
                    "Treat the shipped stamp as stale or premature — the work needs "
                    "further action after all (a fix regressed, or the ship landed "
                    "before the baton was genuinely done). Check `shipped_in` (when "
                    "present) against what actually landed on disk/git before "
                    "proceeding, and record why this baton is being reopened."
                ),
            },
            {
                "value": "confirm-shipped-stand-down",
                "resolves": [],
                "guidance": (
                    "Confirm the shipped stamp is accurate — this baton is "
                    "genuinely done. Stand down; do not claim it. The "
                    "handoff sitting in state/handoffs/ awaiting an archival sweep "
                    "is expected (ship-handoff retains it in place for later "
                    "archival, per `handoff_archive_transition`'s stamp_shipped "
                    "mode) — not a sign it needs picking up."
                ),
            },
        ],
        None,
        reason="insufficient-evidence",
    )


def build_gate_recheck_directive(artifact_path: str) -> dict[str, Any]:
    """Piece A (cross-repo/inbox/2026-08-04-example-market-data-repo-em-pickup-
    jgate-cleared-strands-gate-fields.md) — the `jgate: cleared` recording
    directive, built ONLY in the `awaiting_gate` branch alongside `d2`
    (`claim-handoff`). Dispatches `archive-stamp-cli gate-recheck` ->
    `archive_stamp.cs_gate_recheck_handoff(path, at=..., cleared=True)`
    (`apply.py::_dispatch_archive_stamp_cli`), which flips
    `awaiting_gate -> ready_to_fire` and, when a `gate_evidence:` block is
    present, re-verifies it before honoring the clearance (`MutateAbort`,
    no write, unless the re-resolution reduces to `"freed"`) — an EM's
    `jgate: cleared` becomes an assertion the engine re-checks, not one it
    trusts absolutely. Most `awaiting_gate` records carry only
    `gate_dependency` prose, no `gate_evidence:` block: for those, this
    handler flips straight to `ready_to_fire` with `gate_dependency`
    stripped and zero machine verification — the EM's `cleared` is the
    sole authority when there is no machine evidence to check it against.
    (Review: staff-eng — the prior wording read as an unconditional
    guarantee.)

    The `awaiting_gate -> ready_to_fire -> in_flight` sequence makes
    `ready_to_fire` briefly observable on disk mid-`apply`, a state no
    consumer of an `awaiting_gate` record has had to reason about before
    (`handoff_gate_aging`, `roadmap/audit`, `cockpit_schema.roadmap_summary`
    and `handoff_children` all key off `deployment_state`). What bounds that
    window is the claim lock, NOT any assumption about who reads when:
    `apply` promotes the brief-stage reservation to a durable `apply`-stage
    claim (`promote_claim_stage`) before the first directive dispatches, so
    no second pickup can interleave a mutation here. A reader racing the
    window still sees committed disk state one transition early — bounded,
    single-process, and self-correcting on the same run. (Review: staff-eng —
    recorded because the reasoning that reached this conclusion originally
    rested on consumers not reading concurrently, which is false on a box
    running dozens of sessions; the lock is the real invariant, and anyone
    changing the locking needs to see that here.)

    No compensator is registered for this directive: if it lands and `d2`
    then raises, `apply` returns `APPLY_EXIT_PARTIAL_MUTATION` and commits
    nothing, leaving the record on disk at `ready_to_fire` with
    `gate_dependency` stripped, uncommitted. This is recoverable both ways rather than
    wedged, but by two DIFFERENT mechanisms — do not assume symmetry.
    `drop` -> `cs_unclaim_handoff` no-ops idempotently on `status: open`
    + `ready_to_fire`, then commits the dirty path. A re-run of `apply`
    recovers WITHOUT re-firing this directive at all: `brief()` reads the
    record's now-`ready_to_fire` `deployment_state`, so it never re-emits
    `jgate` or `d-gate-recheck`, and `d2` claims directly off
    `ready_to_fire`. The recheck is not replayed and not idempotently
    re-applied — it is already done. (Review: staff-eng — recorded because
    neither recovery path was documented; the earlier claim here, that a
    re-run replays `_gate_recheck` byte-identically, was false and was
    corrected after a reviewer's test disproved it.)

    Ordering (load-bearing): this directive's id must appear in `d2`'s
    `depends_on` list ALONGSIDE `"jgate"` — `order_by_depends_on`
    (`contract/apply_base.py`) topologically sorts on directive-id
    dependencies (this id is a real entry in the directives list), so `d2`
    never dispatches before this one lands; `directive_gate_open` ignores a
    `depends_on` entry that does not name a live judgment-point id, so
    this id contributes ordering only, never a second gate on `d2`'s own
    judgment resolution (`jgate` alone still gates `d2`, unchanged). `at`
    is stamped as today's date (`date.today().isoformat()`), matching
    `handoff_gate_aging`'s own `date.today()` use for gate-facing engine
    timestamps — `cs_gate_recheck_handoff`'s own `at` contract is a bare
    date string (see `test_archive_stamp.py::test_gate_recheck_cleared`),
    not a full ISO-8601 timestamp.

    The emitted verb is the LONG form `gate-recheck-handoff` — what
    `archive-stamp-cli`'s usage line advertises and its subcommand dispatch
    accepts. A directive's `cli` + `args` pair is a claim about the named
    CLI's argv contract, and both ways of working a directive are supported:
    dispatched through `pa_apply`, or run verbatim by hand off the brief.
    The short `gate-recheck` satisfied only the former — run as written it
    exits 2 with `unknown subcommand` (cross-repo/inbox/2026-08-20-doe-
    claude-em-cmd-forwarder-eats-json-and-two-smaller-seams.md, item 2).
    `_dispatch_archive_stamp_cli` accepts both spellings, so briefs emitted
    before this change still dispatch."""
    return {
        "id": "d-gate-recheck",
        "cli": "archive-stamp-cli",
        "args": ["gate-recheck-handoff", artifact_path, date.today().isoformat()],
        "depends_on": "jgate",
        "already_satisfied": False,
    }


#: M3 kind-dispatch disposition sets (contract § JUDGMENT checklist rows
#: "ask: Accept/Decline/Surface-to-PM", "proposal: Adopt/Decline/Negotiate",
#: "fyi impact: nil/invalidated/surgical-fix/product-decision/ambiguous").
#: `consult` is NOT `ask`'s Accept/Decline/Surface-to-PM shape — SKILL.md's
#: `consult` branch ("reply in place") never adopts, declines, or performs an
#: action beyond the reply itself. Answering in place IS the receiver-side
#: completion of a consult, though: the memo's work is fully done once the
#: reply lands, so both `consult` dispositions resolve `d-action-memo` like
#: any other receiver-done disposition (defect fix, 2026-07-25 — the prior
#: `resolves: []` throughout left every `consult` memo permanently claimed
#: at `status: in_progress`, mirroring the `fyi`/`ack-nil` regression this
#: repoint already closed once). The class discriminator that decides
#: `resolves: []` vs `["d-action-memo"]` across ALL of `_KIND_DISPOSITIONS`
#: is receiver-done-ness, not action-taken-ness: `resolves: []` is reserved
#: for dispositions where the receiver's work genuinely is NOT done yet
#: (surface-to-PM, re-plan, investigate-further, and the like) — see the
#: per-disposition comment below for the concrete split.
#: C8 BUILD (3) — every action-taking disposition below resolves
#: `d-action-memo` (the disposition-gated terminal write), never `d1` (the
#: claim, which fires unconditionally per C7 and must never appear in ANY
#: disposition's `resolves` — memo-code GAP 3, the stale-`["d1"]` failure
#: mode this repoint exists to close). `claim-memo-stamp` likewise never
#: appears here — it fires unconditionally alongside `d1` (see
#: `build_memo_directives`), not gated on this judgment point.
#:
#: C4 BUILD — `guidance` lands co-located on each disposition dict
#: (alongside `value`/`resolves`), NOT a sibling map keyed by disposition
#: value: a sibling map can drift (a value present here but absent from a
#: parallel map, or vice versa, silently ships wrong/missing guidance for
#: that value) while co-location cannot. `_KIND_DISPOSITIONS` entries are
#: raw dict literals never routed through `build_disposition`
#: (`contract/decision_object/judgment.py:66`, used only by
#: `workstream_complete`), and DoE's `schemas/decision-object.schema.json:178`
#: sets the disposition object `additionalProperties: true` by design for
#: exactly this kind of per-skill-instance content — no schema change, no
#: cross-repo round-trip (the Director of Engineering, DR-047 resolved in-repo).
#:
#: AUTHORING DISCIPLINE (reviewer holds this line): `guidance` describes
#: what a disposition MEANS and how to carry it out — evenhandedly, across
#: every option in a set. It must never front-load or editorialize toward
#: one option ("usually you'll want to..."); that would de-facto recommend
#: and defeat this judgment point's `recommendation=None`,
#: `reason="insufficient-evidence"` (see `build_kind_dispatch_judgment_point`
#: below, unchanged by this addition — guidance rides on the disposition
#: entries, never on a `recommendation` object, which the shared seam
#: (`contract/decision_object/judgment.py`) restricts to
#: `disposition`/`rationale` only).
_KIND_DISPOSITIONS: dict[str, list[dict[str, Any]]] = {
    "ask": [
        {
            "value": "accept-mechanical-direct",
            "resolves": ["d-action-memo"],
            "guidance": (
                "Accept and action now, no plan needed — three shapes: route-to-baton "
                "(the ask falls inside an active handoff's scope; fold it into that "
                "handoff's body and commit, rather than triaging it fresh), "
                "direct-dispatch (small/bounded enough to hand an executor immediately), "
                "or do-now-before-gate (act before a pending gate closes). "
                "route-to-baton requires a LIVE target, and the target is a handoff — a "
                "plan or handoff already in a terminal state (`status: implemented` / "
                "`shipped` / `superseded`, or `deployment_state: shipped`) is a "
                "historical record, and folding a forward-binding constraint into one "
                "buries it: nobody reads a delivered plan's Anti-scope before building "
                "the thing it constrains. Recording correspondence against a delivered "
                "plan is fine; writing an instruction there is not. When the only "
                "on-topic artifact is terminal, the fold belongs in live substrate "
                "instead — the roadmap's amendment file, the downstream stub handoff "
                "that will actually build the thing, or a decision record — and say in "
                "the commit why the delivered plan was not the home. Before "
                "accepting: verify the memo's premise against current disk/git state "
                "(a sender's absence-claim is scoped to the sender's own visibility — "
                "treat a contradicting local hit as a real contradiction, not as the "
                "sender simply not having looked; a receiver-repo dedup check is a "
                "same-topic judgment call, not a keyword match) and check no other "
                "session already holds a live claim on the artifact this memo concerns "
                "(an apparently-orphaned lock still needs a liveness check before any "
                "takeover). **A route-to-baton fold into a target that HAS a live "
                "holder is not complete when the commit lands.** The same claim check "
                "that says whether you may write also says whom to tell: a live holder "
                "is mid-work against the body you just changed, will not re-read it on "
                "your account, and can execute the very thing your constraint binds "
                "before ever seeing it — a write nobody is told about is a race you "
                "chose to run. Message them: `gates.competing_claim`'s candidates carry "
                "`send_message_address` for exactly this, resolved fresh in this brief "
                "(never persisted or reused past this instant — see that field's own "
                "negative-spec), so it costs one call and no lookup. Say what you wrote, "
                "where, and what it binds; keep it to that. If the holder is NOT live, "
                "or has no resolvable address, say so in the `decision_note` — \"no "
                "message was owed\" is a finding the next reader needs, and is not the "
                "same as having skipped it. If the memo's `scoped_to` looks too narrow or too broad for "
                "the actual change, challenge it rather than accepting it as given. "
                "Capture any commitment this creates for a sibling repo/session before "
                "moving on, and record the item's distillation fate (ephemeral / "
                "commitment / ratification) so a later `/distill` knows whether to prune "
                "it. If the accepted item is a cross-repo roadmap-stub MOVE, audit the "
                "source side for a residual after the move lands. This disposition maps "
                "to `--decision accepted`, which requires `realized_by` (a pointer to "
                "what realized the ask — typically the commit SHA that landed the fix) "
                "alongside `decision_note`; `cs_action_memo` fails loud without it."
            ),
        },
        {
            "value": "accept-escalate-to-sizing",
            "resolves": ["d-action-memo"],
            "guidance": (
                "Accept, but the ask is novel work in this repo and bigger than a direct "
                "action — route it into `coordinator:sizing` rather than executing inline "
                "or gut-reading \"big enough for a plan\". The sizing lobby picks the room "
                "(dispatch / spec-dispatch / shape / plan / roadmap / pm-decision) for you. "
                "Same premise-verification and live-claim-holder checks as "
                "accept-mechanical-direct apply before accepting. If the memo's "
                "resolution forward-points at an existing plan rather than asking for a "
                "new one, reconcile that plan's on-disk state first: read the plan's "
                "current status, confirm it is still live (not already executed, "
                "abandoned, or superseded) before treating it as the resolution target, "
                "and surface what you find rather than assuming the pointer is still "
                "accurate. This disposition maps to `--decision partial` (the memo is "
                "only partially actioned in-line; the sizing object it escalates to "
                "carries the rest), which requires `realized_by` (a pointer to what "
                "realized this partial step — the sizing object's path, "
                "`state/sizings/<id>.yaml`; a sizing that terminates at `route: "
                "pm-decision` with `xl_exit: null` is a legitimate open state, which is "
                "exactly why `partial` remains right rather than becoming wrong) "
                "alongside `decision_note`; `cs_action_memo` fails loud without it."
            ),
        },
        {
            # Receiver-done: declining IS the disposal of the ask (no further
            # action is owed), so this resolves `d-action-memo` via the
            # `--decision declined` channel (`_MEMO_ACTION_DECISION_MAP`) —
            # NOT `--actioned-note` (defect fix, 2026-07-25; see the class
            # comment above `_KIND_DISPOSITIONS` for the receiver-done vs
            # work-still-owed discriminator).
            "value": "decline",
            "resolves": ["d-action-memo"],
            "guidance": (
                "Decline — no action taken on the ask itself. Record why in "
                "`decision_note` (NOT `actioned_note` — a decision-mapped disposition "
                "takes its reasoning via `decision_note`; supplying `actioned_note` here "
                "instead raises a fail-loud from `_build_action_memo_args`) so the sender "
                "(and any later reader) sees the reasoning, not just the verdict. A "
                "wrong-addressee memo (this session is not who the memo names) is a "
                "stop-and-offer, not a silent decline — surface the mismatch rather than "
                "claiming/stamping/actioning a memo addressed to someone else."
            ),
        },
        {
            # Work still owed: the ask is unresolved until the PM weighs in,
            # so this stays `resolves: []` — halting at `d-action-memo` is
            # correct, not a defect.
            "value": "surface-to-PM",
            "resolves": [],
            "guidance": (
                "Surface to the PM rather than deciding unilaterally — the right call "
                "when the ask is product direction, a scope change, an external-facing "
                "action, or a genuine no-correct-answer tradeoff. Present the memo and "
                "the fork, not a pre-baked recommendation."
            ),
        },
    ],
    "consult": [
        {
            "value": "reply-short",
            "resolves": ["d-action-memo"],
            "guidance": (
                "Answer in place, briefly — the reply goes directly into `actioned_note`. "
                "Appropriate when the question has a short, self-contained answer that "
                "doesn't need its own section. Actioning this disposition requires "
                "`actioned_note` (the reply itself): `d-action-memo` resolves via the "
                "`--actioned-note` path (no `--decision`, since replying in place is not "
                "an accepted/partial/declined outcome), and `cs_action_memo` fails loud if "
                "neither `--decision` nor `--actioned-note` is supplied — so state the "
                "reply, however brief, rather than leaving `actioned_note` empty."
            ),
        },
        {
            "value": "reply-long",
            "resolves": ["d-action-memo"],
            "guidance": (
                "Answer in place, at length — write the full reply under a `## EM "
                "Response` heading in the artifact body, and point `actioned_note` at "
                "that heading rather than duplicating the text. Appropriate when the "
                "question needs reasoning, options, or evidence laid out, not just a "
                "verdict. Actioning this disposition requires `actioned_note` (pointing "
                "at the `## EM Response` heading): `d-action-memo` resolves via the "
                "`--actioned-note` path (no `--decision`, since replying in place is not "
                "an accepted/partial/declined outcome), and `cs_action_memo` fails loud if "
                "neither `--decision` nor `--actioned-note` is supplied — so state the "
                "pointer, however brief, rather than leaving `actioned_note` empty."
            ),
        },
    ],
    "proposal": [
        {
            "value": "adopt",
            "resolves": ["d-action-memo"],
            "guidance": (
                "Adopt the proposal as sent — action it directly. Same premise- and "
                "live-claim-holder verification as an `ask` accept applies before "
                "adopting. This disposition maps to `--decision accepted`, which "
                "requires `realized_by` (a pointer to what realized the proposal — "
                "typically the commit SHA that landed it) alongside `decision_note`; "
                "`cs_action_memo` fails loud without it."
            ),
        },
        {
            # Receiver-done: declining IS the disposal of the proposal, so
            # this resolves `d-action-memo` via the `--decision declined`
            # channel (`_MEMO_ACTION_DECISION_MAP`) — NOT `--actioned-note`.
            # Mirrors `ask`/`decline` above.
            "value": "decline",
            "resolves": ["d-action-memo"],
            "guidance": (
                "Decline the proposal — no action taken. Record why in `decision_note` "
                "(NOT `actioned_note` — a decision-mapped disposition takes its reasoning "
                "via `decision_note`; supplying `actioned_note` here instead raises a "
                "fail-loud from `_build_action_memo_args`)."
            ),
        },
        {
            "value": "negotiate",
            "resolves": ["d-action-memo"],
            "guidance": (
                "Neither adopt nor decline outright — counter-propose a modified shape "
                "and record the counter in `actioned_note` (or reply body) for the "
                "sender to react to. Actioning this disposition requires `actioned_note` "
                "(the counter, or a pointer to it): `d-action-memo` resolves via the "
                "`--actioned-note` path (no `--decision`, since negotiating is not an "
                "accepted/partial/declined outcome), and `cs_action_memo` fails loud if "
                "neither `--decision` nor `--actioned-note` is supplied — so state the "
                "counter, however brief, rather than leaving `actioned_note` empty."
            ),
        },
    ],
    "fyi": [
        {
            "value": "ack-nil",
            "resolves": ["d-action-memo"],
            "guidance": (
                "No impact on this repo's work — acknowledge and close, no further "
                "action. Actioning this disposition requires `actioned_note` (recording "
                "the nil-impact rationale): `d-action-memo` resolves via the "
                "`--actioned-note` path (no `--decision`, since nil-impact is not an "
                "accepted/partial/declined outcome), and `cs_action_memo` fails loud if "
                "neither `--decision` nor `--actioned-note` is supplied — so state the "
                "rationale, however brief, rather than leaving `actioned_note` empty."
            ),
        },
        {
            "value": "re-plan",
            "resolves": [],
            "guidance": (
                "The FYI invalidates an existing plan's premise — the right response is "
                "re-planning that surface, not a direct edit."
            ),
        },
        {
            "value": "surgical-fix",
            "resolves": ["d-action-memo"],
            "guidance": (
                "The FYI needs a small, contained fix here — action it directly rather "
                "than a full re-plan. Same premise/live-claim verification as an `ask` "
                "accept applies. This disposition maps to `--decision accepted`, so it "
                "requires BOTH `realized_by` (the SHA of the commit that lands the fix — "
                "`cs_action_memo` fails loud without it) and `decision_note` for the "
                "reasoning; `actioned_note` is rejected on this branch, it belongs to "
                "nil-impact dispositions only. Land the fix first, then action the memo "
                "with its SHA."
            ),
        },
        {
            "value": "surface-to-PM",
            "resolves": [],
            "guidance": (
                "The FYI's impact is a product-direction call — surface it to the PM "
                "rather than deciding it here."
            ),
        },
        {
            "value": "investigate-further",
            "resolves": [],
            "guidance": (
                "The FYI's impact is ambiguous from the memo alone — investigate before "
                "committing to nil/re-plan/surgical-fix/surface-to-PM."
            ),
        },
    ],
}

#: Defect 2 fix (2026-07-29, doe-claude-em self-claim-reads-as-live-peer
#: memo): `ops/memo_transition.py` hard-requires `--realized-by` whenever
#: `--decision` is `accepted`/`partial` (`memo_transition.py:642-643`), but
#: nothing an operator reads before `apply` said so — not the disposition
#: `guidance` strings, not `validate_decisions_shape` (deliberately
#: shape-only, per its own docstring), not the `--decisions` usage block.
#: General fix, not a narrow one: declare the requirement ONCE, keyed off
#: `decision` (`_MEMO_ACTION_DECISION_MAP`'s own output value, the SAME
#: truth `_build_action_memo_args` reads to pick `cs_action_memo`'s
#: `--decision`), so any future `_MEMO_ACTION_DECISION_MAP` entry inherits
#: discoverability automatically rather than needing a second hand-written
#: table kept in sync by hand. `realized_by` is required exactly where the
#: decision is `accepted`/`partial`; `declined` requires nothing
#: (`memo_transition.py`'s own condition matches this exactly).
_DECISION_REQUIRED_CONTENT_KEYS: dict[str, tuple[str, ...]] = {
    "accepted": ("realized_by",),
    "partial": ("realized_by",),
    "declined": (),
}


def _required_content_keys(kind: str, disposition: str) -> tuple[str, ...]:
    """The `--decisions` content keys `memo_transition.py` requires for this
    `(kind, disposition)` pair, derived from `_MEMO_ACTION_DECISION_MAP` +
    `_DECISION_REQUIRED_CONTENT_KEYS` rather than a second parallel table —
    a disposition absent from `_MEMO_ACTION_DECISION_MAP` (every
    `consult`/`proposal negotiate`/nil-impact `fyi` disposition, none of
    which take `--decision` at all) has no mapped decision and therefore no
    required content key."""
    decision_value = _MEMO_ACTION_DECISION_MAP.get((kind, disposition))
    if decision_value is None:
        return ()
    return _DECISION_REQUIRED_CONTENT_KEYS.get(decision_value, ())


def _dispositions_with_required_keys(kind: str) -> list[dict[str, Any]]:
    """`_KIND_DISPOSITIONS[kind]` with `required_content_keys` stamped onto
    each disposition dict, so the decision object `brief` emits tells the
    operator which `--decisions` content keys a disposition needs BEFORE
    `apply` runs (defect 2) — discoverable on the surface an EM actually
    reads, not only as a hard failure three commands later. Returns fresh
    dicts; never mutates the module-level `_KIND_DISPOSITIONS` literal
    in place, since that dict is shared across every `brief()` call."""
    return [
        {**entry, "required_content_keys": list(_required_content_keys(kind, entry["value"]))}
        for entry in _KIND_DISPOSITIONS[kind]
    ]


_KIND_QUESTIONS: dict[str, str] = {
    "ask": "ask: Accept mechanical-direct / Accept escalate-to-sizing / Decline / Surface-to-PM?",
    "consult": "consult: Reply short (goes in actioned_note) / Reply long (## EM Response heading, actioned_note points at it)?",
    "proposal": "proposal: Adopt / Decline / Negotiate?",
    "fyi": "fyi impact: nil / plan-invalidated / surgical-fix / product-decision / ambiguous?",
}


def resolve_memo_kind(fm: dict[str, Any]) -> tuple[str, bool]:
    """M3 kind-enum resolution (contract § MECHANICAL checklist, Locus M3):
    absent -> `ask` default; present-unrecognized -> `ask` + warn;
    pinned-enum match -> itself. Returns `(kind_resolved, unrecognized)`."""
    kind = fm.get("kind")
    if not kind:
        return "ask", False
    if kind not in _KIND_DISPOSITIONS:
        return "ask", True
    return kind, False


def _archived_open_memo_kind_dispatch(
    artifact_path: str, terminal_fields: dict[str, Any], decisions: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Archived-memo-still-open kind-dispatch assembly (2026-07-27
    doe-claude-em memo defect fix, `brief()`'s `classification == "archived"`
    branch) — an archived MEMO whose terminal `status` frontmatter field is
    NOT already a terminal disposition (in `_MEMO_TERMINAL_STATUS`) was
    swept into the archive without ever having a disposition stamped on it. Before this
    fix `brief()` unconditionally emitted `directives: []` for every
    archived artifact, so there was no directive-driven path left to
    discharge it — an operator had to hand-run `archive-stamp-cli
    action-memo`, exactly the "operator remembers" shape this repo's
    CLAUDE.md § North star discharge test forbids.

    Reuses the SAME `resolve_memo_kind` / `build_memo_directives` /
    `build_kind_dispatch_judgment_point` triple the live in-place memo
    branch calls below (do NOT re-implement kind resolution or directive
    assembly here — this function's whole job is to be the second CALL
    SITE of that existing triple, never a second composing of it) — so the
    answer to "what does closing a memo of kind K look like" exists exactly
    once regardless of whether the memo currently lives at a live path or
    an archive-resident one.

    Callers gate the `terminal_fields.get("status") not in
    _MEMO_TERMINAL_STATUS` check themselves before invoking this — it is
    unconditional once called.
    """
    kind_resolved, kind_unrecognized = resolve_memo_kind(terminal_fields)
    directives = build_memo_directives(artifact_path, kind_resolved, decisions)
    jp = build_kind_dispatch_judgment_point(kind_resolved, terminal_fields.get("kind"), kind_unrecognized)
    return directives, [jp]


def build_kind_dispatch_judgment_point(kind_resolved: str, kind_raw: Optional[str], unrecognized: bool) -> dict[str, Any]:
    """M3 kind-dispatch JUDGMENT entry (dispatch brief defect 4) — frames the
    engine's `kind_resolved` narrowing as an overridable offer, never a
    verdict: the EM picks the disposition, the engine never auto-decides
    Accept/Decline/Adopt/etc. for it.

    Tier: `insufficient-evidence` — `kind_resolved` narrows a closed enum
    from frontmatter to itself; the choice among its dispositions is a
    product/policy read the engine cannot make, not evidence quoted
    verbatim from an untrusted body."""
    entry = build_judgment_point(
        "j-kind",
        _KIND_QUESTIONS[kind_resolved],
        "artifact.kind_resolved",
        _dispositions_with_required_keys(kind_resolved),
        None,
        reason="insufficient-evidence",
    )
    if unrecognized:
        entry["warning"] = f"kind {kind_raw!r} unrecognized — defaulted to 'ask'"
    return entry


# ---------------------------------------------------------------------------
# Function 6 — completeness_checklist parse + directive/judgment assembly
# (Step 5.5a-d — MECHANICAL parse+ordering, JUDGMENT probe-run confirmation)
# ---------------------------------------------------------------------------

def build_completeness_checklist(fm: dict[str, Any], artifact_path: str) -> dict[str, Any]:
    """Function 6 — parses `completeness_checklist:` items (in-process, via
    `coordinator_core.ops.parse_completeness_item`, AC16 — never re-derives
    the grammar here), hoists `restart-gated` items ahead of `live` items
    (Step 5.5b fixed ordering rule), and returns one `coordinator-tasks-mirror
    init` EM-run directive per item.

    MIRROR IS PRIMARY, HARNESS TASK IS BEST-EFFORT. Each directive carries an
    additive `harness_task_create` payload for a consumer that mirrors the item
    into the agent harness's own task surface. That payload is advisory: this
    repo has no consumer for it, it commands no directive of its own, and an EM
    whose harness lacks `TaskCreate` simply does not act on it. The disk-backed
    `coordinator-tasks-mirror` CLI is the durable half and the only half that
    executes — confirmed inert-but-harmless in a live session on 2026-08-17,
    when `TaskCreate` was absent from an EM tool surface entirely
    (`cross-repo/inbox/2026-08-17-example-cockpit-repo-em-harness-task-create-payload-inert.md`).
    NEGATIVE SPEC: do not invert these. Promoting the harness task to primary
    and demoting the mirror to fallback trades a durable on-disk record for a
    harness capability that has already vanished once.

    SECURITY-LOAD-BEARING (contract § "Probe-confirmation is JUDGMENT, not a
    gates boolean"): a `[probe: ...]` command is UNTRUSTED input with full
    agent-Bash blast radius. This function NEVER returns a directive that
    runs a probe — the only artifact a probe-carrying item produces is a
    `judgment_points` entry ("Run untrusted completeness probe `<cmd>`?")
    whose `dispositions` resolve NOTHING (`resolves: []` on both choices) —
    there is no downstream directive for either disposition to unblock,
    by construction. An autonomous no-human consumer MUST leave that
    judgment point unresolved and the probe unrun; this function's shape
    makes "auto-run the probe" structurally unreachable rather than merely
    discouraged.
    """
    raw_items = fm.get("completeness_checklist")
    if not raw_items:
        return {"items": [], "directives": [], "judgment_points": [], "batches": []}
    if isinstance(raw_items, str):
        raw_items = [raw_items]

    parsed: list[dict[str, Any]] = []
    for raw in raw_items:
        try:
            item_class, assertion, probe = _parse_completeness_item(raw)
        except _CompletenessMalformed as exc:
            parsed.append({"raw": raw, "malformed": True, "error": str(exc)})
            continue
        parsed.append({
            "raw": raw,
            "malformed": False,
            "class": item_class,
            "assertion": assertion,
            "probe": probe or None,
        })

    # Step 5.5b — restart-gated items hoisted ahead of live items (fixed
    # ordering rule; stable sort preserves within-class declaration order).
    ordered = sorted(
        (item for item in parsed if not item["malformed"]),
        key=lambda item: 0 if item["class"] == "restart-gated" else 1,
    )

    basename = Path(artifact_path).name
    directives: list[dict[str, Any]] = []
    judgment_points: list[dict[str, Any]] = []

    for idx, item in enumerate(ordered):
        task_id = f"ct{idx + 1}"
        directives.append({
            "id": f"d-{task_id}-mirror",
            "cli": "coordinator-tasks-mirror",
            "args": ["init", basename, item["assertion"]],
            "depends_on": None,
            "already_satisfied": False,
            "harness_task_create": {"content": item["assertion"], "class": item["class"]},
        })
        if item["probe"]:
            # SECURITY-LOAD-BEARING (see module docstring/negative-spec):
            # `item["probe"]` is untrusted, branch-writable content quoted
            # verbatim into `evidence` — recommending here would nudge an
            # operator toward running attacker-influenceable input.
            # `build_untrusted_gate_judgment_point` makes `recommendation`
            # structurally unreachable rather than merely discouraged.
            judgment_points.append(build_untrusted_gate_judgment_point(
                f"j-{task_id}-probe",
                f"Run untrusted completeness probe `{item['probe']}`?",
                item["probe"],
                [
                    {"value": "confirm-and-run", "resolves": []},
                    {"value": "skip-and-validate-manually", "resolves": []},
                ],
            ))

    # Step 5.5b — `preflight.completeness_batches`: the restart-gated-hoisted
    # ordering as its own evidence field (contract § computed-skills.md
    # "Restart-gated hoist/partition (fixed ordering rule)"), independent of
    # `directives[]`'s parallel ordering above.
    # Review: code-reviewer — Finding 5: `probe` is duplicated here rather
    # than replaced with an index back into `items[]` intentionally — batches
    # is self-contained ordering evidence, requiring no cross-referencing by
    # consumers.
    batches = [
        {"class": item["class"], "assertion": item["assertion"], "probe": item["probe"]}
        for item in ordered
    ]

    return {"items": parsed, "directives": directives, "judgment_points": judgment_points, "batches": batches}


# ---------------------------------------------------------------------------
# gates.execution_stamp_match (AC18) — the execution_authorized_sha
# recompute-and-classify gate.
#
# Purpose: `computed-skills.md` maps the "`execution_authorized_sha` recompute
# (`hash-object`) + compare" MECHANICAL step to `gates.execution_stamp_match`
# — this is that mapping's engine-side computation. Fires on any artifact
# carrying an `execution_authorized_sha` directly, or a `## Plan to Execute`
# body pointer to a plan that carries one. `stale-bookkeeping` promotes to a
# tier-1 re-stamp `directives[]` entry; `stale-substantive` stays a tier-3
# `judgment_points[]` entry (`recommendation: null`) — this module pre-tags
# the diff's shape, it never adjudicates whether the underlying scope change
# is an acceptable one.
#
# Negative-spec: does NOT invent a second hash algorithm. `_frontmatter_body_
# text`/`_git_hash_object_stdin` below now DELEGATE to the shared
# `coordinator_core.frontmatter.primitives.frontmatter_body_text`/
# `git_blob_sha1`/`canonical_body_sha` recipe (Review: code-reviewer —
# Finding 3, extracted from two independently hand-maintained copies of this
# module's own body here and `coordinator_core.review_assemble.
# exec_auth_stamp._canonical_body_sha`) rather than each hand-rolling the
# awk-port + blob-hash algorithm locally. `_find_stamp_commit`/
# `_read_file_at_revision` resolve through the same in-process loose/pack
# object + ref read-model as the rest of this module. Only
# `_classify_stamp_delta`'s `git diff` comparison stays a real spawn — a
# PM-gating verdict where a `difflib` vs. Myers alignment divergence would
# be silently unsafe (see the `git diff` residual-spawn comment above
# `_NO_CONSOLE`).
# ---------------------------------------------------------------------------

_PLAN_TO_EXECUTE_HEADING = "Plan to Execute"
_RATIFICATION_LINE_RE = re.compile(
    r"^[+-]\s*execution_authorized_(?:by|at|sha|note)\s*:", re.IGNORECASE
)
_STATUS_LINE_RE = re.compile(r"^[+-]\*\*Status:?\*\*")


def _extract_plan_to_execute_pointer(body_text: str) -> Optional[str]:
    """First `.md` path cited under a `## Plan to Execute` body heading —
    mirrors `_extract_cited_path`'s first-match-only convention and
    `_parse_pending_items`'s section-scoped heading scan. Returns `None` when
    the artifact carries no such section."""
    in_section = False
    for line in body_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = stripped[3:].strip() == _PLAN_TO_EXECUTE_HEADING
            continue
        if stripped.startswith("# ") and not stripped.startswith("## "):
            in_section = False
            continue
        if not in_section:
            continue
        cited = _extract_cited_path(stripped)
        if cited:
            return cited
    return None


def _frontmatter_body_text(file_text: str) -> str:
    """Everything below the second `---` frontmatter delimiter line —
    delegates to the shared `coordinator_core.frontmatter.primitives.
    frontmatter_body_text` recipe (Review: code-reviewer — Finding 3).
    Kept as a thin local alias so this module's many internal call sites
    need no rename."""
    return _shared_frontmatter_body_text(file_text)


def _git_hash_object_stdin(text: str, cwd: Path) -> Optional[str]:
    """`git hash-object --stdin` over already-extracted body text —
    delegates to the shared `coordinator_core.frontmatter.primitives.
    git_blob_sha1` recipe (Review: code-reviewer — Finding 3), which computes
    the literal git blob-hash algorithm (`sha1("blob " + len(content) +
    "\\0" + content)`) in-process, byte-for-byte, no subprocess spawn. `cwd`
    is accepted for call-site-signature parity only — computing a blob hash
    needs no repo state, only the content and its length."""
    return _shared_git_blob_sha1(text)


def _read_file_at_revision(repo_root: Path, revision: str, path: str) -> Optional[str]:
    result = _run_git(["show", f"{revision}:{path}"], repo_root)
    if result.returncode != 0:
        return None
    return result.stdout


def _find_stamp_commit(repo_root: Path, path: str, stamped_sha: str) -> Optional[str]:
    """The commit `git log -S<stamped_sha>` names as having last changed the
    occurrence count of the stamped literal in `path` — the same pickaxe
    search the contract's own worked example names. `--follow` keeps this
    resolving across a rename; `None` when no commit in history ever
    introduced the value (the `unstampable` premise).

    Spawns real `git` directly, bypassing `_run_git`'s in-process read-model
    dispatch (stamp-integrity investigation, `tasks/mise-findings/stamp-
    integrity.md`, DoE-claude, Root cause B). The read-model's own
    `_in_process_pickaxe`/`_walk_commits` reimplementation of this search
    provably disagrees with real git — not only on the documented `--follow`
    rename gap, but also on a merge commit that is TREESAME to one parent on
    this path: real git's default merge simplification walks past such a
    commit into the parent that actually introduced the change, while
    `_in_process_pickaxe` returns the merge commit itself the moment ANY
    parent's needle count differs (see the corrected read-model negative-
    spec comment above `_NO_CONSOLE`, and
    `test_find_stamp_commit_disagrees_with_read_model_on_treesame_merge_no_rename`).
    A wrong answer here makes `compute_execution_stamp_match` hash the wrong
    revision and misclassify a genuinely valid, correctly-computed
    `execution_authorized_sha` as `unstampable` — a PM-gating verdict, so
    this must stay byte-for-byte git-equivalent. This call is off
    `compute_execution_stamp_match`'s zero-spawn hot path (it only runs once
    an `execution_authorized_sha`/pointer is already present on the artifact,
    not the common `brief()` case — see the module docstring above
    `_NO_CONSOLE`), so one real spawn here costs nothing that matters; the
    same tradeoff is already accepted for `_classify_stamp_delta`'s `git
    diff` spawn, for the identical reason."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "log", "-1", "--follow", f"-S{stamped_sha}", "--format=%H", "--", path],
            capture_output=True,
            text=True,
            timeout=30,
            **_NO_CONSOLE,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return out or None


def _is_bookkeeping_diff_line(line: str) -> bool:
    """One changed content line (a unified-diff `+`/`-` row, header rows
    already filtered by the caller) is bookkeeping iff it is a ratification
    field (`execution_authorized_*`), a body `**Status:**` line, or blank."""
    if _RATIFICATION_LINE_RE.match(line):
        return True
    if _STATUS_LINE_RE.match(line):
        return True
    if not line[1:].strip():
        return True
    return False


def _classify_stamp_delta(repo_root: Path, stamp_commit: str, path: str) -> str:
    """The `/pickup` Step 1 triage, mechanized: every changed content line in
    `stamp_commit..HEAD -- path` must be a ratification-line, a
    `**Status:**` line, or blank to count as `bookkeeping` — a single new
    spine row, changed target, or altered scope/AC line (or anything this
    triage does not recognize) defaults to `substantive`. This function
    only recognizes that substantive content changed; it never judges
    whether the change is an acceptable one."""
    result = _run_git(["diff", f"{stamp_commit}..HEAD", "--", path], repo_root)
    if result.returncode != 0:
        return "substantive"
    saw_change = False
    for line in result.stdout.splitlines():
        if line.startswith(("+++", "---", "diff --git", "index ", "@@")):
            continue
        if not line or line[0] not in "+-":
            continue
        saw_change = True
        if not _is_bookkeeping_diff_line(line):
            return "substantive"
    return "bookkeeping" if saw_change else "substantive"


def compute_execution_stamp_match(
    repo_root: Path, fm: dict[str, Any], artifact_path: str
) -> Optional[tuple[dict[str, Any], str]]:
    """`gates.execution_stamp_match` (AC18) — `None` when the artifact
    carries neither an `execution_authorized_sha` of its own nor a pointer
    to a plan that carries one (the field is conditional, per the
    contract's typed field schema, not always-present). The pointer is
    read from either of two conventions: a `## Plan to Execute` body
    heading (canonical, `docs/wiki/plan-execute-session-split.md` §
    Pinned conventions), or a `governing_plan:` frontmatter field (seen in
    the wild on handoffs that skip the body-heading convention — e.g.
    `state/handoffs/2026-07-27-execute-workstream-complete-computed-
    frontage.md`). When BOTH the pointer and a mirrored
    `execution_authorized_sha` are present — a handoff mirroring its
    target plan's stamp on its own frontmatter for human readability,
    alongside the pointer — the pointer wins: the plan is always the hash
    target, never the pointing artifact's own body.

    Missing this second convention was a live defect (2026-07-27): a
    handoff pointing at its plan only via `governing_plan:` fell through
    to the "no pointer" branch, which then hashed the HANDOFF's own body
    against the plan's `execution_authorized_sha` mirrored onto the
    handoff — comparing the wrong document's hash against the mirrored
    value, guaranteed to mismatch. `execution_stamp_match`'s `d-stamp`
    auto-remediation directive then re-stamped the handoff's own field to
    match its own body, which self-consistently "fixed" the symptom while
    permanently discarding the mirror's actual purpose (verifying the
    PLAN's authorization). Recognizing `governing_plan:` here restores the
    pointer and makes the plan — not the handoff — the hash target again.

    Returns `(gate, target_path)` on a hit: `gate` is the exact
    `{verdict, stamped_sha, computed_sha, stamp_commit, delta_class,
    next_move}` shape the contract specifies; `target_path` (repo-relative)
    is the file the stamp was read from — this artifact itself, or the plan
    it points at — kept out of `gate` so the emitted object matches the
    contract's field list exactly, and threaded back to the caller only for
    building the re-stamp directive's `args`.
    """
    live_path = repo_root / artifact_path
    if not live_path.is_file():
        return None
    try:
        artifact_text = live_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    target_rel_path = artifact_path
    target_text = artifact_text

    # A `## Plan to Execute` pointer (or, failing that, a `governing_plan:`
    # frontmatter field — see docstring) names the plan the stamp actually
    # authorizes — takes priority over an `execution_authorized_sha` the
    # pointing artifact (e.g. a handoff) may ALSO carry directly on its own
    # frontmatter as a human-readable mirror of the plan's stamp (the
    # `/execute-plan` SKILL's Phase 1.2 convention). Hashing the pointing
    # artifact's own body in that case computes the wrong recipe input
    # entirely — the artifact isn't the plan — and false-negatives a validly
    # stamped plan as `unstampable`. Only fall back to the artifact's own
    # body+field when there is no pointer of either shape, i.e. the artifact
    # IS the plan.
    split = split_frontmatter(artifact_text)
    body_text = split.body_with_leading_newline if split is not None else artifact_text
    pointer = _extract_plan_to_execute_pointer(body_text) or fm.get("governing_plan")

    if pointer:
        plan_abs = repo_root / pointer
        if not plan_abs.is_file():
            return None
        try:
            plan_text = plan_abs.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        plan_split = split_frontmatter(plan_text)
        if plan_split is None:
            return None
        stamped_sha = read_fm_field_unquoted(plan_split.fm_text, "execution_authorized_sha")
        if not stamped_sha:
            return None
        target_rel_path = pointer
        target_text = plan_text
    else:
        stamped_sha = fm.get("execution_authorized_sha")
        if not stamped_sha:
            return None

    computed_sha = _git_hash_object_stdin(_frontmatter_body_text(target_text), repo_root)
    if computed_sha is None:
        return None

    stamp_commit = _find_stamp_commit(repo_root, target_rel_path, stamped_sha)

    if computed_sha == stamped_sha:
        return (
            {
                "verdict": "match",
                "stamped_sha": stamped_sha,
                "computed_sha": computed_sha,
                "stamp_commit": stamp_commit,
                "delta_class": None,
                "next_move": "Execution authorization stamp matches the current plan body — proceed.",
            },
            target_rel_path,
        )

    if stamp_commit is None:
        return (
            {
                "verdict": "unstampable",
                "stamped_sha": stamped_sha,
                "computed_sha": computed_sha,
                "stamp_commit": None,
                "delta_class": None,
                "next_move": (
                    f"Re-stamp execution_authorized_sha on {target_rel_path} to {computed_sha} "
                    "— no commit in this file's history introduced the recorded value by the "
                    "canonical recipe."
                ),
            },
            target_rel_path,
        )

    stamp_body_text = _read_file_at_revision(repo_root, stamp_commit, target_rel_path)
    stamp_computed_sha = (
        _git_hash_object_stdin(_frontmatter_body_text(stamp_body_text), repo_root)
        if stamp_body_text is not None
        else None
    )
    body_invariant_since_stamp = (
        stamp_computed_sha is not None and stamp_computed_sha == computed_sha
    )
    reproduces_at_stamp_commit = stamp_computed_sha == stamped_sha

    if body_invariant_since_stamp and not reproduces_at_stamp_commit:
        # The live defect this verdict was added for: the recorded value
        # reproduces at NO revision including its own stamp commit, while
        # the body hash is invariant across the range — a mis-computed
        # stamp, not a post-approval body edit. Must not fall through to
        # `_classify_stamp_delta` and be reported as a stale plan.
        return (
            {
                "verdict": "unstampable",
                "stamped_sha": stamped_sha,
                "computed_sha": computed_sha,
                "stamp_commit": stamp_commit,
                "delta_class": None,
                "next_move": (
                    f"Re-stamp execution_authorized_sha on {target_rel_path} to {computed_sha} "
                    "— the recorded value never reproduces the canonical recipe even at its own "
                    "stamp commit, and the plan body is unchanged since then: a mis-computed "
                    "stamp, not a body edit."
                ),
            },
            target_rel_path,
        )

    delta_class = _classify_stamp_delta(repo_root, stamp_commit, target_rel_path)
    if delta_class == "bookkeeping":
        verdict = "stale-bookkeeping"
        next_move = f"Re-stamp execution_authorized_sha on {target_rel_path} to {computed_sha} and proceed."
    else:
        verdict = "stale-substantive"
        next_move = (
            "Surface to the PM before proceeding — the plan changed target, scope, or "
            "acceptance criteria since it was stamped; re-authorization is a PM call."
        )
    return (
        {
            "verdict": verdict,
            "stamped_sha": stamped_sha,
            "computed_sha": computed_sha,
            "stamp_commit": stamp_commit,
            "delta_class": delta_class,
            "next_move": next_move,
        },
        target_rel_path,
    )


def build_execution_stamp_directive(execution_stamp_match: dict[str, Any], target_path: str) -> dict[str, Any]:
    """The tier-1 re-stamp `directives[]` entry for `stale-bookkeeping`/
    `unstampable` (AC18) — unconditional: the engine has already
    established the delta is bookkeeping-only (or that the recorded value
    never reproduced at all), so re-stamping is mechanical, not a call left
    to the EM. `restamp-execution-sha` is a registered verb on
    `archive-stamp-cli`'s dispatch table (`apply.py::_dispatch_archive_stamp_
    cli`, wired by chunk C9) — dispatching this directive re-stamps the
    target's `execution_sha` frontmatter field via a locked read-modify-
    write, exactly like any other archive-stamp-cli directive."""
    return {
        "id": "d-stamp",
        "cli": "archive-stamp-cli",
        "args": ["restamp-execution-sha", target_path, execution_stamp_match["computed_sha"]],
        "depends_on": None,
        "already_satisfied": False,
    }


def build_execution_stamp_judgment_point(execution_stamp_match: dict[str, Any]) -> dict[str, Any]:
    """The tier-3 `judgment_points[]` entry for `stale-substantive` (AC18).
    `resolves: []` on both dispositions — neither answer unblocks a
    directive by itself; the right response is exactly the open question.

    Tier: `insufficient-evidence` — `_classify_stamp_delta` pre-tags the
    diff's shape from engine-computed git state, not a quote of untrusted
    body text, but whether a substantive change is an acceptable one stays
    the EM's (and, per the contract, ultimately the PM's) read."""
    return build_judgment_point(
        "jstamp",
        "The plan changed since its execution_authorized_sha stamp — bookkeeping drift, "
        "or a substantive change requiring re-authorization?",
        "gates.execution_stamp_match",
        [
            {"value": "re-authorized-proceed", "resolves": []},
            {"value": "surface-to-PM", "resolves": []},
        ],
        None,
        reason="insufficient-evidence",
    )


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

class BriefResult:
    __slots__ = ("decision_object", "exit_code")

    def __init__(self, decision_object: dict[str, Any], exit_code: int):
        self.decision_object = decision_object
        self.exit_code = exit_code


#: The noun `_ready_summary` uses to state the claim as a plain fact. The EM's
#: first sentence is what it now HOLDS, never how that was worked out.
#:
#: Negative-spec — this narration never names the assembler, the brief, the
#: pre-compute, or the hook that fired it (PM ruling 2026-08-19, memo
#: `2026-08-19-claude-klabauter-em-pickup-skill-leads-with-the-assembler`): an EM
#: told a brief was computed goes looking for the thing that computed it, and
#: the observed failure was one narrating "I'll run the pickup assembler" and
#: then hand-reading the artifact the brief had already resolved. Offering the
#: machinery for inspection is the same defect in a softer register.
_CLASSIFICATION_NOUN: dict[str, str] = {
    "memo": "memo",
    "handoff": "handoff",
    "spinoff": "spinoff",
}


#: C4 (2) — classification-keyed "how you run with it" injection (gap-sweep
#: amendment), prepended onto `_ready_summary`'s shared future-tense
#: `next_move` sentence rather than a parallel narration builder: the
#: material differs (memo vs. handoff/spinoff) but the underlying "N ready /
#: M open" fact and its resolve-then-dispatch instruction are identical
#: across both `brief()` tails, so this dict extends the shared function's
#: output instead of duplicating it. Unrecognized/absent classification (a
#: defensive default — every live `_ready_summary` call site today passes
#: "handoff", "spinoff", or "memo") contributes no prefix.
_CLASSIFICATION_NEXT_MOVE_PREFIX: dict[str, str] = {
    "memo": "This is a memo — decide its disposition from the options below. ",
    "handoff": "Grab it and run with it — reconcile the pending list against reality. ",
    "spinoff": (
        "This is a spinoff — treat the handoff body as the ground-truth spec; do not "
        "hand-search for pre-existing in-progress work on it — its own declared "
        "successor chain, if any, surfaces mechanically as gates.successor above. "
    ),
}


def _ready_summary(
    classification: str, directives: list[dict[str, Any]], judgment_points: list[dict[str, Any]]
) -> tuple[str, str]:
    """Shared `(narration, next_move)` pair for a successful compute
    (`EXIT_OK`) that carries at least a `directives`/`judgment_points`
    shape — the handoff and memo tails of `brief()` converge on the same
    two sentences rather than each hand-composing its own (AC14's register
    separation: present-tense fact, then a distinct future-tense instruction).
    The fact is the claim the EM now holds — see `_CLASSIFICATION_NOUN`'s
    negative-spec for what it must never mention.

    `classification` (`"handoff"`/`"spinoff"`/`"memo"`) prepends a
    classification-keyed instruction onto the shared `next_move` sentence —
    "how you run with it depends on the material," delivered by the fired
    message rather than left to SKILL.md prose (C4 (2))."""
    blocked_by = [jp["id"] for jp in judgment_points if jp.get("id")]
    held = f"You hold this {_CLASSIFICATION_NOUN.get(classification, 'artifact')}."
    if blocked_by:
        narration = f"{held} {len(directives)} directive(s) ready, {len(blocked_by)} judgment point(s) open."
        next_move = "Resolve the open judgment point(s) before dispatching the ready directives."
    else:
        narration = f"{held} {len(directives)} directive(s) ready to run."
        next_move = "Coast is clear — dispatch the directives."
    prefix = _CLASSIFICATION_NEXT_MOVE_PREFIX.get(classification, "")
    return narration, prefix + next_move


def _emit(decision_object: dict[str, Any], exit_code: int) -> "BriefResult":
    """The single validation chokepoint every decision-object construction
    site routes through (AC14/AC15; AC5b's enforcement backstop per the Staff Engineer
    second-pass finding #4). This is the one place that can fail loud on a
    malformed decision object — every `BriefResult(...)` construction and
    every `main()` error payload calls this instead of building the result
    directly, so there is exactly one place to get this right rather than
    eleven independently-driftable ones.

    Fails loud (`ValueError`) on:
      (a) `narration` missing or empty — AC14. Non-negotiable regardless of
          verdict: even a clean, coast-clear compute owes the amnesiac EM a
          one-line statement of what just happened.
      (b) `next_move` missing or empty whenever `gates.coast.verdict` is not
          `"clear"` — AC15. A decision object with no `gates.coast` at all
          (the bare `{"error": ..., "transport_failure": True}` shape this
          chokepoint replaces) reads as verdict `None`, which is also not
          `"clear"` — so a transport-failure payload is held to the same
          bar, not exempted by virtue of having no `gates` object to check.
      (c) any `judgment_points[]` entry missing a `recommendation` key,
          REGARDLESS of whether that entry was built through the required-
          parameter constructor (AC5b) or assembled as a raw dict literal —
          the constructor alone binds only code that calls it (the Staff Engineer
          finding #4); this is the backstop that closes the gap.

    Negative-spec: does NOT invent a narration or next_move when one is
    missing — a caller that omits either field is a bug in this module to
    be fixed at the call site, never papered over here with a fabricated
    default.
    """
    narration = decision_object.get("narration")
    if not narration:
        raise ValueError("_emit: decision object missing non-empty 'narration'")

    coast_verdict = (((decision_object.get("gates") or {}).get("coast")) or {}).get("verdict")
    if coast_verdict != "clear":
        next_move = decision_object.get("next_move")
        if not next_move:
            raise ValueError(
                f"_emit: decision object with gates.coast.verdict={coast_verdict!r} "
                "missing non-empty 'next_move'"
            )

    for jp in decision_object.get("judgment_points") or []:
        if "recommendation" not in jp:
            raise ValueError(
                f"_emit: judgment_points entry {jp.get('id', '<no id>')!r} missing "
                "required 'recommendation' key"
            )

    return BriefResult(decision_object, exit_code)


def brief(
    artifact_path: str,
    decisions: Optional[dict[str, Any]] = None,
    repo_root: Optional[Path] = None,
    claim_at_brief: bool = False,
) -> BriefResult:
    """`brief <artifact-path> [--decisions <json>]` — the single-shot decision
    object computation (contract § two-phase-stateless protocol, resolved
    single-shot).

    Mutates no TRACKED content (AC3): every computation below reads disk and
    git state only, and `test_brief_mutates_nothing_on_disk` still holds the
    working tree clean across a call.

    `claim_at_brief` (default False) is the one exception, and it writes nothing
    tracked either — it takes the `brief`-stage mkdir lock under
    `.git/coordinator-sessions/`, so that the read-verify-draft window this
    brief opens is actually excluded against a concurrent pickup rather than
    only being checked once that window has already closed
    (`acquire_brief_claim`'s docstring has the incident). Acquisition happens
    only once the artifact has resolved to a live handoff/spinoff/memo that a
    pickup could actually proceed on — never for an archived, ambiguous,
    already-`actioned`, or wrong-addressee artifact, none of which reach an
    `apply`.

    It defaults False so that every in-process caller and every test that
    treats `brief()` as a pure computation keeps that guarantee; the CLI turns
    it on for a SINGLE-artifact `brief` (`brief_multi`), which is the shape
    that means "I am picking this up". A multi-artifact ` AND `/brace argument
    is a survey and claims nothing.
    """
    decisions = decisions or {}
    root = repo_root or resolve_repo_root()
    if root is None:
        raise _TransportFailure("could not resolve a git worktree root")

    # Set by the acquisition below when this brief took the lock off a
    # PREVIOUS holder; read by `_emit_elision_aware` so the reclaim is
    # narrated instead of vanishing behind the "you already hold this" the
    # grant reports the instant the takeover lands.
    reclaimed: list[dict[str, Any]] = []

    try:
        artifact = resolve_artifact(artifact_path, root)
    except _ArtifactUnreadable as exc:
        return _emit(
            {
                "artifact": {"path": artifact_path, "classification": "ambiguous", "frontmatter": {}, "resolution": None},
                "preflight": {"tree_quiescence": compute_tree_quiescence(root, []), "staleness": {}, "closure_signals": []},
                "gates": {"claim": {}, "addressee": {}, "branch": {}, "aging_verdict": "not_applicable", "coast": compute_coast([])},
                "directives": [],
                "judgment_points": [],
                "decisions": decisions,
                "error": str(exc),
                "narration": f"Could not resolve {artifact_path}.",
                "next_move": (
                    f"Confirm the path is correct — {exc}. Already searched every live dir "
                    f"({', '.join(LIVE_DIRS)}) and every archive dir ({', '.join(ARCHIVE_DIRS)})."
                ),
            },
            EXIT_BUSINESS_FAIL,
        )
    except _ArtifactElisionInconclusive as exc:
        candidates_str = ", ".join(exc.candidates)
        jp = build_judgment_point(
            "j-elision",
            f"Which baton does the elided path '{exc.artifact_path}' resolve to?",
            f"candidates: {candidates_str}",
            [{"value": c, "resolves": []} for c in exc.candidates],
            None,
            reason="insufficient-evidence",
        )
        return _emit(
            {
                "artifact": {
                    "path": exc.artifact_path,
                    "classification": "ambiguous",
                    "frontmatter": {},
                    "resolution": {"status": "elision_inconclusive", "candidates": exc.candidates},
                },
                "preflight": {"tree_quiescence": compute_tree_quiescence(root, []), "staleness": {}, "closure_signals": []},
                "gates": {"claim": {}, "addressee": {}, "branch": {}, "aging_verdict": "not_applicable", "coast": compute_coast([])},
                "directives": [],
                "judgment_points": [jp],
                "decisions": decisions,
                "narration": (
                    f"'{exc.artifact_path}' is an elided path that matches {len(exc.candidates)} "
                    f"candidates — cannot resolve without operator input: {candidates_str}."
                ),
                "next_move": "Pick the intended baton from the candidates and re-run brief with its literal path.",
            },
            EXIT_BUSINESS_FAIL,
        )

    elision_resolution = artifact.pop("elision_resolution", None)
    sanitize_resolution = artifact.pop("sanitize_resolution", None)
    suffix_resolution = artifact.pop("suffix_resolution", None)
    revision_resolution = artifact.pop("revision_resolution", None)

    def _emit_elision_aware(decision_object: dict[str, Any], exit_code: int) -> "BriefResult":
        """Wraps `_emit` for every return site below this point — prepends
        an audit-visible resolution sentence to `narration` when this
        `brief()` call resolved an elided path to a unique match (contract
        item 3: "never resolve silently"), and/or when the passed path only
        resolved after stripping surrounding prose punctuation (trailing
        `.`/`,`/etc., or a matched `(...)`/`[...]`/quote/backtick wrapper —
        2026-07-27 incident), and/or when the passed path only resolved by
        matching a SUFFIX of a basename with the `<date>-<sender>-` prefix
        omitted (2026-07-28 tier). A no-op on all three counts when
        `artifact_path` was passed clean."""
        if elision_resolution:
            note = (
                f"Resolved elided baton path '{elision_resolution['passed']}' -> "
                f"'{elision_resolution['resolved']}'. "
            )
            decision_object = {**decision_object, "narration": note + decision_object.get("narration", "")}
        if sanitize_resolution:
            # `passed` is rendered with its line breaks escaped: the sanitize
            # tier also repairs a hard line wrap, and a raw newline here would
            # split this one-line note across the narration.
            _passed = sanitize_resolution["passed"].replace("\r", "\\r").replace("\n", "\\n")
            note = (
                f"Passed path '{_passed}' only resolved after "
                f"trimming surrounding/trailing prose punctuation and rejoining "
                f"any hard line wrap -> "
                f"'{sanitize_resolution['resolved']}'. "
            )
            decision_object = {**decision_object, "narration": note + decision_object.get("narration", "")}
        if suffix_resolution:
            note = (
                f"Passed slug '{suffix_resolution['passed']}' had no exact match — resolved "
                f"via unique basename-suffix match -> '{suffix_resolution['resolved']}'. "
            )
            decision_object = {**decision_object, "narration": note + decision_object.get("narration", "")}
        if revision_resolution:
            note = (
                f"Passed revision '{revision_resolution['passed']}' resolved via its "
                f"delivery commit -> '{revision_resolution['resolved']}'. "
            )
            decision_object = {**decision_object, "narration": note + decision_object.get("narration", "")}
        if reclaimed:
            record = reclaimed[0]
            if record["basis"] == "expired-brief-lease":
                basis_phrase = f"its {_claims.BRIEF_CLAIM_LEASE_MINUTES}-minute brief-stage lease elapsed"
            elif record["basis"] == "dead-holder":
                basis_phrase = "that session's process was confirmed gone"
            elif record["basis"] == "holder-absent":
                # Review: staff-eng F2 — no local session dir AND no
                # harness-registry record for that session, so no process
                # check ever ran. Say so plainly rather than either
                # "confirmed gone" (no confirmation happened) or "inferred
                # from inactivity" (there was nothing to infer from).
                basis_phrase = (
                    "no session directory or harness-registry record could "
                    "be found for that session — liveness could not be "
                    "checked at all"
                )
            else:
                # "holder-liveness-unknown": the takeover happened (the lock
                # was takeable) but the only evidence was Layer 2 recency
                # inference, a fail-open "unknown" read, or a liveness
                # computation that disagreed with the one the takeover acted
                # on (F1) — never a confirmed-dead process. Say so, not
                # "reads dead" (2026-08-11 incident).
                basis_phrase = (
                    "that session had not refreshed its claim inside the "
                    "liveness window — liveness NOT confirmed dead, only "
                    "inferred from inactivity"
                )
            age = record.get("claim_age_minutes")
            age_phrase = f" (claim was {age}m old)" if age is not None else ""
            note = (
                f"RECLAIMED from session {record['holder']} — {basis_phrase}{age_phrase}. "
                f"That session may still believe it holds this; reconcile before acting "
                f"externally on it. "
            )
            decision_object = {**decision_object, "narration": note + decision_object.get("narration", "")}
            # `gates.claim_reclaim`, NOT a key inside `gates.claim_grant`:
            # `claim_grant` reports the CURRENT verdict against the state this
            # brief itself just read (post-reclaim), never the fact that a
            # takeover happened — folding the reclaim record into it would
            # either overwrite that current verdict or fabricate a second,
            # conflicting one. One field path, present for both the handoff
            # and memo branches (parity fix,
            # cross-repo/inbox/2026-08-17-doe-claude-em-memo-claim-fires-
            # after-the-em-can-already-act.md — the memo path is precisely
            # where the 2026-08-10 duplicate-memo incident happened), and no
            # fabricated half-populated `claim_grant`.
            gates = decision_object.get("gates")
            if isinstance(gates, dict):
                decision_object = {
                    **decision_object,
                    "gates": {**gates, "claim_reclaim": record},
                }
        return _emit(decision_object, exit_code)

    classification = artifact["classification"]
    fm = artifact["frontmatter"]
    basename = Path(artifact["path"]).name

    if classification == "archived":
        resolution = artifact.get("resolution") or {}
        archive_path = resolution.get("archive_path", "an archive directory")
        terminal_fields = resolution.get("terminal_fields") or {}
        archived_class = resolution.get("archived_class")
        base_narration = f"{artifact['path']} is archived at {archive_path} — a terminal record."
        base_next_move = "Nothing further to do — this artifact already closed."
        # "from" in terminal_fields doubles as the memo-vs-handoff
        # discriminator here (see `_TERMINAL_MEMO_FIELDS`'s comment) — an
        # archived handoff's terminal_fields never carries that key, so the
        # reply-closure check (memo-only, 2026-07-25 defect fix) only runs
        # when this archived artifact actually is a memo.
        if "from" in terminal_fields:
            closure = compute_reply_closure(terminal_fields, artifact["path"], root)
            reply_jps, narration, next_move = _render_reply_closure(
                closure,
                artifact["path"],
                base_narration,
                base_next_move,
                status=terminal_fields.get("status"),
            )
        else:
            reply_jps, narration, next_move = [], base_narration, base_next_move

        # 2026-07-27 doe-claude-em memo defect fix: an archived MEMO whose
        # terminal `status` is not already `"actioned"` was swept into the
        # archive without ever having a disposition stamped — until now
        # this branch unconditionally emitted `directives: []` and asserted
        # "already closed" for it regardless, leaving no directive-driven
        # path to discharge it. The discriminator is `resolution.
        # archived_class` (built in `_build_archived_resolution`), NOT the
        # `"from" in terminal_fields` heuristic used just above for the
        # reply-closure gate — that call site is left alone on purpose (a
        # sibling concern, not this one).
        kind_directives: list[dict[str, Any]] = []
        kind_jps: list[dict[str, Any]] = []
        if archived_class == "memo" and terminal_fields.get("status") not in _MEMO_TERMINAL_STATUS:
            kind_directives, kind_jps = _archived_open_memo_kind_dispatch(
                artifact["path"], terminal_fields, decisions
            )
            status_display = terminal_fields.get("status") or "open"
            narration = (
                f"{narration} status: {status_display} has no disposition stamped yet — "
                "this memo is not actually closed."
            )
            next_move = "Resolve the open judgment point(s) before dispatching the ready directive(s)."

        judgment_points = reply_jps + kind_jps
        return _emit_elision_aware(
            {
                "artifact": artifact,
                "preflight": {"tree_quiescence": compute_tree_quiescence(root, fm.get("scope", []) or []), "staleness": {}, "closure_signals": []},
                "gates": {"claim": {}, "addressee": {}, "branch": {}, "aging_verdict": "not_applicable", "coast": compute_coast(judgment_points)},
                "directives": kind_directives,
                "judgment_points": judgment_points,
                "decisions": decisions,
                "narration": narration,
                "next_move": next_move,
            },
            EXIT_OK,
        )

    if classification == "ambiguous":
        return _emit_elision_aware(
            {
                "artifact": artifact,
                "preflight": {"tree_quiescence": compute_tree_quiescence(root, fm.get("scope", []) or []), "staleness": {}, "closure_signals": []},
                "gates": {"claim": {}, "addressee": {}, "branch": {}, "aging_verdict": "not_applicable", "coast": compute_coast([])},
                "directives": [],
                "judgment_points": [],
                "decisions": decisions,
                "narration": f"Could not classify {artifact['path']} against the handoff/spinoff/memo shape.",
                "next_move": "Read the artifact directly and confirm its kind by hand before proceeding.",
            },
            EXIT_BUSINESS_FAIL,
        )

    scope_entries = fm.get("scope", []) or []
    tree_quiescence = compute_tree_quiescence(root, scope_entries)
    staleness = compute_staleness(root)
    branch_gate = compute_branch_gate(root, classification=classification)
    liveness_fired = compute_liveness_signal(root, fm, artifact["path"])
    execution_stamp_hit = compute_execution_stamp_match(root, fm, artifact["path"])
    execution_stamp_match = execution_stamp_hit[0] if execution_stamp_hit else None
    execution_stamp_target = execution_stamp_hit[1] if execution_stamp_hit else None

    if classification in ("handoff", "spinoff"):
        artifact_live_path = root / artifact["path"]
        aging_verdict = compute_aging_verdict(artifact_live_path)
        # Claim FIRST, then read the claim state: both reads below must see
        # the post-acquisition truth, or this brief would narrate "no
        # competing claim" about a lock it just took itself.
        if claim_at_brief:
            took_from = acquire_brief_claim(root, "handoff", basename)
            if took_from:
                reclaimed.append(took_from)
            # C5: routes into baton_assemble's multi-leg unification when
            # C4's verdict says proceed and the predicate is on; a plain
            # advisory append (`_adopt_into_baton`) otherwise. See
            # `route_baton_adoption`'s own docstring.
            route_baton_adoption(root, artifact["path"], fm)
        claim = compute_claim_gate(root, "handoff", basename)
        claim_grant = compute_claim_grant(root, "handoff", basename, artifact["path"], cwd=str(root), fm=fm)
        # Self-claim idempotence (2026-07-29): `d2` (archive-stamp-cli's
        # frontmatter mutation) is already-satisfied only when THIS session
        # holds the claim (`claim_grant.held_by_self`) AND the frontmatter
        # already shows `status: claimed` — i.e. a prior pass in this same
        # session already landed it. `held_by_self` alone is not enough:
        # `compute_claim_grant` can also grant on a lineage handover
        # (a DIFFERENT session's claim), where re-stamping is still required.
        #
        # Ledger-first (C11, row 35): the AND's second conjunct used to be a
        # raw `fm.get("status") == "claimed"` frontmatter-only read, which is
        # exactly the branch-switch-revert desync case — a session that
        # already landed the stamp on one branch reads as never-stamped on a
        # branch that never carried that commit, so the AND collapses to
        # False, `d2` re-emits, and `/pickup` re-stamps a baton this session
        # already holds, clobbering `claimed_at` (fact-find row 35). Routed
        # through `claim_state.resolve_claim_state` instead: `claim_state.
        # holder is not None` answers "has a claim already landed for this
        # artifact" ledger-first, with the frontmatter mirror as fallback,
        # so a ledger-confirmed prior stamp still satisfies the idempotence
        # check even when the mirror has reverted.
        #
        # Landed-stamp gate (cross-repo/inbox/2026-08-13-doe-claude-em-pickup-
        # already-satisfied-masks-a-refused-write.md, repairing the memo
        # 2026-08-11-doe-claude-em-pickup-claim-never-reaches-frontmatter
        # fallback below): `claim_state.holder is not None` alone is
        # satisfied by the `brief`-stage reservation `acquire_brief_claim`
        # just took a few lines above (same ledger dir `resolve_claim_state`
        # reads) — a pre-work lock, not evidence `d2` (archive-stamp-cli's
        # durable frontmatter mutation) ever landed. The fallback used to read
        # `claim_stage(...) == CLAIM_STAGE_APPLY`, but `apply.py::apply`
        # promotes `brief` -> `apply` UNCONDITIONALLY, immediately BEFORE the
        # directives (including `d2`) execute — so `apply` stage is reachable
        # on a REFUSED stamp attempt exactly as much as on a landed one (e.g.
        # a schema-violating handoff whose EVERY lifecycle write fails loud)
        # and is never reverted on that failure. That let `d2` be reported
        # `already_satisfied` for a write that never happened, permanently
        # (a directive believed satisfied never re-fires, and `drop` refuses
        # once `deployment_state` is terminal — no repair path).
        #
        # Fixed by reading the durable `stamped` marker (`session.claims.
        # claim_stamped`) instead — written ONLY by `apply.py`'s
        # `_dispatch_archive_stamp_cli` AFTER `cs_claim_handoff` has confirmed
        # its own post-write `_validate_fm` pass succeeded, so it cannot exist
        # on a refused write. It also still serves the ORIGINAL fallback's
        # purpose (branch-switch-revert desync, C11 row 35): the marker lives
        # in the same claim dir under `.git/`, which survives a branch switch
        # that reverts the tracked frontmatter mirror, so a landed stamp
        # remains visible even when the mirror has reverted. A mirror-sourced
        # holder (the `mirror_holder` branch above) still counts regardless,
        # since it has no stage/marker concept and is itself already-landed
        # frontmatter.
        #
        # Back-compat: a claim dir written before this marker existed carries
        # no `stamped` file, so `stamp_evidence` reads False for it and `d2`
        # re-emits as unsatisfied on the next brief — safe, since
        # `handoff_transition._claim` is idempotent at the full target state
        # (`status: claimed` + `deployment_state: in_flight` + matching
        # `claimed_by`), returning `applied: False` / exit 0 rather than a
        # duplicate mutation. No migration needed.
        claim_state = resolve_claim_state(root / artifact["path"], repo_root=root)
        stamp_evidence = claim_state.mirror_holder is not None
        if not stamp_evidence and claim_state.ledger_holder is not None:
            try:
                _common_dir_for_stage = lifecycle.git_common_dir(root)
            except Exception:
                _common_dir_for_stage = None
            if _common_dir_for_stage is not None:
                _handoff_claim_dir = handoff_claim_dir(_common_dir_for_stage, root / artifact["path"])
                stamp_evidence = _claims.claim_stamped(_handoff_claim_dir)
        self_claimed_in_frontmatter = bool(claim_grant.get("held_by_self")) and stamp_evidence
        directives = build_handoff_directives(
            artifact["path"], claim["holder"], basename, self_claimed_in_frontmatter=self_claimed_in_frontmatter
        )
        preflight_evidence = compute_handoff_preflight(root, artifact, fm, scope_entries)
        preflight = {"tree_quiescence": tree_quiescence, "staleness": staleness, **preflight_evidence}

        # Step 5.5a/b — Function 6 wiring (Finding 1): completeness-checklist
        # parse + hoist evidence is real preflight content regardless of
        # which handoff sub-path returns below; the mirror-init directives
        # and probe-confirmation judgment points (Step 5.5c) are folded into
        # the main path's directives[]/judgment_points[] further down, never
        # into the live-claim-holder early bail (that path already emits no
        # directives — a business failure, nothing left to mirror-init).
        completeness = build_completeness_checklist(fm, artifact["path"])
        preflight["completeness_items"] = completeness["items"]
        preflight["completeness_batches"] = completeness["batches"]

        if claim["holder"] is not None and claim_grant.get("verdict") != "granted":
            # Self-claim / handover fix (PM ruling 2026-07-24,
            # pickup-skill-code-driven-branch-result spinoff, AC2): a holder
            # this session already holds (or is a lineage handover) is
            # `claim_grant.verdict == "granted"` — `compute_claim_grant`
            # already resolves that cleanly ("you already hold this" /
            # handover narration). Gating this whole early business-fail
            # bail on the coarser `claim["holder"]` alone re-surfaced the
            # exact self-claim false alarm this spinoff exists to close: an
            # EM re-picking up its OWN claimed handoff got told to "confirm
            # by hand whether that session is still active" about itself.
            # Only a claim `compute_claim_grant` actually DENIES falls
            # through to this stand-down path now.
            #
            # Defect 5 — the live-claim stand-down still builds the liveness
            # judgment point (no longer `revalidate_at_dispatch` — chunk C7
            # Part A4 — since the stamp-read `liveness_fired` is now stable
            # across the brief-to-apply gap) rather than dropping it
            # silently; the EM's only offer text on this path.
            #
            # phantom-resolves-sweep fix (2026-07-27) — this decision
            # object's own `directives` is emitted as `[]` a few lines
            # below (nothing survives this business-fail bail to mirror-
            # init or claim), so a `"proceed"` disposition naming
            # `resolves: ["d2"]` here names a directive id THIS pass never
            # emits — dead by construction, since `directive_gate_open`
            # (`apply_base.py`) only ever walks the SAME decision object's
            # `directives[]`, which is empty. Passing `resolves: []`
            # instead makes the emitted judgment point honestly inert:
            # "proceed" just closes out this stand-down offer, exactly as
            # `claim_next_move`'s own text below promises ("Resolve the
            # open judgment point(s) below before proceeding") — proceeding
            # here means re-running `brief()` once the peer's liveness
            # clears, not that this pass silently unblocks a `d2` that was
            # never built. The main handoff/spinoff path (this same
            # function, `liveness_jp` a few dozen lines further down) still
            # passes `["d2"]` unchanged — that call site's `directives` is
            # real and non-empty, so `d2` genuinely exists there to gate.
            live_claim_jp = build_liveness_judgment_point(liveness_fired, "gates.liveness_signal", [])
            # Review: code-reviewer — Finding 4: `build_competing_claim_judgment_point`
            # is retired to an always-`None` no-op (PM ruling 2026-07-24,
            # pickup-skill-code-driven-branch-result spinoff) — it can never
            # contribute a judgment point here, so the call site is dropped
            # rather than kept as a permanently-dead invocation.
            live_claim_judgment_points = [jp for jp in (live_claim_jp,) if jp]
            if live_claim_judgment_points:
                claim_next_move = "Resolve the open judgment point(s) below before proceeding."
            else:
                claim_next_move = (
                    f"{claim['holder']} holds the claim but no live signal fired for it — "
                    "confirm by hand whether that session is still active before proceeding."
                )
            return _emit_elision_aware(
                {
                    "artifact": artifact,
                    "preflight": preflight,
                    "gates": {
                        "claim": claim,
                        "claim_grant": claim_grant,
                        "branch": branch_gate,
                        "gate_notes": compute_gate_notes(fm),
                        "aging_verdict": aging_verdict,
                        "liveness_signal": liveness_fired,
                        "coast": compute_coast(
                            live_claim_judgment_points, claim_grant=claim_grant, tree_quiescence=tree_quiescence
                        ),
                    },
                    "directives": [],
                    "judgment_points": live_claim_judgment_points,
                    "decisions": decisions,
                    "narration": f"{artifact['path']} is already claimed by {claim['holder']}.",
                    "next_move": claim_next_move,
                },
                EXIT_BUSINESS_FAIL,
            )

        judgment_points = []
        gate_check = None
        shipped_state = None

        # Defect 3 — awaiting_gate never gets an unconditional claim; the
        # gate-clearance call is a JUDGMENT entry the EM resolves, not an
        # auto-directive.
        if fm.get("deployment_state") == "awaiting_gate":
            # Piece B — widen the bundle beyond gate_dependency/aging_verdict
            # so the EM sees the two fields a "cleared" answer strands
            # unread otherwise (`fm` is already in scope; both reads are
            # same-scope, no new I/O). `blocking_notes` stays advisory
            # prose per schema (never resolver-read) — surfaced here only
            # so the EM sees what its own claim would orphan, same as
            # `blocked_by`.
            gate_check = {
                "gate_dependency": fm.get("gate_dependency"),
                "aging_verdict": aging_verdict,
                "blocked_by": fm.get("blocked_by"),
                "blocking_notes": fm.get("blocking_notes"),
                "gate_evidence": fm.get("gate_evidence"),
            }
            # C7 enrichment — reads ONLY this record's own `gate_evidence`
            # field (no corpus walk); see `compute_gate_shipped_blocker_
            # evidence`'s docstring for the narrowing rationale.
            shipped_blocker = compute_gate_shipped_blocker_evidence(root, fm.get("gate_evidence"))
            if shipped_blocker is not None:
                gate_check["shipped_blocker"] = shipped_blocker
                gate_jp = build_gate_check_judgment_point(
                    "gates.gate_check",
                    ["d2", "d-gate-recheck"],
                    recommendation={
                        "disposition": "cleared",
                        "rationale": (
                            f"gate_evidence leg {shipped_blocker['leg_id']!r} names "
                            f"commit-sha {shipped_blocker['sha']}, resolvable in this "
                            "repo — the named blocker appears shipped."
                        ),
                    },
                )
            else:
                gate_jp = build_gate_check_judgment_point(
                    "gates.gate_check", ["d2", "d-gate-recheck"]
                )
            judgment_points.append(gate_jp)
        elif fm.get("deployment_state") == "shipped":
            # 2026-07-25 defect fix — a shipped handoff previously briefed as
            # freely dispatchable (`gates.coast.verdict == "clear"`,
            # `judgment_points: []`), telling a peer session to redo finished
            # work. `deployment_state: shipped` is mutually exclusive with
            # `awaiting_gate` on this same field, mirrored as a sibling
            # elif — never a hard block (re-opening a shipped baton is
            # legitimate), always a surfaced judgment point (design call,
            # dispatch brief 2026-07-25).
            shipped_state = {
                "deployment_state": "shipped",
                "shipped_in": fm.get("shipped_in"),
            }
            shipped_jp = build_shipped_state_judgment_point("gates.shipped_state", ["d2"])
            judgment_points.append(shipped_jp)

        liveness_jp = build_liveness_judgment_point(liveness_fired, "gates.liveness_signal", ["d2"])
        if liveness_jp:
            judgment_points.append(liveness_jp)

        # `depends_on` for d2 (claim-handoff) — AND-semantics across every
        # blocking judgment point that independently claims to gate it
        # (contract § "The list form of `depends_on`"): `jgate` (awaiting_gate,
        # never co-occurs with `jshipped` — same frontmatter field), `jshipped`
        # (shipped), and `j1` (a firing liveness signal, orthogonal to both).
        # A single blocker keeps the plain string form (contract: "do not
        # gratuitously wrap single gates in one-element lists"); zero blockers
        # -> unconditional (`None`), matching the pre-existing behavior this
        # generalizes. Carve-out (Piece A, this same block): when `gate_check`
        # is set, `d-gate-recheck`'s id always rides alongside `jgate` below,
        # so the single-judgment-point `awaiting_gate` case is never scalar
        # post-Piece-A — two ids minimum whenever this branch fires. Review:
        # coordinator:code-reviewer — the old scalar-pinning test name/contract
        # text had no pointer to this exception.
        blocking_ids: list[str] = []
        if gate_check is not None:
            blocking_ids.append("jgate")
            # Piece A ordering — `d-gate-recheck`'s id rides in this same
            # AND-list purely so `order_by_depends_on` sequences it before
            # `d2`; `directive_gate_open` skips any `depends_on` entry that
            # is not a live judgment-point id, so this contributes no
            # second gate on `d2`'s own resolution (still `jgate` alone).
            # Review: staff-eng — built here, at the use site, rather than
            # bound ~70 lines earlier: nothing between the two points read
            # the intermediate, so the temp was possibly-unbound fragility
            # with no benefit (Pyright flagged it).
            directives.append(build_gate_recheck_directive(artifact["path"]))
            blocking_ids.append("d-gate-recheck")
        if shipped_state is not None:
            blocking_ids.append("jshipped")
        if liveness_jp:
            blocking_ids.append("j1")

        if not blocking_ids:
            directives[1]["depends_on"] = None
        elif len(blocking_ids) == 1:
            directives[1]["depends_on"] = blocking_ids[0]
        else:
            directives[1]["depends_on"] = blocking_ids

        # AC3/AC3e — `gates.competing_claim` (computed above, always threaded
        # into `gates_obj` below) is informational-only post-retirement
        # (PM ruling 2026-07-24, pickup-skill-code-driven-branch-result
        # spinoff): `build_competing_claim_judgment_point` always returns
        # `None` now, so no verdict here ever contributes a judgment point
        # or blocks `gates.coast` — the call site is dropped rather than
        # kept as a permanently-dead invocation (Finding 4).

        # Step 5.5c — completeness-checklist directives (coordinator-tasks-
        # mirror `init`, one per item, restart-gated-hoisted) and Step 5.5's
        # probe-confirmation judgment points are independent of d1/d2's
        # claim chain — appended, not gated behind j1/jgate.
        directives = directives + completeness["directives"]
        judgment_points = judgment_points + completeness["judgment_points"]

        # AC18 — the pre-tagged tier split: `stale-bookkeeping`/`unstampable`
        # promote to an unconditional re-stamp directive; `stale-substantive`
        # stays a judgment point the EM (and PM) resolves. `match` and a
        # `None` hit contribute neither.
        if execution_stamp_match is not None and execution_stamp_target is not None:
            stamp_verdict = execution_stamp_match["verdict"]
            if stamp_verdict in ("stale-bookkeeping", "unstampable"):
                directives.append(
                    build_execution_stamp_directive(execution_stamp_match, execution_stamp_target)
                )
            elif stamp_verdict == "stale-substantive":
                judgment_points.append(build_execution_stamp_judgment_point(execution_stamp_match))

        gates_obj: dict[str, Any] = {
            "claim": claim,
            "claim_grant": claim_grant,
            "branch": branch_gate,
            "gate_notes": compute_gate_notes(fm),
            "aging_verdict": aging_verdict,
            "liveness_signal": liveness_fired,
            "coast": compute_coast(judgment_points, claim_grant=claim_grant, tree_quiescence=tree_quiescence),
        }
        if gate_check is not None:
            gates_obj["gate_check"] = gate_check
        if shipped_state is not None:
            gates_obj["shipped_state"] = shipped_state
        if execution_stamp_match is not None:
            gates_obj["execution_stamp_match"] = execution_stamp_match

        sizing_disposition = compute_sizing_disposition(root, fm)

        narration, next_move = _ready_summary(classification, directives, judgment_points)
        next_move = unsized_next_move_prefix(sizing_disposition) + next_move
        if claim_grant.get("held_by_self"):
            # 2026-07-29 self-claim narration fix — an EM re-briefing an
            # artifact it claimed itself must be told plainly it already
            # holds the baton, never left to infer that from a bare
            # `claimed_by: <its own sid>` string match. Prepended (not
            # replacing) so the existing "N directive(s) ready"/"M judgment
            # point(s) open" facts _ready_summary already computes still
            # show through unchanged.
            narration = f"Already held by you — resuming, not contending. {narration}"
            if not judgment_points:
                next_move = "Already held by you — resume. " + next_move
        return _emit_elision_aware(
            {
                "artifact": artifact,
                "preflight": preflight,
                "sizing_disposition": sizing_disposition,
                "gates": gates_obj,
                "directives": directives,
                "judgment_points": judgment_points,
                "decisions": decisions,
                "narration": narration,
                "next_move": next_move,
            },
            EXIT_OK,
        )

    # classification == "memo"

    # Defect 2 — M0 short-circuit: an already-`actioned` memo is a read-only
    # terminal artifact. Surface the terminal fields as context; emit no
    # claim directive; do not re-run M3 kind-dispatch on it.
    if fm.get("status") in _MEMO_TERMINAL_STATUS:
        memo_status = fm.get("status")
        terminal_state = {
            "status": memo_status,
            "decision": fm.get("decision"),
            "decision_note": fm.get("decision_note"),
            "actioned_note": fm.get("actioned_note"),
            "realized_by": fm.get("realized_by"),
            "superseded_by": fm.get("superseded_by"),
        }
        closure = compute_reply_closure(fm, artifact["path"], root)
        reply_jps, narration, next_move = _render_reply_closure(
            closure,
            artifact["path"],
            f"{artifact['path']} is an {memo_status} memo — a terminal record.",
            "Nothing further to do — this memo already closed.",
            status=fm.get("status"),
        )
        return _emit_elision_aware(
            {
                "artifact": {**artifact, "terminal_state": terminal_state},
                "preflight": {"tree_quiescence": tree_quiescence, "staleness": staleness, "closure_signals": []},
                "gates": {
                    "addressee": {},
                    "branch": branch_gate,
                    "gate_notes": compute_gate_notes(fm),
                    "aging_verdict": "not_applicable",
                    "liveness_signal": liveness_fired,
                    "sender_reachability": compute_sender_reachability(fm.get("sent_by")),
                    "coast": compute_coast(reply_jps, tree_quiescence=tree_quiescence),
                },
                "directives": [],
                "judgment_points": reply_jps,
                "decisions": decisions,
                "narration": narration,
                "next_move": next_move,
            },
            EXIT_OK,
        )

    to_value = fm.get("to")
    addressee = compute_addressee_gate(root, to_value)
    if addressee.get("checked") and addressee.get("exit_code") not in (0, None):
        override = os.environ.get("COORDINATOR_OVERRIDE_MEMO_ADDRESSEE")
        if not override:
            return _emit_elision_aware(
                {
                    "artifact": artifact,
                    "preflight": {"tree_quiescence": tree_quiescence, "staleness": staleness, "closure_signals": []},
                    "gates": {
                        "addressee": {
                            **addressee,
                            "cross_seat_override": "COORDINATOR_OVERRIDE_MEMO_ADDRESSEE",
                        },
                        "branch": branch_gate,
                        "gate_notes": compute_gate_notes(fm),
                        "aging_verdict": "not_applicable",
                        "liveness_signal": liveness_fired,
                        "coast": compute_coast([], tree_quiescence=tree_quiescence),
                    },
                    "directives": [],
                    "judgment_points": [],
                    "decisions": decisions,
                    "narration": (
                        f"{artifact['path']} names an addressee this session is not — "
                        f"{addressee.get('message', 'addressee mismatch')}."
                    ),
                    "next_move": (
                        "Confirm the addressee, or set COORDINATOR_OVERRIDE_MEMO_ADDRESSEE "
                        "if you and the PM judge otherwise."
                    ),
                },
                EXIT_BUSINESS_FAIL,
            )

    # Defect 4 — M3 kind-dispatch judgment point: surfaces ask/consult/
    # proposal/fyi as an overridable offer, never a verdict. Computed before
    # `build_memo_directives` (C8) so the `d-action-memo` directive's args can
    # resolve the EM's already-recorded `j-kind` disposition on this same
    # round-trip, rather than needing a third pass.
    kind_resolved, kind_unrecognized = resolve_memo_kind(fm)

    # Placed after the M0 `actioned` short-circuit and the addressee gate,
    # both of which return without any path to an `apply` — an artifact this
    # session must not action is one it must not lock either. Everything from
    # here on IS the unguarded window the 2026-08-10 duplicate-memo incident
    # ran through (`acquire_brief_claim`), so this is where the lock belongs.
    if claim_at_brief:
        took_from = acquire_brief_claim(root, "memo", basename)
        if took_from:
            reclaimed.append(took_from)
        # C5: same routing seam as the handoff branch above.
        route_baton_adoption(root, artifact["path"], fm)

    # Memo/handoff parity fix (cross-repo/inbox/2026-08-17-doe-claude-em-memo-
    # claim-fires-after-the-em-can-already-act.md) — the handoff branch above
    # computes `claim`/`claim_grant` and emits both under `gates` right after
    # its own `acquire_brief_claim` call (~L8167-8168); this memo branch used
    # to compute neither, so a memo brief carried no `gates.claim_grant` key
    # at all and `acquire_brief_claim`'s docstring promise — that a FAILED
    # brief-stage acquisition is narrated by "the `compute_claim_grant` call
    # that follows" — was never kept for memos. A live peer holding only the
    # brief-stage lock (no durable frontmatter stamp yet, so
    # `gates.liveness_signal` never catches it) meant the second session's
    # brief emitted a fully actionable object with no denial anywhere.
    claim = compute_claim_gate(root, "memo", basename)
    claim_grant = compute_claim_grant(root, "memo", basename, artifact["path"], cwd=str(root), fm=fm)

    if claim["holder"] is not None and claim_grant.get("verdict") != "granted":
        # Mirrors the handoff branch's live-claim-holder stand-down
        # (~L8256-8331) exactly: a DENIED grant against a live foreign
        # holder halts before the memo body is worth reading — no
        # directives, a liveness judgment point as the only offer. Row 2 of
        # `compute_claim_grant` (`held_by_self`) resolves `granted` for a
        # same-session re-brief, so this branch is never reached for that
        # case — see `_claim_already_self_held` below for the idempotence
        # check on top of that.
        live_claim_jp = build_liveness_judgment_point(liveness_fired, "gates.liveness_signal", [])
        live_claim_judgment_points = [jp for jp in (live_claim_jp,) if jp]
        if live_claim_judgment_points:
            claim_next_move = "Resolve the open judgment point(s) below before proceeding."
        else:
            claim_next_move = (
                f"{claim['holder']} holds the claim but no live signal fired for it — "
                "confirm by hand whether that session is still active before proceeding."
            )
        return _emit_elision_aware(
            {
                "artifact": artifact,
                "preflight": {"tree_quiescence": tree_quiescence, "staleness": staleness, "closure_signals": []},
                "gates": {
                    "claim": claim,
                    "claim_grant": claim_grant,
                    "addressee": addressee,
                    "branch": branch_gate,
                    "gate_notes": compute_gate_notes(fm),
                    "aging_verdict": "not_applicable",
                    "liveness_signal": liveness_fired,
                    "sender_reachability": compute_sender_reachability(fm.get("sent_by")),
                    "coast": compute_coast(
                        live_claim_judgment_points, claim_grant=claim_grant, tree_quiescence=tree_quiescence
                    ),
                },
                "directives": [],
                "judgment_points": live_claim_judgment_points,
                "decisions": decisions,
                "narration": f"{artifact['path']} is already claimed by {claim['holder']}.",
                "next_move": claim_next_move,
            },
            EXIT_BUSINESS_FAIL,
        )

    directives = build_memo_directives(artifact["path"], kind_resolved, decisions)
    if _claim_already_self_held(root, "memo", basename):
        # Idempotent same-session re-entry (see _claim_already_self_held's
        # docstring) — d1 (claim-artifact) already landed for THIS session on
        # a prior partial apply; skip re-dispatching its handler rather than
        # calling claim_artifact again and hard-failing on the designed
        # same-session-reclaim rejection. claim-memo-stamp (directives[1])
        # needs no equivalent flag — it relies on cs_claim_memo_stamp's own
        # no-op-on-self-reclaim behaviour (build_memo_directives' docstring).
        directives[0]["already_satisfied"] = True
    judgment_points = []
    # Review: code-reviewer — Finding 1: mirror the handoff branch's pattern
    # (directives[1]["depends_on"] = "j1" at ~L1374). A firing liveness signal
    # must gate d1 (claim-artifact memo); previously only the non-firing
    # else-branch touched depends_on, a no-op since it was already None, so a
    # live peer signal never actually gated the claim directive.
    #
    # C8 BUILD (1): `claim-memo-stamp` (directives[1]) mirrors `d1`'s grab
    # mechanics exactly — same `depends_on`, gated together, never
    # `j-kind`-gated (that's `d-action-memo`/directives[2] alone).
    #
    # Defect fix (2026-07-25, live repro this session): `resolves` here MUST
    # be every directive id `build_memo_directives` emits, `d-action-memo`
    # included — derived from `directives` itself, never a second
    # hand-maintained literal list. `d-action-memo["depends_on"]` below is
    # widened to `["j-kind", "j1"]` once `j1` fires (AND semantics), so if
    # `j1`'s `proceed` disposition does not ALSO resolve `d-action-memo`, the
    # terminal memo write becomes structurally unreachable the moment
    # liveness fires — identical in shape to the bug fixed in `8d94ebb9`.
    jp = build_liveness_judgment_point(
        liveness_fired, "gates.liveness_signal", [d["id"] for d in directives]
    )
    if jp:
        judgment_points.append(jp)
        directives[0]["depends_on"] = "j1"
        directives[1]["depends_on"] = "j1"
        # Review: code-reviewer — Finding 1: `d-action-memo` (directives[2])
        # was never widened to also require `j1` when liveness fires, so a
        # live-foreign-peer stand-down was bypassable by resolving only
        # `j-kind`. List-form depends_on carries AND semantics (mirrors the
        # handoff branch's `jgate`+`j1` co-gating above); `j1`'s sole
        # disposition never resolves, so this makes the terminal write
        # correctly and permanently unreachable under live contention.
        directives[2]["depends_on"] = ["j-kind", "j1"]
    else:
        directives[0]["depends_on"] = None
        directives[1]["depends_on"] = None

    judgment_points.append(build_kind_dispatch_judgment_point(kind_resolved, fm.get("kind"), kind_unrecognized))

    # AC3/AC3e — same informational-only `gates.competing_claim` handling as
    # the handoff branch (Finding 4): no call site here either, since
    # `build_competing_claim_judgment_point` always returns `None`.

    narration, next_move = _ready_summary(classification, directives, judgment_points)
    return _emit_elision_aware(
        {
            "artifact": {**artifact, "kind_resolved": kind_resolved},
            "preflight": {"tree_quiescence": tree_quiescence, "staleness": staleness, "closure_signals": []},
            "gates": {
                "claim": claim,
                "claim_grant": claim_grant,
                "addressee": addressee,
                "branch": branch_gate,
                "gate_notes": compute_gate_notes(fm),
                "aging_verdict": "not_applicable",
                "liveness_signal": liveness_fired,
                "sender_reachability": compute_sender_reachability(fm.get("sent_by")),
                "coast": compute_coast(judgment_points, claim_grant=claim_grant, tree_quiescence=tree_quiescence),
            },
            "directives": directives,
            "judgment_points": judgment_points,
            "decisions": decisions,
            "narration": narration,
            "next_move": next_move,
        },
        EXIT_OK,
    )


#: Standalone multi-artifact join token. `/pickup` is explicitly multi-artifact
#: ("one decision object per artifact") and callers commonly join paths with the
#: bare word ` AND `. Split is whitespace-bounded and case-sensitive on purpose:
#: the `\s+AND\s+` boundary never fires inside a path segment (`/BRAND/`,
#: `COMMAND.md`, a lowercase `and`), so single-path behavior is untouched.
_ARTIFACT_JOIN_RE = re.compile(r"\s+AND\s+")

#: A markdown list-item bullet at the start of a line — optional leading
#: horizontal whitespace (a nested/indented sub-bullet renders the same as a
#: top-level one when a caller pastes a bullet list; both mean "one more
#: artifact"), then a literal `-`/`*` marker, then required whitespace before
#: the path itself.
_BULLET_PREFIX_RE = re.compile(r"^[ \t]*[-*][ \t]+")

#: Trailing EM-facing aside marker (contract § Multi-Artifact Grab:
#: `pickup a AND b -- <free text>`). Recognized once, at the FIRST standalone
#: ` -- ` (whitespace-bounded on both sides, so a hyphenated path segment is
#: never mistaken for it) — everything from there to the end of the argument
#: is the aside, not an artifact-path candidate.
_ASIDE_RE = re.compile(r"\s+--\s+")


def _strip_aside(artifact_arg: str) -> str:
    """Strips a documented trailing ` -- <prose>` EM-facing aside off the
    whole multi-artifact argument before any path-splitting runs, so the
    aside is never glued onto the last artifact's path (2026-08-11 defect:
    an unstripped aside on an ` AND `-joined argument corrupted the final
    path's resolution instead of surfacing as prose).

    Matches ONCE, against the raw argument as a whole — the aside is
    documented as trailing the entire multi-artifact expression, not any
    one bullet line, so this runs before bullet-line splitting reassembles
    a newline-separated grab into the ` AND `-joined form below.
    """
    match = _ASIDE_RE.search(artifact_arg)
    if match is None:
        return artifact_arg
    return artifact_arg[: match.start()]


def _reassemble_bullet_lines(artifact_arg: str) -> str:
    """Recombines a pasted markdown bullet list of artifact paths — one per
    line, each optionally prefixed with `- `/`* ` and leading indentation —
    into the ` AND `-joined form `split_artifact_args` already knows how to
    split, so a newline-separated grab and an ` AND `-joined grab dispose
    through the identical downstream path.

    CONTRACT REVERSAL (`_baton_unification_routing_enabled` is now True).
    The rule this function was written under — "N independent dispositions,
    one decision object per artifact" — described the pre-unification
    world. One decision object per artifact still holds, and this function
    is unchanged by the flip: the splitting is what makes the paths
    separable at all. What no longer holds is the INDEPENDENCE of the
    dispositions downstream. A multi-artifact grab whose members are
    inheritable batons now converges: each is claimed, and the held set
    unifies into ONE successor carrying them as fan-in legs, rather than N
    batons standing separately. Do not restore the old sentence from a
    reading of this function alone — the reversal lives one layer down, in
    the routing, not in the parsing.

    Only fires when the input contains a REAL newline AND at least one
    resulting line is bullet-prefixed. An unmarked multi-line paste (no
    bullets at all) is left byte-identical — that shape is the existing
    hard-line-wrap-inside-ONE-path signal `_sanitize_artifact_path_str`
    already tolerates as a single mid-token wrap, and reinterpreting every
    newline here as an artifact boundary would break that fallback and
    silently fragment a single long path instead.
    """
    if "\n" not in artifact_arg and "\r" not in artifact_arg:
        return artifact_arg
    raw_lines = re.split(r"\r\n|\r|\n", artifact_arg)
    if not any(_BULLET_PREFIX_RE.match(line) for line in raw_lines):
        return artifact_arg
    paths = [
        stripped
        for line in raw_lines
        if (stripped := _BULLET_PREFIX_RE.sub("", line, count=1).strip())
    ]
    if not paths:
        return artifact_arg
    return " AND ".join(paths)


def _expand_braces(artifact_arg: str) -> list[str]:
    """Expand ONE `PREFIX{a,b,c}SUFFIX` brace group into N literal paths.

    Alternatives are comma-split at the group's own nesting depth (a nested
    `{...}` inside an alternative doesn't fragment the split) and each
    alternative is whitespace/newline-stripped, so a shell-style line-wrapped
    brace list (`{a,\n  b}`) resolves the same as `{a,b}`. A string with no
    `{`, or an unbalanced `{` with no matching `}` at the same depth, passes
    through UNCHANGED as `[artifact_arg]` — this is the no-behavior-change
    guarantee for every pre-existing single-path caller.

    A second (or further) brace group in the same string is expanded by
    recursing on each already-substituted alternative; each recursive call
    operates on a strictly shorter string than its parent (the matched group
    is replaced by one alternative), so recursion is depth-bounded by the
    number of brace groups in the input and cannot loop.
    """
    start = artifact_arg.find("{")
    if start == -1:
        return [artifact_arg]
    depth = 0
    end = -1
    for i in range(start, len(artifact_arg)):
        ch = artifact_arg[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        return [artifact_arg]

    prefix = artifact_arg[:start]
    suffix = artifact_arg[end + 1 :]
    inner = artifact_arg[start + 1 : end]

    alternatives: list[str] = []
    nested_depth = 0
    current: list[str] = []
    for ch in inner:
        if ch == "{":
            nested_depth += 1
            current.append(ch)
        elif ch == "}":
            nested_depth -= 1
            current.append(ch)
        elif ch == "," and nested_depth == 0:
            alternatives.append("".join(current))
            current = []
        else:
            current.append(ch)
    alternatives.append("".join(current))

    if len(alternatives) < 2:
        return [artifact_arg]

    expanded: list[str] = []
    for alt in alternatives:
        combined = prefix + alt.strip() + suffix
        expanded.extend(_expand_braces(combined))
    return expanded


def split_artifact_args(artifact_arg: str) -> list[str]:
    """Split a `brief` argument on the standalone ` AND ` token into N paths,
    then brace-expand (`PREFIX{a,b,c}SUFFIX`) each resulting path.

    A single path with no standalone ` AND ` and no `{...}` group returns
    `[path]` unchanged (so the single-artifact path is byte-identical to
    today). Empty/whitespace-only segments are dropped; a fully-empty result
    degrades to `[artifact_arg]` so the caller still gets a resolvable-or-
    failing single entry rather than an empty batch. Brace expansion (see
    `_expand_braces`) feeds this SAME per-path list — a multi-artifact
    argument built from `AND`, from `{...}`, or from both, disposes through
    the identical N-independent-`brief()`-calls path in `brief_multi`.

    A trailing ` -- <prose>` EM-facing aside is stripped first
    (`_strip_aside`) so it is never swallowed as (part of) a path. A
    newline-separated bullet list is then reassembled into the ` AND `-joined
    form (`_reassemble_bullet_lines`) before the split below runs, so
    bullets, `AND`, and brace groups all compose freely.
    """
    working = _reassemble_bullet_lines(_strip_aside(artifact_arg))
    parts = [p.strip() for p in _ARTIFACT_JOIN_RE.split(working)]
    paths = [p for p in parts if p]
    if not paths:
        paths = [artifact_arg]
    expanded: list[str] = []
    for path in paths:
        expanded.extend(_expand_braces(path))
    return expanded


def brief_multi(
    artifact_arg: str,
    decisions: Optional[dict[str, Any]] = None,
    repo_root: Optional[Path] = None,
) -> list["BriefResult"]:
    """Resolve a (possibly multi-artifact) `brief` argument into one
    `BriefResult` per artifact.

    Splits `artifact_arg` on the standalone ` AND ` token and calls `brief`
    independently for each resulting path (each carries its own archive-fallback
    and re-anchoring resolution). The repo root is resolved once and threaded to
    every call. A single-path argument yields a one-element list whose sole
    entry is identical to `brief(artifact_arg, …)`.

    CLAIM-AT-BRIEF IS SINGLE-ARTIFACT ONLY. A one-path argument is a pickup
    attempt and takes the `brief`-stage lock (`acquire_brief_claim`); an
    ` AND `-joined or brace-expanded argument is a SURVEY across several
    batons — locking all of them would strand every one the EM did not go on
    to pick up, which is a worse failure than the contention this exists to
    stop. Splitting a survey into N separate single-path invocations does
    claim each one, and that is correct: each of those IS a pickup attempt.
    """
    root = repo_root or resolve_repo_root()
    if root is None:
        raise _TransportFailure("could not resolve a git worktree root")
    paths = split_artifact_args(artifact_arg)
    claim_at_brief = len(paths) == 1
    return [brief(path, decisions, root, claim_at_brief=claim_at_brief) for path in paths]


class _TransportFailure(Exception):
    """Reserved for the trampoline's own resolution failure (exit 3) — this
    module raises it only for a repo-root resolution failure that occurs
    inside `brief` itself (e.g. invoked outside any git worktree)."""


# ---------------------------------------------------------------------------
# CLI entrypoint (mirrors archive-stamp-cli's argv-parse shape)
# ---------------------------------------------------------------------------

#: The optional content keys a `--decisions[jp_id]` entry may carry alongside
#: `disposition` — the memo action-memo directive's content channel
#: (`_build_action_memo_args` below, consumed by
#: `pickup_assemble/apply.py::_read_session_dispositions`). Mirrors
#: `cs_action_memo`'s own `--decision-note`/`--realized-by`/`--actioned-note`/
#: `--distill-fate`/`--in-repo-capture` flags (`archive_stamp.py::
#: _DISPOSITION_FLAGS`) in snake_case, minus `--decision` itself (that one is
#: never EM-supplied directly — it is DERIVED from `disposition` via
#: `_MEMO_ACTION_DECISION_MAP`). This is the ONE place `--decisions` payload
#: fields are named — `validate_decisions_shape` and `_build_action_memo_args`
#: both key off it, so a new content field is added here once, not in each
#: consumer separately.
#:
#: Defect fix (2026-07-25, live repro this session): `in_repo_capture` was
#: added to `_DISPOSITION_FLAGS` (`cs_action_memo`'s own accepted flag set)
#: without a matching entry here, so `validate_decisions_shape` rejected any
#: `--decisions` payload that supplied it — the `distill_fate: ratification`
#: `distill_fate` value is UNREACHABLE without `in_repo_capture`
#: (`ops/memo_transition.py`'s cross-field validation hard-requires it), so
#: ratification was a dead end through this CLI despite `cs_action_memo`
#: fully supporting it end to end. This is exactly the silent-drop failure
#: the comment above warned against — an accepted-but-unwired key — except
#: inverted: `in_repo_capture` was unwired-but-unaccepted. Wiring it here
#: (validation) AND in `_build_action_memo_args` (forwarding) in the same
#: change is what closes the gap; adding it to only one side would repeat the
#: original mistake in the opposite direction.
DISPOSITION_CONTENT_KEYS = (
    "decision_note",
    "realized_by",
    "actioned_note",
    "distill_fate",
    "in_repo_capture",
)


def validate_decisions_shape(decisions: Any) -> Optional[str]:
    """Validates a parsed `--decisions` JSON value against the required
    judgment-point-id -> `{"disposition": <str>, ...}` map shape.

    Well-formed JSON with the wrong VALUE shape (`{"j1": "proceed"}` instead
    of `{"j1": {"disposition": "proceed"}}`) used to be silently ignored: the
    judgment point stayed unresolved with no error, which reads as a gating
    outcome rather than a usage error. This closes that gap by making a
    wrong-shaped payload fail loud, mirroring the existing malformed-JSON
    usage-error path exactly (same exit code, same stderr channel) rather
    than inventing a new error convention.

    `disposition` is required on every entry — OR its exact equivalent
    `value` (normalized to `disposition` in place, so every downstream
    reader keeps seeing one shape): `brief`'s own OUTPUT vocabulary names
    the choice-key `value` (`dispositions=[{"value": "proceed", ...}]`,
    mirrored by `baton_assemble._build_judgment_points`; this engine's own
    `pickup_assemble/__init__.py:2401` reads `d.get("value")` internally,
    confirming `value` is the engine's own word), and an operator round-
    tripping that output straight back into `--decisions` was rejected for
    using the engine's own vocabulary. If BOTH `disposition` and `value`
    are present and disagree, that is genuinely ambiguous and fails loud
    naming both values. Alongside it, an entry may carry any of
    `DISPOSITION_CONTENT_KEYS` — the note/decision content a disposition
    like `fyi`/`ack-nil` requires before `d-action-memo` can fire
    (`cs_action_memo` fails loud downstream if the required note is absent;
    this validator only shapes the channel, it does not enforce which notes
    a given disposition needs). Any other key is rejected — this defect fix
    (2026-07-25) intentionally does NOT open the schema to arbitrary keys,
    only to the closed set `cs_action_memo` itself understands (`value` is
    now part of that closed set, as `disposition`'s alias).

    Returns `None` when `decisions` is a valid map (including the empty
    map). Returns a one-line, actionable error string naming the first
    offending id, the shape it received, and the expected shape otherwise.

    Negative-spec: does NOT coerce a bare string into
    `{"disposition": <string>}`. Coercion would silently make two distinct
    payloads mean the same thing and paper over an operator's mistake — the
    ruling here is fail-loud, not be-liberal. A dict carrying NEITHER
    `disposition` nor `value` still fails loud, same as before.
    """
    if not isinstance(decisions, dict):
        return (
            f"--decisions must be a JSON object mapping judgment-point id to "
            f'{{"disposition": <value>}}, got {type(decisions).__name__}'
        )
    allowed_keys = {"disposition", "value", *DISPOSITION_CONTENT_KEYS}
    expected_form = (
        '{"disposition": "<value>"'
        + "".join(f', "{key}": "<value>"' for key in DISPOSITION_CONTENT_KEYS)
        + " (all but disposition optional)}"
    )
    for jp_id, value in decisions.items():
        if not isinstance(value, dict):
            return (
                f"--decisions[{jp_id!r}] must be shaped "
                f'{{"disposition": <value>}}, got {value!r} — expected form: '
                f'{{"{jp_id}": {expected_form}}}'
            )
        has_disposition = "disposition" in value
        has_value = "value" in value
        if not has_disposition and not has_value:
            return (
                f"--decisions[{jp_id!r}] must be shaped "
                f'{{"disposition": <value>}}, got {value!r} — expected form: '
                f'{{"{jp_id}": {expected_form}}}'
            )
        if has_disposition and has_value and value["disposition"] != value["value"]:
            return (
                f"--decisions[{jp_id!r}] carries both 'disposition' "
                f"({value['disposition']!r}) and 'value' ({value['value']!r}) "
                f"and they disagree — supply only one"
            )
        unknown_keys = set(value) - allowed_keys
        if unknown_keys:
            return (
                f"--decisions[{jp_id!r}] has unrecognized key(s) "
                f"{sorted(unknown_keys)!r} — accepted keys are "
                f"{sorted(allowed_keys)!r}"
            )
        if not has_disposition:
            value["disposition"] = value.pop("value")
        elif has_value:
            del value["value"]
        # Defect fix (2026-07-25): `ops/memo_transition.py` hard-requires
        # `in_repo_capture` whenever `distill_fate == "ratification"`
        # (cross-field validation on the downstream write), but that op only
        # runs after `d1`/`claim-memo-stamp` have already landed — a payload
        # missing `in_repo_capture` reached that error only after a partial
        # apply, mid-baton. Catching it HERE, at shape-validation time
        # (before any directive fires), turns a partial-mutation discovery
        # into a clean pre-flight rejection — same rationale as the
        # wrong-shape and unrecognized-key checks above.
        if value.get("distill_fate") == "ratification" and not value.get("in_repo_capture"):
            return (
                f"--decisions[{jp_id!r}] sets distill_fate=\"ratification\" but "
                f"omits \"in_repo_capture\" — supply the in-repo capture path "
                f'(e.g. "docs/decisions/..." or "docs/plans/...") the '
                f"ratification was captured to; cs_action_memo hard-requires it "
                f"for this distill_fate"
            )
    return None


def _usage(prog: str, stream=None) -> int:
    stream = sys.stderr if stream is None else stream
    print(
        f"usage: {prog} brief <artifact-path> [--decisions <json> | --decisions-file <path>]",
        file=stream,
    )
    print(
        f"       {prog} apply <artifact-path> [--session-id <id>] "
        "[--decisions <json> | --decisions-file <path>]",
        file=stream,
    )
    print(f"       {prog} drop <artifact-path> [--session-id <id>]", file=stream)
    print(f"       {prog} stamp-check <plan-path>", file=stream)
    print(
        '       --decisions is a JSON object: {"<jp-id>": {"disposition": "<value>", ...}}',
        file=stream,
    )
    print(
        '       ("value" is accepted as an exact equivalent of "disposition" -- brief\'s own',
        file=stream,
    )
    print(
        "        output uses that key). Legal <value>s for a given jp-id are that judgment",
        file=stream,
    )
    print(
        "        point's own dispositions[].value entries from this run's `brief` output.",
        file=stream,
    )
    return EXIT_USAGE


def main(argv: list[str]) -> int:
    if not argv:
        return _usage("pickup-assemble")

    if argv[0] in ("--help", "-h"):
        _usage("pickup-assemble", stream=sys.stdout)
        return EXIT_OK

    subcmd, rest = argv[0], argv[1:]

    if subcmd == "apply":
        from coordinator_core.pickup_assemble.apply import main_apply

        return main_apply(rest)

    if subcmd == "drop":
        from coordinator_core.pickup_assemble.apply import main_drop

        return main_drop(rest)

    if subcmd == "stamp-check":
        from coordinator_core.pickup_assemble.stamp_check import main_stamp_check

        return main_stamp_check(rest)

    if subcmd != "brief":
        print(f"pickup-assemble: unknown subcommand {subcmd!r}", file=sys.stderr)
        return _usage("pickup-assemble")

    if not rest:
        return _usage("pickup-assemble")

    artifact_path = rest[0]
    tail = rest[1:]
    decisions: dict[str, Any] = {}
    conflict = detect_conflicting_payload_channels(tail)
    if conflict is not None:
        print(f"pickup-assemble: {conflict}", file=sys.stderr)
        return EXIT_USAGE
    i = 0
    while i < len(tail):
        tok = tail[i]
        if (payload := resolve_json_payload_flag(tail, i)).consumed:
            if payload.error is not None:
                print(f"pickup-assemble: {payload.error}", file=sys.stderr)
                return EXIT_USAGE
            decisions = payload.value
            shape_error = validate_decisions_shape(decisions)
            if shape_error is not None:
                print(f"pickup-assemble: {shape_error}", file=sys.stderr)
                return EXIT_USAGE
            i += payload.consumed
        else:
            print(f"pickup-assemble: unrecognized argument {tok!r}", file=sys.stderr)
            return EXIT_USAGE

    try:
        results = brief_multi(artifact_path, decisions)
    except _TransportFailure as exc:
        print(f"pickup-assemble: transport failure: {exc}", file=sys.stderr)
        failure = _emit(
            {
                "error": str(exc),
                "transport_failure": True,
                "narration": f"Could not compute a brief: {exc}.",
                "next_move": "Confirm the command is run from inside a git worktree, then retry.",
            },
            EXIT_TRANSPORT_FAIL,
        )
        print(json.dumps(failure.decision_object))
        return failure.exit_code
    except Exception as exc:  # noqa: BLE001 - Finding 4b backstop
        # `_run_git`'s (OSError, subprocess.SubprocessError) guard (Finding
        # 4a) covers every enumerated git-shellout raise site, but the
        # contract's "a decision object is emitted on every exit, including
        # non-zero — never a bare exit code with no object" guarantee must
        # not rest on having enumerated every raise site correctly. This is
        # the structural backstop: ANY unexpected exception from `brief()`
        # still yields EXIT_TRANSPORT_FAIL plus a JSON decision-object-shaped
        # payload, matching the `_TransportFailure` branch's shape above.
        print(f"pickup-assemble: unexpected failure: {exc}", file=sys.stderr)
        failure = _emit(
            {
                "error": str(exc),
                "transport_failure": True,
                "narration": f"brief() raised an unexpected exception: {exc}.",
                "next_move": (
                    "Re-run against the same artifact path; if this repeats, report the "
                    "traceback — this is a structural backstop firing, not an enumerated "
                    "failure mode."
                ),
            },
            EXIT_TRANSPORT_FAIL,
        )
        print(json.dumps(failure.decision_object))
        return failure.exit_code

    # Single-artifact invocation prints ONE decision object (byte-identical to
    # pre-multi behavior); a multi-artifact ` AND `-joined argument prints a
    # JSON ARRAY of decision objects (one per artifact, in argument order), and
    # the process exit code is the worst per-artifact code (OK < business <
    # usage < transport, i.e. numeric max).
    #
    # This bare-object/bare-array shape is a HARD cross-repo consumer contract:
    # DoE-claude's `coordinator/hooks/scripts/pickup-autofire.py`
    # `decode_decision_payload()` parses this exact stdout shape — a bare
    # object becomes `[obj]`, a bare array's dict elements are kept as-is,
    # and anything else (including a `{"briefs": [...]}`-style wrapper) is a
    # fail-open `[]` with no error surfaced. Do NOT introduce a wrapper
    # envelope around this payload without first sending a memo to
    # doe-claude-em — it would silently break autofire with no exception to
    # catch.
    if len(results) == 1:
        payload: Any = results[0].decision_object
        exit_code = results[0].exit_code
    else:
        payload = [r.decision_object for r in results]
        exit_code = max(r.exit_code for r in results)

    # Review: code-reviewer — Finding 2: the F4 "a decision object is emitted
    # on every exit" guarantee previously only covered exceptions raised
    # during brief() itself — a json.dumps serialization failure on the
    # *result* escaped main() uncaught. Wrap it too, degrading to the same
    # error-object shape as the _TransportFailure/backstop branches above.
    try:
        print(json.dumps(payload, indent=2, sort_keys=True))
    except (TypeError, ValueError) as exc:
        print(f"pickup-assemble: could not serialize decision object: {exc}", file=sys.stderr)
        failure = _emit(
            {
                "error": str(exc),
                "transport_failure": True,
                "narration": "The computed decision object could not be serialized to JSON.",
                "next_move": (
                    "Report this — a non-serializable decision object is a defect in the "
                    "assembler, not a caller error."
                ),
            },
            EXIT_TRANSPORT_FAIL,
        )
        print(json.dumps(failure.decision_object))
        return failure.exit_code
    return exit_code
