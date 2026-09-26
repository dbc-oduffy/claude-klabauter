"""
coordinator_core.ops.fleet.memo_reconcile_outbox — memo.reconcile_outbox MUTATING UDS op.

Purpose: the outbox depth IS the interface — the workday-start surface, the
pickup skill, and a handoff's "N undelivered drafts" line all read
`.coordinator-local/memo-outbox/*.md` (plus, dual-root, the retired
`state/memo-outbox/*.md`) as a work queue. `memo.send` moves its own draft to
`sent/` when it succeeds, so the depth is honest for everything that went out
that way. Nothing moves an entry that reached the receiver by ANY OTHER route
— an out-of-band hand-delivery, a hand-written inbox file, a send whose
receipt leg failed — and each of those leaves a stale local copy sitting in
the outbox looking exactly like a live draft. A triage of one such outbox
found 25 of 30 entries were already delivered: a reader who trusted the count
scheduled roughly six times the work that existed.

This op reconciles that: every entry whose `status:` is not `draft` AND that
already has a local `sent/<name>` receipt is MOVED to `sent/`, never deleted,
so the paper trail survives and an operator can still audit what went where.

It also reports the opposite asymmetry — a DELIVERY THAT HAS NO SENDER
RECEIPT. `memo.send` writes its `sent/<topic>.md` copy only after reading the
receiver's own object store back and confirming the delivery commit is really
there, and only then attempts the sender-side commit. So a `sent/` copy that
HEAD does not know is a memo the receiver demonstrably has and this repo has
no committed record of: the ledger append, the index lock, or the commit
itself failed after the point of no return, and `memo.send` said so on an
envelope that is gone by the next session. Symmetrically, a `status: draft`
entry whose `sent/<name>` already exists is the stale duplicate the send's
unlink-degrades-to-a-warning path leaves behind — delivered, archived, and
still counted as work by every reader of the outbox depth.

Neither is moved. The first is not an outbox entry at all, and the second
cannot be archived without clobbering the authoritative copy. They are
REPORTED, with the pathspec that completes the receipt, because what to do
about a half-landed delivery is an operator's judgement and a sweep that
guesses at it writes into history.

Delivery truth is ultimately the RECEIVER's own inbox/archive, not the local
`status:` field, and a `status:` claim is not itself evidence: `_classify`
keys "delivered" on whether a `sent/<name>` receipt already exists locally,
never on `status != draft` alone. A `status: sent`/`open` entry with no
`sent/<name>` copy has no local evidence it ever left this repo by the
route this op can see, so it is left in place — on the staleness-nudge
surface, where an undelivered ask belongs — rather than moved on the
strength of a field that can lie. Reaching into peer trees to confirm
delivery is a different, heavier op with a different blast radius; this op
does not do that either, so an entry with a receipt is trusted (the receipt
is downstream of `memo.send`'s own receiver-side verification) and an entry
without one is left alone rather than guessed at.

Negative-spec:
  - Does NOT delete anything, ever. Every action is a move into `sent/`.
  - Does NOT clobber: an entry whose `sent/<name>` already exists is skipped
    with a note, never overwritten — the archived copy is authoritative.
  - Does NOT touch `status: draft` entries. A draft's home IS the outbox;
    moving one would be the very over-count this op exists to fix, inverted.
  - Does NOT move a non-draft entry that has no local `sent/<name>` receipt.
    A `status:` field claiming delivery is not itself a receipt; without one
    the entry stays in the outbox, on the staleness-nudge surface, until a
    receipt lands or an operator disposes of it by hand.
  - Does NOT act on frontmatter-less files. Those are orphaned `--body-file`
    fragments from the one-shot flag form DR-210 retired; they were never
    memos and `sent/` is not their home. They are REPORTED so an operator can
    dispose of them, which is a per-file judgement, not a sweep.
  - Does NOT reach into the receiver's tree to confirm a delivery. Every
    check here reads this repo's own disk and its own HEAD. The uncommitted-
    receipt report is exact WITHOUT a peer read precisely because the sent/
    copy is written downstream of memo.send's own receiver-side verification.
  - Does NOT report an uncommitted receipt when HEAD is unreadable (a fresh
    clone, a detached or corrupt ref), nor when HEAD knows NO archived memo
    at all — `.coordinator-local/` is gitignored, so a repo that never
    tracked the bucket would otherwise report every memo it ever sent. "Not
    in HEAD", "no HEAD to ask", and "this repo does not track its archive"
    are three different answers and only the first is a finding.
  - Does NOT commit. `memo.send` commits its own receipt because it is
    completing one delivery it just performed; this op moves an operator-
    chosen batch in a corpus other sessions read live, so it returns the
    moved paths and leaves the commit — and its pathspec — to the caller.

Registered as "memo.reconcile_outbox" via @register_op; MUTATING
classification, `common_dir`-scoped (it reconciles the CALLING repo's own
outbox, like memo.draft/memo.compose/memo.send).

Spec backlink: state/bug-backlog/2026-08-25-the-memo-outbox-does-not-clean-itself-up-after-a-send.yaml
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from coordinator_core.session.machinery_paths import (
    LEGACY_MEMO_OUTBOX_RELDIR,
    MEMO_OUTBOX_RELDIR,
)
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.git.git_state import read_tree_spine
from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import (
    build_act_result,
    build_dry_run_result,
    build_setup_error_result,
    main_worktree_root,
)
from coordinator_core.ops.fleet.memo_draft import merged_outbox_drafts
from coordinator_core.session import machinery_paths as _machinery_paths

_LOG = logging.getLogger(__name__)

_MODE = "reconcile_outbox"

_LIVE_STATUS = "draft"

_KNOWN_PARAM_KEYS = frozenset({"dry_run"})

MUTATES = [
    f"{MEMO_OUTBOX_RELDIR}/*.md",
    f"{LEGACY_MEMO_OUTBOX_RELDIR}/*.md",
    f"{MEMO_OUTBOX_RELDIR}/sent/*.md",
]


def _validate_params(params: dict):
    dry_run = params.get("dry_run")
    if not isinstance(dry_run, bool):
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.reconcile_outbox: dry_run must be bool, got "
            + repr(type(dry_run).__name__),
        )
    unknown_keys = set(params.keys()) - _KNOWN_PARAM_KEYS
    if unknown_keys:
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.reconcile_outbox: unrecognized param(s) {sorted(unknown_keys)} — "
            f"known params: {sorted(_KNOWN_PARAM_KEYS)}. This op reconciles the "
            f"whole outbox; it takes no per-entry selector.",
        )
    return dry_run


def _classify(path: Path, archived: bool = False) -> tuple[str, Optional[str], Optional[str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return "keep", None, f"unreadable, left in place: {exc}"

    split = split_frontmatter(text)
    if split is None:
        return (
            "report",
            None,
            "no frontmatter — an orphaned --body-file fragment, not a memo; "
            "sent/ is not its home, dispose of it per-file",
        )

    status = read_fm_field(split.fm_text, "status")
    if status is None:
        return "keep", None, "frontmatter present but no status: key — left in place"
    if status == _LIVE_STATUS:
        if archived:
            return (
                "report",
                status,
                f"still reads status: draft, but sent/{path.name} already "
                f"exists — this memo was delivered and archived and the send "
                f"could not remove its own original. The archived copy is "
                f"authoritative; delete this duplicate by hand rather than "
                f"moving it onto the stamped one",
            )
        return "keep", status, None
    # `status != draft` is NOT itself a receipt — key "delivered" on the
    # already-computed receipt check (a local `sent/<name>` copy), not on
    # the status field, which can claim delivery a hand edit invented. No
    # receipt means no local evidence this ever left the repo; leave it in
    # the outbox, on the staleness-nudge surface, rather than moving it on
    # the strength of a field that can lie.
    if archived:
        return "move", status, None
    return (
        "keep",
        status,
        f"status: {status} but no sent/{path.name} receipt exists locally — "
        f"no evidence of delivery by any route this op can see; left on the "
        f"staleness-nudge surface rather than moved on the status field alone",
    )


def _relpath(path: Path, worktree_root: Path) -> str:
    return os.path.relpath(str(path), str(worktree_root)).replace(os.sep, "/")


def _uncommitted_receipts(worktree_root: Path, sent_dir: Path) -> list:
    try:
        names = sorted(p.name for p in sent_dir.glob("*.md") if p.is_file())
    except OSError:
        return []
    if not names:
        return []

    sent_relpath = _relpath(sent_dir, worktree_root)
    spine = read_tree_spine(worktree_root, [f"{sent_relpath}/{names[0]}"])
    if spine is None:
        return []
    in_head = spine.get(sent_relpath, {})
    if not any(name in in_head for name in names):
        return []

    ledger_relpath = _relpath(
        Path(_machinery_paths.memo_outbox_sent_ledger_path(str(worktree_root))),
        worktree_root,
    )
    reports: list = []
    for name in names:
        if name in in_head:
            continue
        path = sent_dir / name
        reports.append(
            _candidate(
                path,
                "report",
                None,
                f"delivered but this repo holds no committed receipt: "
                f"memo.send writes this copy only after confirming the "
                f"delivery commit in the receiver's own object store, and "
                f"nothing in HEAD records it. Complete the receipt with "
                f"`git add -- {_relpath(path, worktree_root)} {ledger_relpath}` "
                f"and a scoped commit, or delete this copy if the delivery "
                f"was itself rolled back",
            )
        )
    return reports


def _candidate(path: Path, disposition: str, status: Optional[str], note: Optional[str]) -> dict:
    return {
        "id": str(path),
        "filename": path.name,
        "topic": path.stem,
        "path": str(path),
        "status": status,
        "disposition": disposition,
        "note": note,
    }


def _reconcile(worktree_root: Path, dry_run: bool) -> tuple[list, list, list]:
    sent_dir = Path(_machinery_paths.memo_outbox_sent_dir(str(worktree_root)))
    candidates: list = []
    acted: list = []
    skipped: list = []

    for path in merged_outbox_drafts(worktree_root):
        disposition, status, note = _classify(path, archived=(sent_dir / path.name).exists())
        candidate = _candidate(path, disposition, status, note)
        candidates.append(candidate)

        if dry_run:
            continue
        if disposition == "report":
            skipped.append(candidate)
            continue
        if disposition != "move":
            continue

        target = sent_dir / path.name
        if target.exists():
            skipped.append(
                dict(
                    candidate,
                    note=(
                        f"sent/{path.name} already exists — the archived copy is "
                        f"authoritative; left in place rather than clobbering it"
                    ),
                )
            )
            continue
        try:
            sent_dir.mkdir(parents=True, exist_ok=True)
            os.replace(path, target)
        except OSError as exc:
            skipped.append(dict(candidate, note=f"move failed: {exc}"))
            continue
        acted.append(
            dict(candidate, id=str(target), path=str(target), source_path=str(path))
        )

    for report in _uncommitted_receipts(worktree_root, sent_dir):
        candidates.append(report)
        if not dry_run:
            skipped.append(report)

    return candidates, acted, skipped


@register_op("memo.reconcile_outbox")
def _memo_reconcile_outbox(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'memo.reconcile_outbox' MUTATING UDS op handler.

    Move every already-delivered entry out of the calling repo's outbox
    into its `sent/` subdirectory, so the outbox depth is a count of work
    rather than a count of files. Sources candidates from BOTH the canonical
    `.coordinator-local/memo-outbox/` root and the retired `state/memo-outbox/`
    root; moves are always written to the root the entry was found under. The
    retired-root read leg is temporary — see `machinery_paths.py`'s
    REMOVAL TRIGGER comment above `memo_outbox_dir` for the exact drop
    condition. See the module docstring for what is deliberately NOT swept.

    Params:
        dry_run (bool, required): true previews (every entry comes back with
            its `disposition`, nothing is moved); false performs the moves.

    Returns:
        `build_dry_run_result` on dry_run, else `build_act_result` whose
        `acted` entries carry the NEW `sent/` path — commit them yourself;
        this op does not commit (module docstring, Negative-spec).
    """
    validated = _validate_params(params)
    if isinstance(validated, dict):
        return validated
    dry_run = validated

    if repo_root is None:
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.reconcile_outbox: no repo_root supplied — this op reconciles "
            "the CALLING repo's own .coordinator-local/memo-outbox/ and requires a resolved "
            "worktree (common_dir-keyed op).",
        )
    worktree = main_worktree_root(Path(repo_root))

    candidates, acted, skipped = _reconcile(worktree, dry_run)

    if dry_run:
        return build_dry_run_result(_MODE, candidates)
    result = build_act_result(_MODE, acted, skipped, [])

    # Claim the REAL write set (ipc.py's `_SCOPE_TOUCH_PATHS_KEY` contract:
    _written: list = []
    for entry in acted:
        target = entry.get("path")
        source = entry.get("source_path")
        if target:
            _written.append(str(target))
        if source:
            _written.append(str(source))
    if _written:
        result["_scope_touch_paths"] = _written
    return result
