"""
coordinator_core.ops.propagate_body — JSON-RPC "handoff.propagate" operation.

Purpose: a peer-delivery door for a `status: claimed` (or legacy `status:
consumed`) `state/handoffs/*.md` file — the NON-author case
`handoff.correct_body` (DR-247) structurally excludes via its authorship gate.
A session that discovers a claimed baton's premises have gone stale (but did
NOT author that baton) needs a sanctioned way to deliver the correction onto
disk, in a form the holding session will actually see the next time it reads
the file. This op is that door.

THIS IS A SIBLING VERB, NOT A WIDENING OF `handoff.correct_body`. That op's
`:94` negative-spec ("Does NOT grow into a general handoff-body editor — no
section adds") is deliberate and load-bearing — collapsing the two ops would
merge two contracts that are each other's exact inverse (author-only,
single-replacement, 512-byte cap vs peer-any, append-only, ~8KB cap). See
docs/plans/2026-08-01-baton-spine-information-integrity.md Anti-scope.

Authority: PM ruling 2026-08-01 (docs/plans/2026-08-01-baton-spine-
information-integrity.md § Part B). DR-247 § 3 is what LICENSES dropping the
authorship gate entirely for this op: it names that gate as anti-accident,
not anti-adversary — "the real control was always the paper trail, not the
gate." This op keeps the paper trail (a stamped, dated, auditable delivery
note under `## Propagated`) while dropping a gate that was never a security
boundary for the author case either, and that structurally cannot apply to
the peer case (the calling session is BY DEFINITION not the author).

Contract (AC7):
    - NO authorship gate. Any calling session may deliver into any claimed
      (or legacy consumed) handoff.
    - APPEND-ONLY into a single bounded, clearly-demarcated `## Propagated`
      section — created once, appended to on every subsequent call, never a
      new heading per delivery (mirrors `handoff_correct_body._append_
      correction_note`'s accumulation shape).
    - Reuses the EXISTING in-body amendment-note token convention from
      `coordinator/skills/plan/SKILL.md` (`**Amended <YYYY-MM-DD> by
      <slug>:** <one-line change>` / `**Superseded <YYYY-MM-DD> by
      <slug>:**`) — NO new frontmatter/schema field (PM declined one as too
      narrow, 2026-08-01). Same greppable token family, extended here from a
      top-of-file plan-amendment one-liner to a bounded baton section.
    - Own net-growth bound (`_NET_GROWTH_CAP` below), sized for a real
      propagation note with file:line citations, NOT DR-247's 512-byte
      correction cap — the reported real note was ~6KB.

Mechanism: the same guard-safe node-write seam `handoff.correct_body` and
`handoff.stamp` use — `locked_rmw` (flock-protected read-mutate-write) under
`asyncio.to_thread`, `contained_path`/`main_worktree_root` for `state/
handoffs/`-only containment and worktree derivation. Never goes through the
Edit or Bash tool, so `block_consumed_handoff_edit` never sees this write and
is never asked to allow anything (DR-073's guard-safe-node-write precedent).

AC12 — THIS OP GIT-COMMITS ITS OWN WRITE, a deliberate, PM-ruled divergence
from `handoff.correct_body`/`handoff_stamp`'s "does NOT git-commit — the
caller commits" negative-spec. Reason: a commit is durable and travels across
machines/clones; a dirty worktree does not, and reaching the holding session
is the entire point of a delivery. The commit is scoped to the single target
file only, never a broad add.

AC12 preconditions (checked BEFORE any file mutation, in order):
    1. Abort if the target file is dirty in the holder's tree (`git status
       --porcelain -- <path>` non-empty) — otherwise a pathspec-scoped commit
       would sweep the holder's own uncommitted edits to that file into the
       delivery commit under the delivering session's authorship.
    2. Abort if any in-progress git operation is detected (`MERGE_HEAD`,
       `rebase-merge`/`rebase-apply`, `CHERRY_PICK_HEAD`) or if HEAD is
       detached — a commit issued mid-rebase/merge/cherry-pick either errors
       opaquely or advances the holder's own operation; a detached-HEAD
       commit orphans the delivery.
    3. The commit is landed via `git commit-tree` + a compare-and-swap
       `git update-ref` — PLUMBING commands that run NO git hooks, so
       `coordinator-prepare-commit-msg` (which fires on every ordinary `git
       commit` and would otherwise stamp the DELIVERING session's own
       `Session-Id:`/`Deliverable-Id:` trailers) never runs at all. This is
       not a suppression applied after the fact — the hook simply never
       fires, mirroring `coordinator_core/ops/ceremony/git_native.py`'s own
       `_commit_scoped_private_index`'s documented reason for using the same
       plumbing (AC18, docs/plans/2026-07-27-computed-commit-mechanism-
       selection.md). This op composes its OWN commit-message trailers
       instead — `Nature: peer-delivery` and `Delivered-By: <slug>` — so a
       reader of `git log` can immediately tell this commit is peer-delivery
       provenance, never the delivering session's own ship-state evidence.
    4. SUBAGENT-INVOCABILITY (explicit, per review requirement): this op IS
       reachable from a dispatched-subagent context. `coordinator_core.invoke`
       (the CLI wrapper a Bash-tool call would use to reach this op) has no
       agent-identity concept at all — `block_subagent_commit` inspects the
       shape of a `Bash`-tool command line (does it look like `git commit`?),
       and this op's own `git commit-tree`/`git update-ref` calls never
       appear on that command line; they run inside THIS process as
       subprocess calls the guard never sees. There is no existing gate at
       the `coordinator_core.ops`/dispatch layer that could restrict this by
       caller identity (no op today conditions its own behavior on agent_id
       — that concept exists only in the write_guards/bash_guards layer that
       inspects Claude Code tool_input, which this op's invocation shape does
       not pass through). Building a NEW agent-identity gate at this layer is
       out of this chunk's scope (B1 authors the op; B2/B3 touch the write
       guards). This is therefore a KNOWING, BOUNDED relaxation of
       `block_subagent_commit`'s intent — pinned by
       `test_subagent_context_env_does_not_block_delivery_commit` in this
       module's test file, which sets `CLAUDE_CODE_CHILD_SESSION=1` (the
       empirically-observed subagent marker documented in
       `handoff_correct_body.py`'s own module docstring) and asserts the
       delivery commit still lands. A future session tightening this MUST
       update that test alongside any new gate.

B2 EXTENSION (AC8): this module also registers `plan.propagate`, targeting
live `docs/plans/**.md` plan bodies instead of `state/handoffs/*.md` batons.
Re-derived scope (review correction): `block_subagent_plan_body_write`
blocks ONLY a `coordinator:executor`-subagent write to a plan body — a
top-level EM peer-delivery write is already unblocked today via that
guard's own allow condition (1), "No agent_id -> always allow." So the case
this extension answers is narrower than "plan bodies have no peer-delivery
route at all": it is the `coordinator:executor`-subagent case, where a
dispatched executor's own findings need to land in a plan body it holds no
write access to.

Same `## Propagated` append-only contract, same guard-safe node-write seam,
same AC12 git-commit requirement (scoped to the single target file). The
two verbs differ only in (a) target root — `state/handoffs/` vs
`docs/plans/` — and (b) the status gate: a handoff must be `status:
claimed`/`consumed` (frozen) before this op will touch it, because an open
handoff can still be edited normally; a plan body carries no equivalent
concept — `block_subagent_plan_body_write` denies ANY
`coordinator:executor`-subagent write to it regardless of the plan's own
frontmatter `status:` or in-body `**Status:**` line — so `plan.propagate`
applies to any plan body carrying valid frontmatter, no additional gate.
See `_TARGET_SPECS` below and
docs/plans/2026-08-01-baton-spine-information-integrity.md § Part B (B2).

This does NOT repoint `block_subagent_plan_body_write`'s deny text to name
this verb — that guard's deny is identity-keyed and its blocked population
is BY CONSTRUCTION the `coordinator:executor` population the verb would
look like a sanctioned bypass to (Anti-scope, same plan). This module only
adds the second target root; the guard is untouched by this extension.

Negative-spec:
    - Does NOT gate on authorship — see Contract above.
    - Does NOT touch any path outside the single `state/handoffs/*.md`
      target — never `archive/handoffs/` (same `allowed_roots` shape as
      `handoff_correct_body`/`handoff_stamp`).
    - Does NOT accept a whole-file content payload or a free-form section
      replace — the mutation is always an APPEND under the canonical `##
      Propagated` heading (created once, never duplicated).
    - Does NOT touch the pickup index or `claimed_by` semantics — a
      `## Propagated` note is invisible to pickup by design (known
      limitation, carried forward deliberately; see the plan's Problem
      section). What closes the delivery question is precisely this op's
      own git-commit (AC12): a committed change is durable and reaches the
      holding session the next time it reads the file or pulls.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import tempfile
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

from coordinator_core.frontmatter.primitives import (
    read_fm_field_unquoted,
    split_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.session.core import SESSION_ENV_PRECEDENCE
from coordinator_core.win_portability import no_console_creationflags


_CREATIONFLAGS = no_console_creationflags()

_LOG = logging.getLogger(__name__)

# AC7 — the canonical, greppable section heading. Created once, appended to
# on every subsequent delivery (mirrors handoff_correct_body's own single-
# canonical-heading accumulation shape).
_PROPAGATED_SECTION_HEADING = "## Propagated"

# Reuses coordinator/skills/plan/SKILL.md's EXISTING amendment-note token
# family — no new schema, no new marker vocabulary. Only these two kinds are
# accepted; anything else is refused rather than silently coerced.
_ALLOWED_KINDS = ("Amended", "Superseded")

# Own net-growth bound (AC7) — sized for a REAL propagation note with
# file:line citations, not handoff_correct_body's 512-byte correction cap.
# The reported real-world note (2026-08-01 propagation-note-ruling memo) was
# ~6KB; 8192 bytes gives ~33% headroom over that observed case while staying
# far below "no cap" — a delivery is still a bounded note, not an unbounded
# progress journal.
_NET_GROWTH_CAP = 8192

_HEADING_LINE_RE = re.compile(r"^#{1,6} ", re.MULTILINE)
_DELIM_LINE_RE = re.compile(r"^---[ \t]*$", re.MULTILINE)


# ---------------------------------------------------------------------------
# B2 (AC8) — per-verb target spec. Same op, two guard-safe target roots (see
# module docstring "B2 EXTENSION" above for the full rationale). The two
# verbs differ only in target root and in whether a status gate applies.
# ---------------------------------------------------------------------------
class _TargetSpec(NamedTuple):
    path_param: str                       # e.g. "handoff_path" / "plan_path"
    allowed_root_parts: Tuple[str, ...]    # relative to the worktree root
    root_label: str                       # for error text
    escaped_label: str                    # what's excluded, for error text
    not_found_label: str                  # "handoff" / "plan"
    status_gate: bool                     # require status: claimed/consumed


_TARGET_SPECS = {
    "handoff.propagate": _TargetSpec(
        path_param="handoff_path",
        allowed_root_parts=("state", "handoffs"),
        root_label="state/handoffs/",
        escaped_label="archive/handoffs/, which this op never touches",
        not_found_label="handoff",
        status_gate=True,
    ),
    "plan.propagate": _TargetSpec(
        path_param="plan_path",
        allowed_root_parts=("docs", "plans"),
        root_label="docs/plans/",
        escaped_label=(
            "an archived plan (e.g. archive/specs/, archive/plans/), which "
            "this op never touches"
        ),
        not_found_label="plan",
        status_gate=False,
    ),
}

# Same invisible-character defense-in-depth handoff_correct_body carries
# (security-audit Finding 1, 2026-07-31) — a frozen/append-only audit record
# has no legitimate use for a zero-width or Unicode format character.
_ZERO_WIDTH_CHARS = frozenset({
    "​",  # ZERO WIDTH SPACE
    "‌",  # ZERO WIDTH NON-JOINER
    "‍",  # ZERO WIDTH JOINER
    "⁠",  # WORD JOINER
    "﻿",  # ZERO WIDTH NO-BREAK SPACE / BOM
})



def _contains_invisible_unicode(s: str) -> bool:
    """True if `s` contains a Unicode format character (category `Cf`) or a
    named zero-width character. Mirrors handoff_correct_body's own check —
    duplicated rather than imported (see that module's own comment on why
    ops in this family keep small, well-tested helpers local rather than
    coupling to a sibling op module)."""
    return any(
        unicodedata.category(ch) == "Cf" or ch in _ZERO_WIDTH_CHARS
        for ch in s
    )


def _err(msg: str) -> dict:
    _LOG.warning("handoff.propagate: %s", msg)
    return {"exit_code": 1, "applied": False, "error": msg}


# ---------------------------------------------------------------------------
# Session-id resolution (audit trail only — NEVER a gate; see module docstring)
# ---------------------------------------------------------------------------


def _resolve_session_id_with_source() -> "Tuple[Optional[str], Optional[str]]":
    """Same three-variable precedence chain handoff_correct_body.py uses
    (centralized as ``coordinator_core.session.core.SESSION_ENV_PRECEDENCE``
    — see that constant's docstring for why a fourth independent copy here
    was a break-class risk), for exactly the same reason: recorded in the
    delivery stamp so a reader knows WHICH session delivered the note and
    which var resolved it. Unlike handoff_correct_body, the resolved value
    here gates NOTHING — a propagation delivered by a session with no
    resolvable id still succeeds (there is no authorship equality to check
    against), it is simply stamped as delivered by an unknown session."""
    for var in SESSION_ENV_PRECEDENCE:
        val = os.environ.get(var)
        if val:
            return val, var
    return None, None


def _build_propagated_block(
    kind: str, slug: str, summary: str, note: str, session_id: Optional[str], session_source: Optional[str],
) -> str:
    """Compose one delivery's appended text (marker line + audit comment +
    blank line + note prose), never a new heading — the heading itself is
    added by `_append_propagated_section` only the first time."""
    ts = datetime.now(timezone.utc).isoformat()
    date = ts[:10]
    if session_id:
        audit = f"<!-- handoff-propagate: {ts} by session {session_id} (resolved via {session_source}) -->"
    else:
        audit = f"<!-- handoff-propagate: {ts} by session UNKNOWN (no calling session id resolvable) -->"
    marker = f"**{kind} {date} by {slug}:** {summary}"
    return f"{marker}\n{audit}\n\n{note}\n"


def _append_propagated_section(body: str, block: str) -> str:
    """Append `block` under the canonical `## Propagated` heading, creating
    the heading once and appending to it thereafter — never a new heading
    per delivery (AC7)."""
    if _PROPAGATED_SECTION_HEADING in body:
        if not body.endswith("\n"):
            body += "\n"
        return body + "\n" + block
    if not body.endswith("\n"):
        body += "\n"
    return body + "\n" + _PROPAGATED_SECTION_HEADING + "\n\n" + block


# ---------------------------------------------------------------------------
# AC12 — git preconditions + plumbing commit
# ---------------------------------------------------------------------------

def _run_git(
    args: List[str], cwd: Path, env: "Optional[Dict[str, str]]" = None,
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
        env=env,
        **_CREATIONFLAGS,
    )


def _target_is_dirty(worktree: Path, rel_path: str) -> bool:
    """AC12 precondition 1 — True if `rel_path` (repo-relative) carries any
    uncommitted change (staged or unstaged) in the holder's tree, checked
    BEFORE this op writes anything."""
    result = _run_git(["status", "--porcelain", "--", rel_path], worktree)
    if result.returncode != 0:
        # Fails closed: an unreadable git-status answer is treated as dirty
        # (refuse) rather than assumed clean.
        return True
    return bool(result.stdout.strip())


def _git_operation_in_progress(worktree: Path) -> Optional[str]:
    """AC12 precondition 2 — returns a human-readable reason string if a
    merge/rebase/cherry-pick is in progress or HEAD is detached, else None."""
    git_dir_result = _run_git(["rev-parse", "--git-dir"], worktree)
    if git_dir_result.returncode != 0:
        return "cannot resolve git-dir to check for an in-progress git operation"
    git_dir = Path(git_dir_result.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = worktree / git_dir

    if (git_dir / "MERGE_HEAD").exists():
        return "a merge is in progress (MERGE_HEAD present)"
    if (git_dir / "CHERRY_PICK_HEAD").exists():
        return "a cherry-pick is in progress (CHERRY_PICK_HEAD present)"
    if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        return "a rebase is in progress (rebase-merge/rebase-apply present)"

    symbolic_result = _run_git(["symbolic-ref", "-q", "HEAD"], worktree)
    if symbolic_result.returncode != 0:
        return "HEAD is detached"
    return None


def _commit_delivery(
    worktree: Path, rel_path: str, slug: str, summary: str,
) -> "Tuple[Optional[str], Optional[str]]":
    """Land the write as a commit scoped to exactly `rel_path`, via
    `commit-tree` + a compare-and-swap `update-ref` — plumbing that runs NO
    git hooks (see module docstring AC12 point 3). Returns (sha, error);
    exactly one is None.

    NEGATIVE-SPEC (why the tree is built in a PRIVATE index):
    - `git add -- <rel_path>` scopes what this op STAGES, but `git write-tree`
      serializes the WHOLE index — every path a concurrent peer already staged
      in the shared `.git/index` included. A path-scoped `add` followed by a
      shared-index `write-tree` is therefore NOT a scoped commit, and the
      "scoped to exactly `rel_path`" contract above was false whenever the
      holder's index was non-empty. Observed 2026-08-07: a delivery into one
      handoff landed 24 files, 23 of them a peer's staged work, under this
      op's own subject line.
    - The fix mirrors `ceremony/git_native.py::_commit_scoped_private_index`:
      redirect `GIT_INDEX_FILE` to a throwaway index seeded from HEAD (never a
      copy of the shared index), stage only `rel_path` there, and write the
      tree from that. The shared index is never read for content and never
      mutated, so a peer's staging is untouchable by this path by
      construction rather than by the caller happening to hold a clean index.
    - Seeded from HEAD via `read-tree`, never `shutil.copy2` of `.git/index`
      — copying the shared index would reintroduce exactly the contamination
      this closes.
    """
    old_head_result = _run_git(["rev-parse", "HEAD"], worktree)
    if old_head_result.returncode != 0:
        return None, f"cannot resolve HEAD: {old_head_result.stderr.strip()}"
    old_head = old_head_result.stdout.strip()

    temp_index = (
        Path(tempfile.gettempdir()) / f"git-index-propagate-{os.getpid()}-{uuid.uuid4().hex}"
    )
    private_env: Dict[str, str] = dict(os.environ)
    private_env["GIT_INDEX_FILE"] = str(temp_index)

    try:
        read_tree_result = _run_git(["read-tree", "HEAD"], worktree, env=private_env)
        if read_tree_result.returncode != 0:
            return None, f"git read-tree failed: {read_tree_result.stderr.strip()}"

        add_result = _run_git(["add", "--", rel_path], worktree, env=private_env)
        if add_result.returncode != 0:
            return None, f"git add failed: {add_result.stderr.strip()}"

        write_tree_result = _run_git(["write-tree"], worktree, env=private_env)
        if write_tree_result.returncode != 0:
            return None, f"git write-tree failed: {write_tree_result.stderr.strip()}"
        tree_sha = write_tree_result.stdout.strip()
    finally:
        try:
            temp_index.unlink()
        except OSError:
            pass

    subject = f"handoff.propagate: deliver into {rel_path}"
    message = (
        f"{subject}\n\n"
        f"Peer delivery via handoff.propagate — see the target file's own\n"
        f"'## Propagated' section for the full note.\n\n"
        f"Nature: peer-delivery\n"
        f"Delivered-By: {slug}\n"
    )

    commit_tree_result = subprocess.run(
        ["git", "commit-tree", tree_sha, "-p", old_head, "-F", "-"],
        cwd=str(worktree),
        input=message,
        capture_output=True,
        text=True,
        timeout=15,
        **_CREATIONFLAGS,
    )
    if commit_tree_result.returncode != 0:
        return None, f"git commit-tree failed: {commit_tree_result.stderr.strip()}"
    new_sha = commit_tree_result.stdout.strip()

    update_ref_result = _run_git(
        ["update-ref", "-m", subject, "HEAD", new_sha, old_head], worktree,
    )
    if update_ref_result.returncode != 0:
        return None, (
            f"compare-and-swap update-ref failed — HEAD moved concurrently "
            f"since {old_head} was captured: {update_ref_result.stderr.strip()}"
        )

    # The tree was built in a private index, so the SHARED index still holds
    # the pre-delivery blob for `rel_path` and would report it as staged-
    # modified against the new HEAD — a phantom-dirty entry for a file this
    # op just committed. Re-point that ONE path at the new HEAD.
    #
    # Safe precisely because `_target_is_dirty` already refused the whole op
    # if `rel_path` carried any staged or unstaged change on entry: there is
    # no holder-staged version of this path to destroy. Scoped to `rel_path`,
    # so every other entry a peer staged is left exactly as it was.
    sync_result = _run_git(["reset", "-q", "HEAD", "--", rel_path], worktree)
    if sync_result.returncode != 0:
        return new_sha, (
            f"delivery committed as {new_sha} but the shared index still "
            f"shows {rel_path} as modified (git reset failed): "
            f"{sync_result.stderr.strip()}"
        )
    return new_sha, None


def _rollback_delivery(
    worktree: Path, rel_path: str, original_text: str,
) -> Optional[str]:
    """Restore `rel_path` (repo-relative, under `worktree`) to `original_text`
    — undoes the `locked_rmw` mutation after a failed `_commit_delivery`.

    Review: code-reviewer — F2: `_commit_delivery` can fail at `write-tree`,
    `commit-tree`, or (most plausibly) a concurrent-HEAD CAS failure on
    `update-ref` — every one of those runs AFTER `locked_rmw` has already
    rewritten the file on disk, so a bare error return left the holder's tree
    dirty in violation of AC12. This is the compensating rollback.

    NEGATIVE-SPEC: does NOT `git reset -- rel_path`. It used to, to unstage
    what `_commit_delivery`'s `git add` had put in the SHARED index; that add
    now runs against a private `GIT_INDEX_FILE` (see `_commit_delivery`), so
    there is nothing of this op's in the shared index to undo. Resetting
    anyway would unstage content the HOLDER staged themselves before calling
    the op — turning a compensating rollback into peer-work destruction, the
    same class of harm the private-index change exists to close.

    Returns None on success, or a human-readable error string if rollback
    itself fails. A rollback failure must be surfaced loudly by the caller
    (folded into the returned error), never swallowed — a dirty tree the
    caller doesn't know about is worse than one it can report.
    """
    target = worktree / rel_path
    try:
        target.write_bytes(original_text.encode("utf-8"))
    except OSError as exc:
        return f"failed to restore {rel_path} to its pre-mutation content: {exc}"

    return None


@register_op("handoff.propagate")
async def _handler(
    params: dict,
    repo_root: Optional[Path] = None,
) -> dict:
    """JSON-RPC "handoff.propagate" handler.

    Appends a stamped, bounded delivery note into the `## Propagated` section
    of a `status: claimed` (or legacy `status: consumed`) `state/handoffs/*.md`
    file, with NO authorship gate, then git-commits the write scoped to that
    single file (AC12). See module docstring for the full contract.

    Params:
        handoff_path (str) — absolute or repo-relative path to the target
                              handoff file. Required. Must resolve under
                              `<worktree>/state/handoffs/` — never `archive/`.
        summary      (str) — one-line change description for the marker line.
                              Required, non-empty, not whitespace-only.
        note         (str) — the full propagation note prose (file:line
                              citations, landed-reality detail). Required,
                              non-empty, not whitespace-only.
        slug         (str) — identifies the delivering session/EM in the
                              marker line (e.g. "example-market-data-repo-em" or a
                              plan slug). Required, non-empty.
        kind         (str) — one of "Amended" / "Superseded" (default
                              "Amended") — the existing plan/SKILL.md
                              amendment-note token family.

    Returns a dict with keys:
        exit_code    (int)      — 0 ok / 1 refused (see `error`).
        applied      (bool)     — True only when the note was written AND the
                                   commit landed; False on every refusal path.
        session_id   (str|None) — resolved calling session id, audit-only.
        session_source (str|None)
        commit_sha   (str|None) — the new commit sha, on success (AC12).
        message      (str)      — human-readable outcome (exit_code 0 only).
        error        (str)      — human-readable refusal reason (exit_code 1
                                   only) — distinct per precondition.
    """
    return await _propagate(params, repo_root, op_name="handoff.propagate")


@register_op("plan.propagate")
async def _handler_plan(
    params: dict,
    repo_root: Optional[Path] = None,
) -> dict:
    """JSON-RPC "plan.propagate" handler (B2, AC8).

    Same contract as `handoff.propagate` (see module docstring "B2
    EXTENSION" section and `_handler` above), retargeted at a live
    `docs/plans/**.md` plan body instead of a `state/handoffs/*.md` baton.
    No status gate — a plan body carries no `claimed`/`consumed` equivalent;
    `block_subagent_plan_body_write` denies ANY `coordinator:executor`
    write to it regardless of the plan's own status.

    Params:
        plan_path (str) — absolute or repo-relative path to the target plan
                           file. Required. Must resolve under
                           `<worktree>/docs/plans/` — never `archive/`.
        summary, note, slug, kind — identical to `handoff.propagate`.

    Returns: identical shape to `handoff.propagate` (see `_handler` above).
    """
    return await _propagate(params, repo_root, op_name="plan.propagate")


async def _propagate(
    params: dict,
    repo_root: Optional[Path],
    *,
    op_name: str,
) -> dict:
    """Shared implementation for `handoff.propagate` / `plan.propagate` —
    see module docstring "B2 EXTENSION" and `_TARGET_SPECS` for the
    per-verb differences (target root, status gate)."""
    spec = _TARGET_SPECS[op_name]

    target_path_raw: str = params.get(spec.path_param) or ""
    summary_raw = params.get("summary")
    note_raw = params.get("note")
    slug_raw = params.get("slug")
    kind = params.get("kind") or "Amended"

    if not target_path_raw:
        return _err(f"missing required param: {spec.path_param}")
    if not isinstance(summary_raw, str) or not summary_raw.strip():
        return _err("missing or empty required param: summary")
    if not isinstance(note_raw, str) or not note_raw.strip():
        return _err("missing or empty required param: note")
    if not isinstance(slug_raw, str) or not slug_raw.strip():
        return _err("missing or empty required param: slug")
    if kind not in _ALLOWED_KINDS:
        return _err(f"kind must be one of {_ALLOWED_KINDS!r}, got {kind!r}")

    summary: str = summary_raw
    note: str = note_raw
    slug: str = slug_raw

    if repo_root is None:
        return _err(
            f"{op_name}: repo_root is required "
            "(no founding root available — handler called without socket-authoritative common_dir)"
        )

    worktree = main_worktree_root(repo_root)

    p = Path(target_path_raw)
    if not p.is_absolute():
        p = worktree / p

    allowed_roots = [Path(worktree, *spec.allowed_root_parts)]
    try:
        p = contained_path(p, allowed_roots)
    except ValueError as exc:
        return _err(
            f"{spec.path_param} is malformed (cannot be resolved as a "
            f"filesystem path): {exc}"
        )
    if p is None:
        return _err(
            f"{spec.path_param} escapes {spec.root_label} (or targets "
            f"{spec.escaped_label}): {target_path_raw!r}"
        )
    if not p.is_file():
        return _err(f"{spec.not_found_label} not found on disk: {target_path_raw}")

    # Review: code-reviewer — F3: `slug` gets the SAME three content checks as
    # `summary`/`note`, plus an explicit no-embedded-newline check. `slug` is
    # interpolated into the delivery commit message as `Delivered-By: {slug}`
    # (see `_commit_delivery`) — an embedded newline there can inject an
    # arbitrary extra "trailer"-shaped line that a downstream trailer consumer
    # (rollup_derive, coverage.py, this very producer contract) could misread
    # as real commit metadata for that commit.
    if _contains_invisible_unicode(summary) or _contains_invisible_unicode(note) or _contains_invisible_unicode(slug):
        return _err(
            "summary, note, or slug contains invisible or Unicode format characters "
            "(zero-width space/joiner, BOM, or category Cf) — refusing to "
            f"write invisible content into a {spec.not_found_label} body"
        )
    if _DELIM_LINE_RE.search(summary) or _DELIM_LINE_RE.search(note) or _DELIM_LINE_RE.search(slug):
        return _err(
            "summary, note, or slug contains a frontmatter-delimiter-shaped line "
            "('---') — refusing content that could be mistaken for frontmatter structure"
        )
    if _HEADING_LINE_RE.search(summary) or _HEADING_LINE_RE.search(note) or _HEADING_LINE_RE.search(slug):
        return _err(
            "summary, note, or slug contains a heading line ('#' through '######') — "
            "a propagation note is prose delivered under the existing "
            "'## Propagated' heading, it does not introduce its own document structure"
        )
    if "\n" in slug:
        return _err(
            "slug contains an embedded newline — slug is interpolated into the "
            "delivery commit message as 'Delivered-By: {slug}'; a newline there "
            "could inject a spoofed trailer-shaped line that downstream trailer "
            "consumers would misread as real commit metadata"
        )

    net_growth = len(summary.encode("utf-8")) + len(note.encode("utf-8"))
    if net_growth > _NET_GROWTH_CAP:
        return _err(
            f"summary+note is {net_growth} bytes, exceeding the "
            f"{_NET_GROWTH_CAP}-byte net-growth cap — a propagation delivers a "
            "bounded note, not an unbounded progress journal; split the "
            "delivery into smaller calls"
        )

    try:
        text = p.read_bytes().decode("utf-8")
    except OSError as exc:
        return _err(f"cannot read {spec.not_found_label} file: {exc}")

    split = split_frontmatter(text)
    if split is None:
        return _err(f"no valid YAML frontmatter block in: {target_path_raw}")

    if spec.status_gate:
        status = read_fm_field_unquoted(split.fm_text, "status")
        if status not in ("claimed", "consumed"):
            return _err(
                f"{op_name} only applies to status:claimed (or legacy "
                f"status:consumed) handoffs — {target_path_raw} carries status "
                f"{status!r}; an open baton needs no peer-delivery op, edit it normally"
            )

    # AC12 precondition 1 — abort if the target is dirty BEFORE any mutation.
    try:
        rel_path = str(p.relative_to(worktree))
    except ValueError:
        return _err(f"{spec.path_param} resolved outside the worktree: {target_path_raw!r}")
    if _target_is_dirty(worktree, rel_path):
        return _err(
            f"{target_path_raw} has uncommitted changes in the holder's tree — "
            "refusing to deliver into a dirty target; a scoped delivery commit "
            "would otherwise sweep those uncommitted changes into this delivery "
            "under the delivering session's authorship"
        )

    # AC12 precondition 2 — abort mid-rebase/merge/cherry-pick or detached HEAD.
    in_progress_reason = _git_operation_in_progress(worktree)
    if in_progress_reason is not None:
        return _err(
            f"refusing to deliver: {in_progress_reason} — a commit issued now "
            "would either error opaquely, advance the holder's own git "
            "operation, or (if HEAD is detached) orphan the delivery"
        )

    session_id, session_source = _resolve_session_id_with_source()
    block = _build_propagated_block(kind, slug, summary, note, session_id, session_source)

    # Review: code-reviewer — F2: captured so a failed `_commit_delivery` can
    # restore the working-tree file to exactly what `locked_rmw` read, rather
    # than leaving the mutated-but-uncommitted (and possibly staged) content
    # behind — AC12's "never leaves a dirty tree" guarantee applies to a
    # failed delivery too, not only the happy path.
    pre_mutation_text: List[Optional[str]] = [None]

    def _mutate(old_text: str) -> str:
        pre_mutation_text[0] = old_text
        split_inner = split_frontmatter(old_text)
        if split_inner is None:
            raise MutateAbort(f"no valid YAML frontmatter block in: {target_path_raw}")
        fm_before = split_inner.fm_text
        inner_body = split_inner.body_with_leading_newline
        new_body = _append_propagated_section(inner_body, block)
        # Rebuild directly (frontmatter/primitives.py's `rebuild()` always
        # reuses `split.body_with_leading_newline` verbatim and has no
        # body-override parameter, so it cannot be reused as-is here) —
        # same preamble + delimiter shape `rebuild()` itself produces.
        fm_normalized = fm_before if fm_before.endswith("\n") else fm_before + "\n"
        rebuilt = (split_inner.preamble or "") + "---\n" + fm_normalized + "---" + new_body
        # Byte-identical frontmatter assertion, re-derived post-rebuild.
        final_split = split_frontmatter(rebuilt)
        if final_split is None or final_split.fm_text != fm_before:
            raise MutateAbort(
                f"{target_path_raw}: post-mutation frontmatter is not "
                "identical to pre-mutation frontmatter — refusing to write"
            )
        return rebuilt

    try:
        await asyncio.to_thread(locked_rmw, p, _mutate, repo_root=repo_root)
    except LockTimeout as exc:
        return _err(f"lock timeout acquiring file lock: {exc}")
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "mutate aborted")
    except OSError as exc:
        return _err(f"cannot read/write {spec.not_found_label} file: {exc}")

    sha, commit_err = await asyncio.to_thread(_commit_delivery, worktree, rel_path, slug, summary)
    if commit_err is not None:
        # Review: code-reviewer — F2: roll back the working-tree write on any
        # commit failure so the holder's tree is never left dirty (AC12).
        assert pre_mutation_text[0] is not None  # _mutate always ran before this point
        rollback_err = await asyncio.to_thread(
            _rollback_delivery, worktree, rel_path, pre_mutation_text[0],
        )
        if rollback_err is not None:
            return _err(
                f"note written to disk but the delivery commit failed: {commit_err}; "
                f"ADDITIONALLY, rollback failed: {rollback_err} — {target_path_raw} "
                "may be left in a dirty/modified state, manual cleanup required"
            )
        return _err(
            f"delivery commit failed and was rolled back — {target_path_raw} was "
            f"restored to its pre-delivery content, the holder's tree is clean: {commit_err}"
        )

    _LOG.info(
        "%s: delivered into %s (session %s via %s), commit %s",
        op_name, p, session_id, session_source, sha,
    )
    return {
        "exit_code": 0,
        "applied": True,
        "session_id": session_id,
        "session_source": session_source,
        "commit_sha": sha,
        "message": f"delivered propagation note into {target_path_raw}, commit {sha}",
    }
