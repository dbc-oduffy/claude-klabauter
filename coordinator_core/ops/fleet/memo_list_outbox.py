"""
coordinator_core.ops.fleet.memo_list_outbox — memo.list_outbox COMPUTE_ONLY UDS op handler.

Purpose: Enumerate the CALLING repo's own `state/memo-outbox/*.md` draft
memos — the claude-klabauter-native port of the DoE `cross-repo-memo` CLI's `list`
verb, which today owns this enumeration logic locally. Moving it into the
engine lets the CLI verb become pure invocation (dispatch + format), matching
the DR-210 Option-A boundary move already applied to memo.draft/memo.compose/
memo.list (receiver-enumeration). This is a DISTINCT op from `memo.list`
(registered receivers, machine-local-registry-sourced) — different data
source, different directory, different purpose; the two ops do not share a
handler or a candidate shape.

Registered as "memo.list_outbox" via @register_op; COMPUTE_ONLY classification
and the `common_dir` `_OP_KEY_SCOPE` entry are wired alongside this file.

Spec backlink:
    docs/plans/2026-07-21-memo-tool-rebuild-full-ownership.md (DR-210 Option-A
    full-ownership move; this op closes the remaining CLI-local `list` verb).
    Parity source: DoE coordinator/bin/cross-repo-memo.py `_cmd_list`
    ("Enumerates state/memo-outbox/*.md in the sender repo").
    Outbox write path (must match exactly): coordinator_core/ops/fleet/memo_draft.py
    `outbox_dir(caller_worktree)` -- `.coordinator-local/memo-outbox/`, with
    `legacy_outbox_dir(caller_worktree)` (`state/memo-outbox/`, retired
    2026-09-03) read as a fallback so a draft staged before the repoint is
    still enumerated.
    Frontmatter parsing: coordinator_core.frontmatter.primitives
    (split_frontmatter / read_fm_field — same primitives memo_compose.py uses,
    not a hand-rolled parser).

Negative-spec:
  - Does NOT touch `memo.list`'s data source (the machine-local receiver
    registry) — this op reads ONLY the calling repo's own
    state/memo-outbox/ directory tree. `memo.list` and `memo.list_outbox`
    are structurally parallel siblings (both COMPUTE_ONLY, both
    build_dry_run_result-only), never the same op nor a shared code path.
  - Does NOT write any file, create any directory, or run any git command —
    provably side-effect-free, same posture as memo_list.py/memo_check_addressee.py.
  - Does NOT grow a fleet-wide memo index or cache — every call re-reads the
    outbox directory fresh from disk (Q-d store-less-ness invariant, same as
    memo_list.py's own enumeration mode).
  - Does NOT hand-roll YAML frontmatter parsing — reuses
    `coordinator_core.frontmatter.primitives.split_frontmatter`/`read_fm_field`,
    the same primitives memo_compose.py uses to read an existing draft.
  - Does NOT error on a missing or empty outbox directory — returns an empty
    candidate list cleanly (a repo that has never drafted a memo has no
    outbox dir at all; that is not a failure state).
  - Does NOT accept `dry_run: false` — memo.list_outbox has no act mode; a
    caller that passes `dry_run: false` gets a setup-error envelope, matching
    memo.list's/memo.check_addressee's posture.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import (
    build_dry_run_result,
    build_setup_error_result,
    main_worktree_root,
)
from coordinator_core.ops.fleet.memo_draft import merged_outbox_drafts

_LOG = logging.getLogger(__name__)

# Mode constant for the envelope mode field — memo.list_outbox is a
# single-mode op (no `to`/resolution-mode split like memo.list); it always
# just enumerates.
_MODE = "list_outbox"

# Frontmatter fields surfaced per candidate — the useful subset a sender
# would want to see when deciding what to compose/send next. Mirrors what
# memo_draft.compose_draft_frontmatter actually writes (title, from, to,
# created, status, summary, kind) minus delivery_mode/scoped_to, which are
# internal wiring rather than sender-facing triage fields.
_SURFACED_FIELDS = ("title", "from", "to", "created", "status", "summary", "kind")


def _validate_list_outbox_params(params: dict):
    """Validate memo.list_outbox params; return dry_run (bool) or a setup-error dict.

    Required: dry_run (bool) — must be True; memo.list_outbox has no act mode
    (it only ever enumerates, never writes).
    """
    dry_run = params.get("dry_run")
    if not isinstance(dry_run, bool):
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.list_outbox: dry_run must be bool, got " + repr(type(dry_run).__name__),
        )
    if dry_run is False:
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.list_outbox: dry_run must be true — memo.list_outbox is a pure "
            "read op with no act mode (it never writes, regardless of this flag).",
        )
    return dry_run


def _candidate_for_draft(draft_path: Path) -> dict:
    """Build one candidate dict for a single outbox draft file.

    Read-only: reads the file's raw text and parses its YAML frontmatter via
    the shared `frontmatter.primitives` helpers (same primitives
    memo_compose.py uses) — no hand-rolled parsing. `topic` is derived from
    the filename stem (memo_draft.py writes `<topic>.md`, topic never lives
    in frontmatter — same convention documented in
    memo_draft.compose_draft_frontmatter's own docstring).

    Unreadable or unparseable files degrade gracefully: the candidate still
    appears (filename/topic always populated), with `None` for every
    surfaced frontmatter field and a `note` explaining why — an outbox
    listing must never silently drop a draft just because it is malformed;
    that's exactly the kind of file a sender needs to see and fix.
    """
    topic = draft_path.stem
    candidate = {
        "id": str(draft_path),
        "filename": draft_path.name,
        "topic": topic,
        "path": str(draft_path),
        "note": None,
    }
    for field in _SURFACED_FIELDS:
        candidate[field] = None

    try:
        text = draft_path.read_text(encoding="utf-8")
    except OSError as exc:
        candidate["note"] = f"unreadable: {exc}"
        return candidate

    split = split_frontmatter(text)
    if split is None:
        candidate["note"] = "unparseable: no valid YAML frontmatter block found"
        return candidate

    for field in _SURFACED_FIELDS:
        candidate[field] = read_fm_field(split.fm_text, field)

    return candidate


def _enumerate_outbox_candidates(worktree_root: Path) -> list:
    """Build one candidate dict per `*.md` file in the calling repo's outbox,
    merged across the new `.coordinator-local/memo-outbox/` root and the
    retired `state/memo-outbox/` root (2026-09-03 relocation:
    `machinery_paths.legacy_memo_outbox_dir`'s own docstring).

    Sorted by filename for deterministic, stable output across calls.

    # Review: overengineering-reviewer (Kira) — merge logic moved to the
    # shared `memo_draft.merged_outbox_drafts` (was duplicated verbatim here
    # and in memo_reconcile_outbox._reconcile).
    """
    return [_candidate_for_draft(p) for p in merged_outbox_drafts(worktree_root)]


@register_op("memo.list_outbox")
def _memo_list_outbox(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'memo.list_outbox' COMPUTE_ONLY UDS op handler.

    Enumerate every draft memo in the CALLING repo's own
    state/memo-outbox/*.md — a store-less, no-write directory read, never a
    write/commit/network call. Distinct data source and directory from
    memo.list (registered receivers, machine-local-registry-sourced); the
    two ops are structural siblings, not variants of the same op.

    repo_root arg: git common dir from _OP_KEY_SCOPE = "common_dir" (wired
    alongside this file, mirroring memo.draft/memo.compose/memo.check_addressee
    — this op needs the caller's own worktree to know which outbox to read,
    unlike memo.list's "none" scoping).

    Params:
        dry_run (bool, required): must be True — memo.list_outbox has no act
                                   mode; it only ever enumerates.

    Returns:
        The `build_dry_run_result` envelope (`exit_code:0, dry_run:true`)
        with one candidate per `*.md` file in state/memo-outbox/ (empty list
        when the directory is absent or empty — not an error); or a
        `build_setup_error_result` envelope (`exit_code:1`) on bad params or
        a missing repo_root.
    """
    validated = _validate_list_outbox_params(params)
    if isinstance(validated, dict):
        return validated  # exit_code:1 setup-error envelope

    dry_run = validated

    if repo_root is None:
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.list_outbox: no repo_root supplied — memo.list_outbox enumerates "
            "the CALLING repo's own state/memo-outbox/ and requires a resolved "
            "worktree (common_dir-keyed op).",
        )
    caller_worktree = main_worktree_root(Path(repo_root))

    candidates = _enumerate_outbox_candidates(caller_worktree)

    return build_dry_run_result(_MODE, candidates)
