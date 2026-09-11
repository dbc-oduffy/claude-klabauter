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

This op reconciles that: every entry whose `status:` is not `draft` is MOVED
to `sent/`, never deleted, so the paper trail survives and an operator can
still audit what went where.

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
`status:` field. This op deliberately uses the local field anyway: in all 20
non-draft entries checked against their receiving repos the two agreed, and a
sweep that reaches into peer trees to read them is a different, heavier op
with a different blast radius. A local status that lies about delivery is a
defect in whatever wrote it, and is not made better by this op guessing.

Negative-spec:
  - Does NOT delete anything, ever. Every action is a move into `sent/`.
  - Does NOT clobber: an entry whose `sent/<name>` already exists is skipped
    with a note, never overwritten — the archived copy is authoritative.
  - Does NOT touch `status: draft` entries. A draft's home IS the outbox;
    moving one would be the very over-count this op exists to fix, inverted.
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

#: The one status whose home is the outbox itself. Everything else has
#: reached its receiver (or been resolved before it could) and is history.
_LIVE_STATUS = "draft"

_KNOWN_PARAM_KEYS = frozenset({"dry_run"})

#: Data-dependent within one fixed directory pair: whichever already-delivered
#: entries the calling repo's own outbox is holding, moved into its `sent/`.
#: Sourced from BOTH the new and retired outbox roots (2026-09-03
#: relocation), always moved to the new `sent/` -- see `_reconcile`.
MUTATES = [
    f"{MEMO_OUTBOX_RELDIR}/*.md",
    f"{LEGACY_MEMO_OUTBOX_RELDIR}/*.md",
    f"{MEMO_OUTBOX_RELDIR}/sent/*.md",
]


def _validate_params(params: dict):
    """Validate memo.reconcile_outbox params; return dry_run or a setup-error dict."""
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
    """Return (disposition, status, note) for one outbox `*.md`.

    Dispositions: "move" (delivered, belongs in sent/), "keep" (a live draft),
    "report" (no frontmatter — an orphaned body fragment, operator's call),
    "keep" also covers an unreadable file: this op refuses to move a file
    whose status it could not read, because the failure mode of guessing
    wrong is archiving a live draft nobody will send.

    `archived` — a `sent/<name>` for this entry already exists — decides the
    DRAFT case and nothing else. A draft is normally the one thing that
    belongs in the outbox, but a draft with an archive is a delivery whose
    send could not remove its own original (memo_send's unlink degrades to a
    warning), so it is a stale duplicate rather than work. It is reported,
    not moved: the archived copy is authoritative and moving onto it would
    clobber the stamped one with the unstamped one. A non-draft entry is
    unaffected — the existing clobber-skip in `_reconcile` already says the
    same thing at the move site.
    """
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
    return "move", status, None


def _relpath(path: Path, worktree_root: Path) -> str:
    return os.path.relpath(str(path), str(worktree_root)).replace(os.sep, "/")


def _uncommitted_receipts(worktree_root: Path, sent_dir: Path) -> list:
    """Report every `sent/*.md` that HEAD does not know — a delivery whose
    sender-side receipt never committed.

    One `read_tree_spine` call answers for the whole directory: it walks only
    the sent dir's own spine and reads each directory's tree object once, in
    process, so the cost is O(path depth) and zero git spawns however many
    archived memos this repo holds (44 here, all in HEAD).

    A `None` spine — no resolvable HEAD, or a tree object that would not read
    — returns nothing at all. This check distinguishes "delivered but never
    committed" from "committed", and a repo that cannot answer the second
    question has not answered the first one in the negative.

    SO DOES AN ARCHIVE HEAD KNOWS NOTHING OF, and that arm is what keeps this
    check honest off this box. `.coordinator-local/` is gitignored fleet-wide
    (.gitignore's 2026-09-02 relocation stanza); claude-klabauter's 44 archived memos
    are in HEAD only because they were tracked before the ignore landed and
    `memo.send` names them explicitly ever since. A repo that never tracked
    the bucket has an entirely absent archive, and reporting a receiptless
    delivery for every memo it ever sent would be this instrument inventing
    the loudest possible finding out of a repo that is perfectly healthy.
    Absence is informative only where presence is the norm, so the norm is
    measured rather than assumed: no archived memo in HEAD means this repo
    does not track its archive, and nothing here is a finding. The cost is
    under-reporting a repo whose FIRST send lost its receipt — the correct
    direction to be wrong in, for an instrument whose whole value is that a
    row it prints is real.
    """
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
    """Return (candidates, acted, skipped) for the calling repo's outbox.

    Sources candidates from BOTH the new `.coordinator-local/memo-outbox/`
    root and the retired `state/memo-outbox/` root (2026-09-03 relocation) —
    a topic present at both surfaces the new-root copy only, via the shared
    `memo_draft.merged_outbox_drafts` helper (was a duplicated ~15-line merge
    block; see Review comment below — Kira, overengineering-reviewer). Every
    moved entry, wherever it was found, lands in the NEW `sent/` dir; nothing
    is ever written back to the retired root.

    In dry-run nothing is touched and `acted`/`skipped` stay empty — the
    caller reads `candidates`' own `disposition` field to see what an act run
    would do.

    The sweep ends with `_uncommitted_receipts`, which looks the other way
    down the same channel: not an entry that is still in the outbox after
    delivery, but a delivery with no committed receipt anywhere in this repo.
    """
    sent_dir = Path(_machinery_paths.memo_outbox_sent_dir(str(worktree_root)))
    candidates: list = []
    acted: list = []
    skipped: list = []

    # Review: overengineering-reviewer (Kira) — was a verbatim copy of
    # memo_list_outbox's dual-root merge; now the shared implementation.
    for path in merged_outbox_drafts(worktree_root):
        disposition, status, note = _classify(path, archived=(sent_dir / path.name).exists())
        candidate = _candidate(path, disposition, status, note)
        candidates.append(candidate)

        if dry_run:
            continue
        if disposition == "report":
            # Act mode's envelope has no `candidates` key, so an orphan
            # fragment would vanish from the result entirely if it were not
            # surfaced here. `skipped` is where it belongs: this op cleanly
            # declined to act, and the disposition is an operator's call.
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
        # `source_path` is carried alongside the overwritten `id`/`path` (which
        # both become the NEW sent/ location, per this op's documented envelope)
        # so the caller can claim the vacated source as well -- see the
        # `_scope_touch_paths` block in the handler.
        acted.append(
            dict(candidate, id=str(target), path=str(target), source_path=str(path))
        )

    # Reported last so an operator reads the queue they were asking about
    # before the archive anomaly underneath it. These are never `acted`: this
    # op moves outbox entries, and a sent/ copy is already where it belongs —
    # what is missing is a COMMIT, which this op does not make (Negative-spec).
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
            # Review: coordinator:code-reviewer — error named the retired write root; corrected to canonical.
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
    # paths actually written this call, never the `MUTATES` surface -- the two
    # legitimately diverge, and a `report`/`keep`/clobber-skip entry moves
    # nothing). Both ends of every `os.replace` are declared: the vacated
    # source as well as the new `sent/` target, because a move is a deletion
    # at the source and Check 5's sink must be able to attribute that deletion
    # to this session too. Without this, every file this op lands in `sent/`
    # reaches `compute_scope` as an owner-less orphan -- one of the four
    # undeclared-op-output orphans in 2026-08-27's scope-warnings.log, the arm
    # gating the scope-strict flip (Check 5, `bash_guards/dispatch_checks.py`).
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
