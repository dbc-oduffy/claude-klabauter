"""
coordinator_core.ops.fleet.memo_heal — memo.heal_inbox MUTATING op handler.

Purpose: a repo run AGAINST ITSELF (repo_root = the receiver) that makes a
delivered cross-repo memo durable against the receiver's OWN branch
gestures — delete, recreate-from-main, rename, hard-reset, `git gc` — by
reading the receiver's `refs/coordinator/inbox/*` anchors
(`_memo_anchor.py`, C3) and reconciling them against the working tree:

  - RESTORE an anchored memo the working tree has lost, when the anchor's
    delivery commit is itself gone or unreachable from any branch (LOST).
  - RETIRE an anchor for a memo the receiver deliberately archived or
    deleted on a branch it kept (removing the anchor is the right outcome,
    not a restore).
  - ADOPT a present, tracked, unanchored inbox memo (a delivery that landed
    before this op existed, or on a receiver `memo.send` never anchored) so
    future heals can reason about it.
  - RE-KEY a present, anchored memo whose delivery commit went away (a
    prior restore's own commit, most often) onto the commit that is
    actually carrying it now, so a later heal does not read a live memo as
    lost forever.

This module lives separately from `memo_send.py` on purpose: it is
RECEIVER-side (acts against `repo_root` itself), where `memo_send` is
SENDER-side (acts against a registry-resolved PEER's repo). Nothing here
writes into another repo.

Spec backlink: docs/plans/2026-09-11-memo-deliveries-survive-the-receiver-s-o.md § C5
Review citations applied per the spec row: eng-director F1/F3/F5/F7, apm A1/A4
(EM-adjudicated).

Negative-spec:
  - Does NOT accept any param besides `dry_run` — the op reads the anchor
    ref namespace and the calling repo's own working tree; there is nothing
    else for a caller to name.
  - Does NOT write into `archive/` — an anchor whose memo sits only in
    `archive/` is RETIRED (the ref is deleted), never restored back into
    `inbox/`; the receiver archived it on purpose.
  - Does NOT restore a memo the receiver deliberately deleted on a branch
    it KEPT (reachable from that branch's tip) — only a memo unreachable
    from every branch (truly orphaned) or whose commit object is gone
    outright counts as LOST.
  - Does NOT adopt an untracked or dirty (worktree bytes != HEAD's blob)
    inbox file — adoption asserts "this IS what HEAD says is here", never
    a guess.
  - Does NOT write a git object for an adopted memo — the anchor points at
    HEAD's OWN existing blob; nothing new is hashed or stored.
  - Does NOT retry a refused `update_refs_stdin` transaction — the refusal
    is reported; the NEXT invocation of this op converges, per this op's
    own idempotence, rather than this call re-deriving and looping.
    (Review: overengineering-reviewer — the retry-once + re-derive layer
    this replaced was redundant with that same convergence guarantee.)
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from coordinator_core.git.content_hash import (
    _autocrlf_checkin_normalize,
    _repo_autocrlf_true,
    _text_attribute_pinned,
)
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_objects import _read_object
from coordinator_core.git.git_state import head_sha as _git_state_head_sha
from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.ceremony.git_native import _head_entry_for
from coordinator_core.ops.fleet._common import (
    build_act_result,
    build_dry_run_result,
    build_setup_error_result,
    main_worktree_root,
)
from coordinator_core.ops.fleet._memo_anchor import (
    ANCHOR_REF_PREFIX,
    CORPUS_ROOT_RELDIRS,
    _valid_commit_sha,
    _valid_ref_component,
    anchor_names,
    present_filenames,
    resolve_anchor,
)

_MODE = "heal_inbox"

# Params this handler declares; anything else fails loud (mirrors memo.send's
# own discipline) — this op reads the anchor namespace and the calling
# repo's own tree, so there is nothing else a caller could meaningfully name.
_KNOWN_PARAM_KEYS = frozenset({"dry_run"})

#: Review: apm A4 (EM-adjudicated) — measured 2026-09-11, one `update-ref
#: --stdin` spawn does 150 creates in 206ms and 300 in 422ms (~1.35ms/ref);
#: 100 creates is ~140ms. At most this many UNANCHORED inbox memos are
#: adopted per run; the rest are picked up on later runs (a backlog of 289
#: converges in 3 runs). Re-keys and retires are NOT capped — they are rare
#: and bounded by losses/dispositions, not by inbox size.
#: Review: overengineering-reviewer — this cap and its branch exist for the
#: pre-A2 backlog only, which converges in 3 runs per repo; once the fleet's
#: repos have converged, mark this constant and the cap branch below for
#: deletion rather than leaving them resident on a number no live run
#: still tests.
ADOPT_CAP_PER_RUN = 100

#: Both corpus roots -- shared with every other present-set reader in this
#: feature via `_memo_anchor.CORPUS_ROOT_RELDIRS` (Review:
#: overengineering-reviewer F1). Kept as a local alias so existing
#: in-module references need no further churn.
_CORPUS_ROOT_RELDIRS = CORPUS_ROOT_RELDIRS

# Generator-provenance: this op writes only into the CALLING repo's own
# inbox/ (a restore's O_EXCL create) and its own refs/coordinator/inbox/*
# anchor namespace — never a peer's tree (contrast memo.send, which crosses
# into a receiver repo it does not own).
MUTATES = [
    "state/cross-repo/inbox/*.md",
    "cross-repo/inbox/*.md",
]


# ---------------------------------------------------------------------------
# Param validation
# ---------------------------------------------------------------------------


def _validate_params(params: dict):
    dry_run = params.get("dry_run")
    if not isinstance(dry_run, bool):
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.heal_inbox: dry_run must be bool, got " + repr(type(dry_run).__name__),
        )
    unknown_keys = set(params.keys()) - _KNOWN_PARAM_KEYS
    if unknown_keys:
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.heal_inbox: unrecognized param(s) {sorted(unknown_keys)} — "
            f"known params: {sorted(_KNOWN_PARAM_KEYS)}.",
        )
    return dry_run


# ---------------------------------------------------------------------------
# Present-set: the working tree's own filename -> (status, path) map,
# unioned across BOTH corpus roots, inbox always shadowing archive.
# ---------------------------------------------------------------------------


class _PresentEntry:
    __slots__ = ("status", "path", "relpath")

    def __init__(self, status: str, path: Path, relpath: str):
        self.status = status  # "inbox" | "archive"
        self.path = path
        self.relpath = relpath  # repo-relative, posix separators


def _present_map(worktree_root: Path) -> Dict[str, _PresentEntry]:
    """filename -> `_PresentEntry`, recursive union of `inbox/` and
    `archive/` under both corpus roots, keyed by filename, read from the
    WORKING TREE — the tree is the truth a reader sees, and a hard reset
    empties it along with the branch. `inbox/` entries always shadow an
    `archive/` hit for the same filename, matching step 3's "In inbox/:
    leave it alone" priority over "In archive/ and not in inbox/: RETIRE".

    The traversal itself is `_memo_anchor.present_filenames` (shared with
    `memo_send`'s own present-set reader, Review: overengineering-reviewer
    F1); this wraps it into the repo-relative-path shape this module's
    callers need."""
    found: Dict[str, _PresentEntry] = {}
    for fname, (status, p) in present_filenames(worktree_root).items():
        found[fname] = _PresentEntry(
            status, p, p.relative_to(worktree_root).as_posix()
        )
    return found


def _commit_object_present(common_dir: Path, sha: str) -> bool:
    """True iff `sha` names a commit object readable in-process from
    `common_dir` — object EXISTENCE, zero spawns (mirrors `memo_send.
    _delivery_commit_is_object`, applied here against the receiver's own
    store rather than a sender's read of a peer's)."""
    found = _read_object(common_dir, sha)
    return found is not None and found[0] == "commit"


def _blob_sha1(data: bytes) -> str:
    """Git's own blob object-id, computed in process without writing
    anything — `hashlib.sha1("blob " + len + NUL + data)`, byte-identical to
    what `git hash-object` (no filters) would report. ADOPT's whole point is
    verifying tracked-and-matching WITHOUT a write, so this must never call
    `write_object`."""
    header = b"blob " + str(len(data)).encode("ascii") + b"\x00"
    return hashlib.sha1(header + data).hexdigest()


# ---------------------------------------------------------------------------
# Restore-side plumbing (mirrors memo_send.py's own O_EXCL / rollback shape,
# duplicated in-module rather than imported: memo_send's helpers are private
# to that file and this op's failure register — "restored", not "sent" —
# is genuinely different text, not a copy that should share a symbol).
# ---------------------------------------------------------------------------


def _rollback_unwritten_file(target_file: Path) -> str:
    """Undo this call's own O_EXCL write when the commit that was supposed
    to follow it did not durably land. Safe here for the identical reason
    `memo_send._rollback_unwritten_file` states: the O_EXCL open that
    created `target_file` proves it did not exist before this call, and a
    declined commit proves nothing references it since."""
    try:
        target_file.unlink()
    except OSError as unlink_exc:
        return (
            f" WARNING: the file this call wrote could NOT be removed"
            f" ({unlink_exc}); it is left uncommitted and a retry will refuse"
            f" on the no-clobber guard until it is cleared."
        )
    return " the file this call wrote was removed (tree restored)."


def _write_msg_file(text: str) -> Path:
    fd, name = tempfile.mkstemp(prefix="memo-heal-msg-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    except BaseException:
        os.unlink(name)
        raise
    return Path(name)


def _restore_commit_message(filename: str) -> str:
    return f"cross-repo memo restored: {filename}\n\nRestored by memo.heal_inbox.\n"


def _inbox_write_target(worktree_root: Path, filename: str) -> Tuple[Path, str]:
    """The restore WRITE target — always this repo's canonical inbox root
    per `memo_corpus.memo_corpus_root` (C10a-migrated when it exists,
    legacy otherwise; never mints a fresh legacy inbox). Returns (absolute
    path, repo-relative posix path)."""
    from coordinator_core import memo_corpus

    inbox_dir = Path(memo_corpus.memo_corpus_root(str(worktree_root))) / "inbox"
    target = inbox_dir / filename
    relpath = target.relative_to(worktree_root).as_posix()
    return target, relpath


class _RestoreOutcome:
    __slots__ = ("ok", "restored_by_peer", "new_commit_sha", "reason")

    def __init__(self, ok: bool, restored_by_peer: bool, new_commit_sha: Optional[str], reason: Optional[str]):
        self.ok = ok
        self.restored_by_peer = restored_by_peer
        self.new_commit_sha = new_commit_sha
        self.reason = reason


def _restore_one(worktree_root: Path, filename: str, blob_sha: str, common_dir: Path) -> _RestoreOutcome:
    """Resolve `blob_sha`'s bytes and land them at `inbox/<filename>` via
    O_EXCL + `git_native.commit_authored_new_file` — one commit per memo,
    since this is the loss path and should be rare.

    `record_ledger=True`: unlike `memo_send.commit_authored_new_file`'s call
    (which commits into a PEER's repo and must never touch that repo's
    commit ledger), this commit lands in THIS repo's own tree — the "caller
    committing into its own repo should pass True" case
    `commit_authored_new_file`'s own docstring names.
    """
    data = resolve_anchor(common_dir, blob_sha)
    if data is None:
        return _RestoreOutcome(False, False, None, f"anchor blob {blob_sha!r} is unresolvable — cannot restore {filename!r}")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return _RestoreOutcome(False, False, None, f"anchor blob for {filename!r} is not valid UTF-8 text: {exc}")

    target_file, rel_path = _inbox_write_target(worktree_root, filename)
    target_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(target_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
    except FileExistsError:
        # Review: eng-director F7 — a peer already restored this memo.
        # Neither a rollback (we created nothing) nor a failure (the memo
        # IS back) — the anchor's own re-key is picked up by a LATER heal's
        # "present + anchored + commit gone" rule, not lost.
        return _RestoreOutcome(True, True, None, None)
    except OSError as exc:
        return _RestoreOutcome(False, False, None, f"write-failed restoring {filename!r}: {exc}")

    msg_file = _write_msg_file(_restore_commit_message(filename))
    try:
        commit_result = git_native.commit_authored_new_file(
            rel_path, content, msg_file, worktree_root, record_ledger=True,
        )
    finally:
        try:
            msg_file.unlink()
        except OSError:
            pass

    if not commit_result.ok:
        rollback_detail = _rollback_unwritten_file(target_file)
        return _RestoreOutcome(
            False, False, None,
            f"restore commit declined for {filename!r}: {commit_result.stderr};{rollback_detail}",
        )
    return _RestoreOutcome(True, False, commit_result.stdout.strip(), None)


# ---------------------------------------------------------------------------
# Transaction-line building
# ---------------------------------------------------------------------------


def _anchor_ref(filename: str, commit_sha: str) -> str:
    return ANCHOR_REF_PREFIX + filename + "/" + commit_sha


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


@register_op("memo.heal_inbox")
def _memo_heal_inbox(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'memo.heal_inbox' MUTATING op handler.

    Run by a repo against ITSELF: restores absent anchored memos whose
    delivery is genuinely lost, retires anchors for memos the receiver
    archived or deleted on a kept branch, adopts unanchored present inbox
    memos (capped at `ADOPT_CAP_PER_RUN` per run), and re-keys an anchor
    whose delivery commit went away while its memo is still present. See
    module docstring for the full decision table.

    Params:
        dry_run (bool, required): preview (true) vs. act (false). Preview
            plans and reports; mutates nothing.

    repo_root: git common dir (`_OP_KEY_SCOPE = "common_dir"`) — THIS
    repo's own worktree is derived via `main_worktree_root(repo_root)`.
    """
    validated = _validate_params(params)
    if isinstance(validated, dict):
        return validated
    dry_run = validated

    if repo_root is None:
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.heal_inbox: no repo_root supplied — this op heals the "
            "CALLING repo's own inbox and requires a resolved worktree "
            "(common_dir-keyed op).",
        )
    worktree_root = main_worktree_root(Path(repo_root))
    common_dir = resolve_git_common_dir(worktree_root)

    present = _present_map(worktree_root)
    anchors = anchor_names(common_dir)
    head_sha = _git_state_head_sha(worktree_root)

    retire_items: List[dict] = []
    rekey_items: List[dict] = []  # {"filename","old_commit_sha","new_commit_sha","blob_sha"}
    restore_items: List[dict] = []
    leave_items: List[dict] = []
    failed_items: List[dict] = []
    restored_by_peer_items: List[dict] = []

    absent_commit_present: List[dict] = []
    anchored_filenames = set()

    for (fname, csha, bsha) in anchors:
        anchored_filenames.add(fname)
        pres = present.get(fname)

        if pres is not None and pres.status == "archive":
            retire_items.append({
                "filename": fname, "commit_sha": csha, "blob_sha": bsha,
                "reason": "archived-and-not-in-inbox",
            })
            continue

        if pres is not None and pres.status == "inbox":
            entry = _head_entry_for(worktree_root, pres.relpath)
            tracked_matching = entry is not None and entry[1] == bsha
            if tracked_matching and head_sha and not _commit_object_present(common_dir, csha):
                rekey_items.append({
                    "filename": fname, "old_commit_sha": csha,
                    "new_commit_sha": head_sha, "blob_sha": bsha,
                })
            else:
                leave_items.append({"filename": fname, "reason": "present-and-anchored"})
            continue

        # Absent from the present-set entirely.
        if not _commit_object_present(common_dir, csha):
            restore_items.append({"filename": fname, "old_commit_sha": csha, "blob_sha": bsha})
        else:
            absent_commit_present.append({"filename": fname, "commit_sha": csha, "blob_sha": bsha})

    # Two-stage batched reachability probe — only when non-empty, never
    # handed a missing object (both already true of `absent_commit_present`
    # by construction above).
    if absent_commit_present:
        stage1_shas = [it["commit_sha"] for it in absent_commit_present]
        stage1 = git_native.rev_list_not(common_dir, stage1_shas, ["--not", "HEAD"])
        if not stage1.ok:
            for it in absent_commit_present:
                failed_items.append({
                    "id": it["filename"],
                    "reason": f"reachability probe (HEAD) failed: {stage1.stderr}",
                })
        else:
            printed1 = {line.strip() for line in stage1.stdout.splitlines() if line.strip()}
            stage2_candidates = []
            for it in absent_commit_present:
                if it["commit_sha"] in printed1:
                    stage2_candidates.append(it)
                else:
                    # Not printed -> reachable from HEAD -> the receiver
                    # removed it on a branch it kept.
                    retire_items.append({
                        "filename": it["filename"], "commit_sha": it["commit_sha"],
                        "blob_sha": it["blob_sha"], "reason": "reachable-from-head",
                    })
            if stage2_candidates:
                stage2_shas = [it["commit_sha"] for it in stage2_candidates]
                stage2 = git_native.rev_list_not(
                    common_dir, stage2_shas, ["--not", "--branches", "--remotes"],
                )
                if not stage2.ok:
                    for it in stage2_candidates:
                        failed_items.append({
                            "id": it["filename"],
                            "reason": f"reachability probe (branches) failed: {stage2.stderr}",
                        })
                else:
                    printed2 = {line.strip() for line in stage2.stdout.splitlines() if line.strip()}
                    for it in stage2_candidates:
                        if it["commit_sha"] in printed2:
                            restore_items.append({
                                "filename": it["filename"], "old_commit_sha": it["commit_sha"],
                                "blob_sha": it["blob_sha"],
                            })
                        else:
                            leave_items.append({
                                "filename": it["filename"],
                                "reason": "reachable-from-another-branch",
                            })

    # ADOPT — unanchored, present-in-inbox filenames, tracked at HEAD with
    # matching bytes, capped per run, zero object writes.
    adopt_items: List[dict] = []
    adopt_skipped = 0
    # Loop-invariant: `core.autocrlf` is a repo-level config read (three
    # files per call), so resolving it per candidate would pay it once per
    # memo on a 100-adopt run for an answer that cannot change mid-run.
    autocrlf_true = _repo_autocrlf_true(worktree_root) if head_sha else False
    if head_sha:
        unanchored = sorted(
            fname for fname, entry in present.items()
            if entry.status == "inbox" and fname not in anchored_filenames
        )
        for fname in unanchored:
            pres = present[fname]
            if len(adopt_items) >= ADOPT_CAP_PER_RUN:
                break
            entry = _head_entry_for(worktree_root, pres.relpath)
            if entry is None:
                adopt_skipped += 1
                continue
            _mode, head_blob_sha = entry
            try:
                data = pres.path.read_bytes()
            except OSError:
                adopt_skipped += 1
                continue
            # Under `core.autocrlf=true` (this fleet's Windows default) git checks a
            # memo out as CRLF while HEAD's blob stays LF, so the raw bytes never
            # hash to the blob and every memo git itself wrote -- clone, checkout,
            # reset -- would read as dirty and never be adopted. Adopt points at
            # HEAD's existing blob and writes no bytes, so the only question here
            # is whether the CONTENT differs from what is committed; a line-ending
            # difference is not a content difference.
            #
            # Review: code-reviewer F2 -- the CRLF-collapsed candidate is only
            # tried when it is actually reachable via git's own checkin-side
            # normalization (`_repo_autocrlf_true` + no `.gitattributes` pin on
            # this path, `git.content_hash`'s canonical helpers, the single
            # implementation `git_native.py` itself defers to). Skipping this
            # gate would accept content that is NOT what HEAD committed for a
            # path where CRLF bytes are literal content rather than a
            # line-ending artifact (e.g. `-text`/`binary` pinned, or
            # autocrlf off).
            candidates = {_blob_sha1(data)}
            if autocrlf_true and _text_attribute_pinned(worktree_root, pres.relpath) is None:
                candidates.add(_blob_sha1(_autocrlf_checkin_normalize(data)))
            if head_blob_sha not in candidates:
                adopt_skipped += 1
                continue
            # Review: code-reviewer F1 -- a raw on-disk inbox filename is
            # untrusted input to the ref namespace: `write_anchor` (the
            # `memo.send` path) refuses the same shape via these same two
            # validators before ever building a ref string, so ADOPT must
            # refuse here too rather than let one ref-illegal filename reach
            # `_anchor_ref`/`update_refs_stdin` and poison the whole batch
            # (git's `update-ref --stdin` transaction is all-or-nothing, so
            # one bad line fails every legitimate retire/rekey/adopt in the
            # same run, and the file stays unanchored to re-poison the next
            # run too).
            if not _valid_ref_component(fname) or not _valid_commit_sha(head_sha):
                failed_items.append({
                    "id": fname,
                    "reason": "adopt refused: filename is not a valid ref path component",
                })
                continue
            adopt_items.append({
                "filename": fname, "commit_sha": head_sha, "blob_sha": head_blob_sha,
            })
    # else: no HEAD commit -> no delivery could have landed and no inbox
    # memo could be tracked, so there is no repository state reachable here
    # to count as skipped (Review: overengineering-reviewer). adopt_skipped
    # stays 0.

    if dry_run:
        candidates = (
            [{"id": it["filename"], "action": "retire", **it} for it in retire_items]
            + [{"id": it["filename"], "action": "restore", **it} for it in restore_items]
            + [{"id": it["filename"], "action": "rekey", **it} for it in rekey_items]
            + [{"id": it["filename"], "action": "adopt", **it} for it in adopt_items]
        )
        return build_dry_run_result(_MODE, candidates)

    # ── act path ────────────────────────────────────────────────────────
    restored_filenames: List[str] = []
    for item in restore_items:
        outcome = _restore_one(worktree_root, item["filename"], item["blob_sha"], common_dir)
        if outcome.restored_by_peer:
            restored_by_peer_items.append({"id": item["filename"], "action": "restored-by-peer"})
            continue
        if not outcome.ok:
            failed_items.append({"id": item["filename"], "reason": outcome.reason})
            continue
        restored_filenames.append(item["filename"])
        rekey_items.append({
            "filename": item["filename"], "old_commit_sha": item["old_commit_sha"],
            "new_commit_sha": outcome.new_commit_sha, "blob_sha": item["blob_sha"],
        })

    planned: List[dict] = []
    for it in retire_items:
        planned.append({"op": "delete", "filename": it["filename"], "commit_sha": it["commit_sha"], "blob_sha": it["blob_sha"]})
    for it in rekey_items:
        planned.append({"op": "delete", "filename": it["filename"], "commit_sha": it["old_commit_sha"], "blob_sha": it["blob_sha"]})
        planned.append({"op": "create", "filename": it["filename"], "commit_sha": it["new_commit_sha"], "blob_sha": it["blob_sha"]})
    for it in adopt_items:
        planned.append({"op": "create", "filename": it["filename"], "commit_sha": it["commit_sha"], "blob_sha": it["blob_sha"]})

    acted: List[dict] = []
    if planned:
        commands = [
            (p["op"], _anchor_ref(p["filename"], p["commit_sha"]), p["blob_sha"])
            for p in planned
        ]
        result = git_native.update_refs_stdin(worktree_root, commands)

        if result.ok:
            for it in retire_items:
                acted.append({"id": it["filename"], "action": "retired"})
            for it in rekey_items:
                acted.append({"id": it["filename"], "action": "rekeyed"})
            for it in adopt_items:
                acted.append({"id": it["filename"], "action": "adopted"})
        else:
            # Reported, not retried — the NEXT invocation converges (module
            # negative-spec), which is what made the prior retry-once +
            # re-derive layer redundant (Review: overengineering-reviewer).
            for it in retire_items:
                failed_items.append({"id": it["filename"], "reason": f"retire transaction refused: {result.stderr}"})
            for it in rekey_items:
                failed_items.append({"id": it["filename"], "reason": f"rekey transaction refused: {result.stderr}"})
            for it in adopt_items:
                failed_items.append({"id": it["filename"], "reason": f"adopt transaction refused: {result.stderr}"})

    # A restore's file-write-and-commit is reported independent of whether
    # its follow-up anchor re-key transaction landed — per this module's
    # own restore docstring, a refused transaction after a landed restore
    # is a SAFE state (the memo is present; a later heal's present+anchor-
    # gone rule carries the re-key forward), never a reason to withhold the
    # "restored" credit for a write and commit that genuinely succeeded.
    for fname in restored_filenames:
        acted.append({"id": fname, "action": "restored"})
    acted.extend(restored_by_peer_items)
    if adopt_skipped:
        acted.append({"id": "*", "action": "adopt-skipped-count", "count": adopt_skipped})

    return build_act_result(_MODE, acted, [], failed_items)
