"""
coordinator_core.ops.queue_close — atomic improvement-queue closure writer
("queue.close" op).

Purpose: the improvement queue has no owning closure op. 36 entries under
``state/improvement-queue/`` already carry a hand-stamped ``status: closed``
(35 plain, one YAML-quoted) — evidence of a two-step discipline (stamp, then
a SEPARATE ``git mv`` into ``archive/improvement-queue/YYYY-MM/``) that
``backlog-prune-discipline.md`` (DoE wiki) names as an anti-pattern and
``quick-wrap/SKILL.md`` § "2. Close the loose ends" calls out directly: "An
entry marked closed but still sitting in `state/` is the failure mode this
shape prevents." 24 of the 36 also carry no ``closed_by`` — the stamp step
itself is incomplete, not just unswept.

``queue.close`` makes closure ATOMIC end-to-end rather than ratifying that
gap:

  1. **Stamp** — flip ``status:`` to ``closed`` (a no-op if already closed)
     and backfill any MISSING ``closed_at:`` / ``closed_by:`` fields (never
     overwrite an existing value — a repeat call on an already-fully-stamped
     entry changes nothing). "deferred" is a distinct, explicitly-deprioritized
     terminal-ish status (see the improvement-queue schema's own enum
     description) and is refused, not silently promoted to "closed".
  2. **Commit its own write**, scoped to exactly the one entry path, via
     ``coordinator_core.ops.ceremony.git_native.commit_scoped`` — the same
     writer-commits shape ``coordinator_core.ops.ceremony.consumed_
     handoff_stamp.post_commit_stamp_and_ship`` and
     ``coordinator_core.ops.plan_status_transition._commit_plan_flip``
     already prove: the op never exits with the stamp left as an unswept
     dirty working-tree edit. Never a bare/broad pathspec.
  3. **Complete the motion** by invoking the existing, unmodified
     ``fleet.archive_queue_entry`` (``dry_run=False``) for the same entry —
     which now receives a terminal write that is ALREADY COMMITTED, so the
     archival op stops laundering a write it merely found (DR-211 § 4
     Invariant 4: "No exception exists beyond these two [sanctioned commit]
     forms" — an archival op committing content it did not itself stage from
     a known-clean state is not one of them). Calling ``queue.close`` on any
     of the 36 pre-existing entries (even ones already fully stamped) drains
     them into the archive on this same call, rather than leaving the
     residue for a human to sweep by hand.

Spec backlink: docs/plans/2026-08-05-*-improvement-queue-closure-writer.md § C12
  (dispatch brief: "The improvement queue has no owning op: give closure a
  writer that commits")
DR authority: docs/decisions/DR-270-queue-closure-writer-side-commit-ownership.md
  — extends the writer-side-commit-ownership category DR-211 § D1 first
  ratified for ``fleet.*`` specifically; DR-211 does NOT itself cover
  ``queue.close`` (see that DR's own "Ops sanctioned" list).

Negative-spec:
  - Does NOT reimplement the git-mv/archive-destination-derivation logic —
    delegates to the existing, UNMODIFIED ``fleet.archive_queue_entry``
    handler (resolved via ``coordinator_core.ipc.get_op_handler``, the
    public by-key resolution seam every sibling op-composing caller uses —
    e.g. ``consumed_handoff_stamp._live_children_guard`` resolving
    ``handoff.has_live_children`` the same way), never a hand-rolled
    re-derivation of its YYYY-MM / idempotent-replay rules.
  - Does NOT scan ``state/improvement-queue/`` for terminal entries — like
    ``fleet.archive_queue_entry``, the caller names the one ``entry_path``
    to close. A batch-close ceremony calls this op once per entry.
  - Does NOT duplicate ``fleet.prune_bugs`` (a different family, BUG
    backlog, read-only self-selecting candidate discovery that writes
    nothing) — disjoint op, disjoint substrate.
  - Does NOT silently promote a "deferred" entry to "closed" — see the
    stamp step above.
  - Does NOT use ``git add -A``/``git add .`` — scoped exact-pathspec only,
    via ``git_native.commit_scoped`` (DR-211 D3 Invariant 4).
  - Does NOT require the caller to separately invoke
    ``fleet.archive_queue_entry`` afterward — step 3 above is unconditional
    once the entry is (or already was) closed.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    read_fm_field,
    read_fm_field_unquoted,
    replace_fm_field,
)
from coordinator_core.ipc import get_op_handler, register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.fleet._common import check_repo_root, main_worktree_root
from coordinator_core.wire_paths import rel_id

_PROG = "queue.close"


def _today_iso() -> str:
    """Return today's UTC date as ``YYYY-MM-DD`` — matches the plain (unquoted)
    date convention every hand-authored ``created:``/``closed_at:`` field on
    disk already uses (e.g. ``closed_at: 2026-07-25``)."""
    return datetime.now(timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------
# Stamp mutate closure
# ---------------------------------------------------------------------------


def _build_close_mutate(closed_by: str, closed_at: str):
    """Build the ``locked_rmw`` mutate closure + result-state dict for one
    queue.close stamp attempt.

    Mirrors ``plan_status_transition._stamp_implemented``'s mutate-closure
    shape (a module-local dict populated as a side effect, returned alongside
    the closure) so the caller can inspect what happened without re-parsing
    text.

    Idempotent by construction: an entry whose status is already "closed"
    with both ``closed_at`` and ``closed_by`` present returns ``old_text``
    unchanged (``locked_rmw`` skips the write on a byte-identical return) —
    a repeat ``queue.close`` call never re-stamps or re-commits an
    already-closed entry. An already-closed entry MISSING ``closed_at``/
    ``closed_by`` (the 24-of-36 residue population this op exists to drain)
    gets exactly the missing fields backfilled — an existing value is never
    overwritten with a fresh one.

    ``status`` is read via ``read_fm_field_unquoted`` (comparison-safe: strips
    both a trailing comment and one layer of YAML quoting) so the one
    YAML-quoted ``status: "closed"`` entry on disk compares equal to the
    unquoted literal ``"closed"`` — matching ``records_query._clause_matches``'
    own str()-coerced comparison, verified empirically against that entry via
    ``queue_family.load_family_records(..., where="status = closed")``
    (see the DR for the write-up). The value is WRITTEN back unquoted
    (``replace_fm_field(..., "closed")``) either way, normalizing that one
    quoted outlier onto the same convention every other entry already uses.

    "deferred" is a distinct terminal-ish status (explicitly deprioritized,
    per the improvement-queue schema's own enum description) — queue.close
    refuses to silently promote it to "closed" (``state["skipped_reason"] =
    "deferred"``, ``old_text`` returned unchanged, no commit); the caller
    must flip it back to ``status: open`` before this op will close it.
    """
    state: dict = {
        "changed": False,
        "prior_status": None,
        "skipped_reason": None,
    }

    def mutate(old_text: str) -> str:
        text = old_text.replace("\r\n", "\n")

        raw_status = read_fm_field(text, "status")
        if raw_status is None:
            raise MutateAbort(f"{_PROG}: no 'status' field found in entry")

        status = read_fm_field_unquoted(text, "status")
        state["prior_status"] = status

        if status == "deferred":
            state["skipped_reason"] = "deferred"
            return old_text

        if status not in ("open", "closed"):
            raise MutateAbort(
                f"{_PROG}: unexpected status {status!r} for entry — "
                "expected one of: open, closed, deferred"
            )

        new_text = text
        # Rewrite whenever the RAW on-disk text isn't already the bare
        # canonical `closed` scalar -- covers both the semantic flip
        # (open -> closed) AND normalizing the one YAML-quoted
        # `status: "closed"` shape (status is already "closed" once
        # unquoted, but the raw text still needs rewriting).
        if status != "closed" or raw_status.strip() != "closed":
            new_text = replace_fm_field(new_text, "status", "closed")
            state["changed"] = True

        if read_fm_field(new_text, "closed_at") is None:
            new_text = insert_fm_field(new_text, "closed_at", closed_at, after_key="status")
            state["changed"] = True

        if read_fm_field(new_text, "closed_by") is None:
            new_text = insert_fm_field(new_text, "closed_by", closed_by, after_key="closed_at")
            state["changed"] = True

        return new_text if state["changed"] else old_text

    return mutate, state


# ---------------------------------------------------------------------------
# Writer-side commit (step 2)
# ---------------------------------------------------------------------------


def _commit_close(
    worktree_root: Path,
    entry_path: Path,
    content: str,
    closed_by: str,
    *,
    resumed: bool = False,
) -> git_native.GitResult:
    """Commit the entry's own status-closure stamp, scoped to exactly the
    entry path just written — the writer-side commit-ownership shape this
    module's docstring documents.

    Routed through ``git_native.commit_authored_content`` (C2's no-worktree-
    read entrypoint), NOT ``git_native.commit_scoped`` — ``content`` is the
    exact authored bytes the caller already validated (either
    ``locked_rmw``'s own return value on a fresh stamp, or the re-read,
    re-validated bytes on a resume — see ``_handler``'s "resume" branch and
    ``_revalidate_closed_for_resume``), so this function commits precisely
    what the caller vouches for rather than re-opening the path itself.

    ``resumed`` only changes the commit message's verb (so ``git log`` on a
    resumed commit reads distinctly from a same-call flip) — it carries no
    other behavioural difference.

    ``closed_by`` is also passed to ``commit_authored_content`` as
    ``attributed_session_id`` (state/bug-backlog/2026-08-18-scoped-git-commit-
    stamps-a-foreign-session-id-8d21f0c4e7b9.yaml) — it is this op's own
    required, params-validated identity for "who is closing this entry",
    the same authority a caller-resolved session id holds over
    ``commit_authored_content``'s blind env-var read (see that function's
    own docstring). A ``closed_by`` that is not UUID-shaped (a human/EM
    name rather than a session id) is a no-op here — the `Session-Id:`
    trailer resolution falls through to the blind read unchanged, exactly
    as `compute_missing_trailer_args`'s own UUID fail-safe already
    guarantees.

    Returns the raw ``GitResult`` — the caller decides how a failure maps to
    the op's own response envelope.
    """
    relpath = rel_id(entry_path.resolve(), worktree_root.resolve())
    verb = "stamp status -> closed (resumed)" if resumed else "stamp status -> closed"
    message = f"{_PROG}: {verb} on {relpath} (closed_by={closed_by})\n"

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(message)
        msg_path = fh.name
    try:
        return git_native.commit_authored_content(
            relpath, content, msg_path, worktree_root,
            attributed_session_id=closed_by,
        )
    finally:
        try:
            Path(msg_path).unlink()
        except OSError:
            pass


def _entry_dirty(worktree_root: Path, relpath: str) -> bool:
    """Return True iff ``relpath`` carries any uncommitted, PREVIOUSLY-TRACKED
    change (staged and/or unstaged) per ``git status --porcelain`` — the
    stranded-write detector for this op's resume branch (mirrors
    ``plan_status_transition._plan_path_dirty`` verbatim).

    Excludes a bare ``??`` (untracked) entry deliberately: a queue entry that
    was never touched by this op and has simply never been committed at all
    must still take the genuine terminal no-op path, not be mistaken for a
    stranded write this op itself left behind.
    """
    result = git_native.status_porcelain(worktree_root)
    if not result.ok:
        return False
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        code, entry = line[:2], line[3:]
        if code == "??":
            continue
        if entry == relpath or entry.endswith(f" -> {relpath}"):
            return True
    return False


def _revalidate_closed_for_resume(entry_path: Path, repo_root: Path) -> str:
    """Re-read ``entry_path`` under the write lock and confirm its on-disk
    frontmatter is genuinely terminal-closed AND matches this op's own close
    verb — status == "closed" (unquoted), ``closed_at`` present, ``closed_by``
    present — before committing bytes this invocation did not itself author.

    A resume commits content written by a PREVIOUS process; committing it
    unvalidated would re-open the exact defect this plan closes. Raises
    ``MutateAbort`` (propagated to the caller, which fails loud per AC10)
    when the re-read content no longer carries the expected terminal state
    — e.g. a concurrent writer flipped it back to "open" between this
    call's dirty-probe and this re-read.

    Returns the (unchanged) on-disk text via ``locked_rmw``'s return value
    — the mutate closure below never rewrites, only validates, so the write
    is always a byte-identical no-op and this is purely a lock-held read.
    """

    def mutate(old_text: str) -> str:
        text = old_text.replace("\r\n", "\n")
        status = read_fm_field_unquoted(text, "status")
        if status != "closed":
            raise MutateAbort(
                f"{_PROG}: resume re-validation failed for {entry_path} — "
                f"expected status 'closed', found {status!r}"
            )
        if read_fm_field(text, "closed_at") is None:
            raise MutateAbort(
                f"{_PROG}: resume re-validation failed for {entry_path} — missing 'closed_at'"
            )
        if read_fm_field(text, "closed_by") is None:
            raise MutateAbort(
                f"{_PROG}: resume re-validation failed for {entry_path} — missing 'closed_by'"
            )
        return old_text

    return locked_rmw(entry_path, mutate, repo_root=repo_root)


# ---------------------------------------------------------------------------
# Response envelope
# ---------------------------------------------------------------------------


def _reply(
    *,
    closed: bool,
    archived: bool,
    dest: Optional[str],
    exit_code: int,
    error: Optional[str] = None,
    committed_sha: Optional[str] = None,
    skipped_reason: Optional[str] = None,
    resumed: bool = False,
) -> dict:
    return {
        "closed": closed,
        "archived": archived,
        "dest": dest,
        "committed_sha": committed_sha,
        "skipped_reason": skipped_reason,
        "resumed": resumed,
        "exit_code": exit_code,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


@register_op("queue.close")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """queue.close — stamp one improvement-queue entry closed, commit that
    stamp, then complete the archival motion via ``fleet.archive_queue_entry``.

    Required params:
        entry_path (str) — repo-relative or absolute path under
            ``state/improvement-queue/``.
        closed_by  (str) — identity stamping the closure (session/EM name).
            Required so this op can never reproduce the missing-``closed_by``
            gap 24 of the 36 pre-existing entries carry.

    Optional params:
        closed_at  (str) — ``YYYY-MM-DD``; defaults to today (UTC).
        repo_root  (str) — D3 consistency check only (never the worktree-root
            resolution source — see ``check_repo_root``).

    repo_root arg: the git common dir delivered by ``_OP_KEY_SCOPE=
    "common_dir"``. Handlers MUST NOT use ``ctx.repo_root`` (None in the
    global service). Worktree root is derived via
    ``main_worktree_root(repo_root)``, matching ``fleet.archive_queue_entry``.

    Response shape — ``closed`` semantics: ``result["closed"]`` means "THIS
    CALL verified or performed the stamp," NOT "the entry is closed." A call
    against an entry that is already gone (already archived by a prior call,
    or a race with a concurrent close) returns ``closed=False`` even though
    the entry unambiguously IS closed — it's sitting in the archive. Check
    ``result["archived"]`` (or the final ``dest``) to learn the entry's actual
    terminal state; ``closed`` alone answers "did I do the stamping," not
    "is it done."
    """
    entry_path_raw: str = (params.get("entry_path") or "").strip()
    closed_by: str = (params.get("closed_by") or "").strip()
    closed_at: str = (params.get("closed_at") or "").strip() or _today_iso()
    param_repo_root = params.get("repo_root")

    if not entry_path_raw:
        return _reply(closed=False, archived=False, dest=None, exit_code=2,
                       error="'entry_path' is required")
    if not closed_by:
        return _reply(closed=False, archived=False, dest=None, exit_code=2,
                       error="'closed_by' is required")

    if repo_root is None:
        return _reply(
            closed=False, archived=False, dest=None, exit_code=1,
            error="repo_root is None; cannot derive worktree root (keying-table misconfiguration)",
        )

    common_dir = Path(repo_root)
    mismatch = check_repo_root(param_repo_root, common_dir)
    if mismatch:
        return _reply(closed=False, archived=False, dest=None, exit_code=1, error=mismatch)

    worktree = main_worktree_root(common_dir)
    queue_dir = worktree / "state" / "improvement-queue"

    candidate = Path(entry_path_raw)
    if not candidate.is_absolute():
        candidate = worktree / candidate

    contained = contained_path(candidate, [queue_dir])
    if contained is None:
        return _reply(
            closed=False, archived=False, dest=None, exit_code=2,
            error=f"entry_path escapes state/improvement-queue/: {entry_path_raw!r}",
        )

    closed_flag = False
    committed_sha: Optional[str] = None
    resumed_flag = False

    if contained.exists():
        mutate, state = _build_close_mutate(closed_by, closed_at)
        try:
            new_text = locked_rmw(contained, mutate, repo_root=common_dir)
        except FileNotFoundError:
            # Raced away between the .exists() check and the lock acquisition —
            # treat identically to the "already gone" branch below.
            pass
        except LockTimeout as exc:
            return _reply(closed=False, archived=False, dest=None, exit_code=1,
                           error=f"stamp lock timeout: {exc}")
        except MutateAbort as exc:
            return _reply(
                closed=False, archived=False, dest=None, exit_code=1,
                error=str(exc.args[0]) if exc.args else "stamp mutate aborted",
            )
        else:
            if state["skipped_reason"] == "deferred":
                return _reply(
                    closed=False, archived=False, dest=None, exit_code=0,
                    skipped_reason="deferred",
                )

            if state["changed"]:
                commit_result = _commit_close(worktree, contained, new_text, closed_by)
                if not commit_result.ok:
                    return _reply(
                        closed=False, archived=False, dest=None, exit_code=1,
                        error=(
                            f"status flip succeeded but committing it failed: "
                            f"{commit_result.stderr}"
                        ),
                    )
                rev_result = git_native.rev_parse_head(worktree)
                committed_sha = rev_result.stdout.strip() if rev_result.ok else None
            else:
                # Stranded-write resume: locked_rmw returned old_text
                # unchanged — this call's own stamp attempt found nothing to
                # flip — but that alone does NOT mean a prior run's stamp+
                # commit fully completed. A prior invocation may have
                # flipped and written the frontmatter, then failed before
                # ITS OWN commit landed, leaving the write stranded, dirty,
                # uncommitted on disk. Detect that BEFORE the unconditional
                # fleet.archive_queue_entry delegation below (AC6:
                # archive_queue_entry carries no dirty-tree skip guard and
                # will git-mv a stranded dirty entry out from under this op
                # if the probe does not run first).
                relpath = rel_id(contained.resolve(), worktree.resolve())
                if _entry_dirty(worktree, relpath):
                    try:
                        revalidated_content = _revalidate_closed_for_resume(
                            contained, common_dir,
                        )
                    except FileNotFoundError:
                        pass
                    except LockTimeout as exc:
                        return _reply(
                            closed=False, archived=False, dest=None, exit_code=1,
                            error=f"resume re-validation lock timeout: {exc}",
                        )
                    except MutateAbort as exc:
                        # AC10: authenticate-before-commit — the on-disk
                        # content no longer carries the expected terminal
                        # close state, so this op refuses to commit it and
                        # fails loud rather than laundering an unverified
                        # write.
                        return _reply(
                            closed=False, archived=False, dest=None, exit_code=1,
                            error=(
                                str(exc.args[0]) if exc.args else "resume re-validation aborted"
                            ),
                        )
                    else:
                        commit_result = _commit_close(
                            worktree, contained, revalidated_content, closed_by, resumed=True,
                        )
                        if not commit_result.ok:
                            return _reply(
                                closed=False, archived=False, dest=None, exit_code=1,
                                error=(
                                    f"entry has a stranded uncommitted status flip from a "
                                    f"prior run, and committing it now failed too: "
                                    f"{commit_result.stderr}"
                                ),
                            )
                        rev_result = git_native.rev_parse_head(worktree)
                        committed_sha = rev_result.stdout.strip() if rev_result.ok else None
                        resumed_flag = True

            closed_flag = True

    # else: source already gone (already archived, or a race with a concurrent
    # close) — nothing to stamp. `closed_flag` stays False (this call verified
    # nothing); the archive step below still runs and takes its own idempotent
    # no-op path (fleet.archive_queue_entry's AC7 replay contract).

    archive_handler = get_op_handler("fleet.archive_queue_entry")
    if archive_handler is None:
        return _reply(
            closed=closed_flag, archived=False, dest=None, exit_code=1,
            error="fleet.archive_queue_entry not registered", committed_sha=committed_sha,
            resumed=resumed_flag,
        )

    try:
        archive_reply = await archive_handler(
            {"entry_path": entry_path_raw, "dry_run": False}, common_dir,
        )
    except Exception as exc:  # noqa: BLE001 -- delegation boundary: never let an
        # unhandled raise from fleet.archive_queue_entry (or anything it calls
        # transitively) propagate past this op's own honest-envelope contract.
        # The stamp commit may have already landed durably (committed_sha
        # populated) even though archival failed -- the caller still needs
        # that SHA back, not a bare traceback.
        return _reply(
            closed=closed_flag, archived=False, dest=None, exit_code=1,
            error=f"archive delegation raised: {exc}", committed_sha=committed_sha,
            resumed=resumed_flag,
        )

    return _reply(
        closed=closed_flag,
        archived=bool(archive_reply.get("archived")),
        dest=archive_reply.get("dest"),
        exit_code=archive_reply.get("exit_code", 0),
        error=archive_reply.get("error"),
        committed_sha=committed_sha,
        resumed=resumed_flag,
    )
