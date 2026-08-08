"""
coordinator_core.ops.handoff_author_fork — JSON-RPC "handoff.author_fork" operation.

Purpose: MUTATING engine primitive that generates a fork/spinoff handoff artifact with
provenance auto-populated at spawn time — the only moment ``origin_session`` /
``origin_handoff`` / ``origin_plan_id`` / ``origin_goal_id`` are cheaply knowable (an
hour later you are backfilling null).

This is the primitive the (example-doctrine-repo-owned) ``/spinoff`` skill invokes.  It does NOT enforce
the PM spinoff gate (that responsibility stays skill-layer); it is a MUTATING primitive
reachable over the UDS whose caller is responsible for any gate-checking.

Auto-fill from live session state
----------------------------------
- ``origin_session``  — resolved via ``session_context.resolve_current_session_id`` (the
  canonical CLAUDE_SESSION_ID → CLAUDE_CODE_SESSION_ID chain; the former
  ``.current-session-id`` sentinel tier was removed KS-2 2026-08-07 — see
  ``session_context.py`` for the unsound-under-concurrency + deleted-writer rationale).
  On resolution failure this is ``None`` and the fork lands with an honest null-stamped
  ``origin_session`` (and consequently ``origin_handoff``, since
  ``_resolve_origin_handoff`` cannot match a claim against a null id) rather than a
  falsely-populated dead-sentinel value or a crash.
- ``origin_handoff``  — the ``state/handoffs/*.md`` file whose ``claimed_by`` (or legacy
  ``consumed_by``) frontmatter equals the resolved session id; ``null`` when none matches
  (graceful-absent).
- ``origin_handoff_id`` — C2 ID-companion for ``origin_handoff`` (add-not-swap): the
  originating baton's own ``handoff_id`` frontmatter scalar, read from the SAME file
  ``origin_handoff`` names (never a different candidate).  Path-independent — survives
  the originating baton's later archival/rename to ``archive/handoffs/YYYY-MM/``, unlike
  ``origin_handoff`` which is a path.  ``null`` when ``origin_handoff`` is null or the
  origin baton predates the ``handoff_id`` field (no backfill — schema's stated policy).

Ambiguity handling
------------------
``origin_plan_id`` (scalar) and ``origin_goal_id`` (list) may be supplied by the caller
or omitted for auto-resolution. Resolution is SCORE-load-bearing
(``coordinator_core.ops.match_core.resolve_candidate``), not candidate-COUNT-bearing —
the raw arity of the scanned directory says nothing about whether any candidate actually
matches ``match_text``:
- Absent param + zero candidates                → resolve to ``null`` (graceful-absent).
- Absent param + top candidate clears the auto-resolve floor AND leads the runner-up by
  a clear gap → auto-resolve (a LONE candidate that fails the floor is NOT auto-resolved
  just for being alone).
- Absent param + no candidate clears the floor, or the top two are too close to call →
  do NOT write; return ``{"status": "needs_disambiguation", "candidates": {...}}`` with
  the ranked candidates from the respective ``match_candidates`` op so the caller can
  re-invoke with a pinned id.

PROVENANCE_FIELD_MAP
---------------------
Module-level dict mapping logical names → ratified frontmatter key names, cardinality,
and serialisation type.  Field names and cardinalities are ratified per the
spinoff-provenance-ancestry contract (example-doctrine-repo C6 ratification memo
``cross-repo/inbox/2026-07-07-spinoff-provenance-claude-klabauter-ratified.md``).  No key swap
needed — the current ``origin_*`` names match the ratified shape exactly.

Write mechanics
---------------
New fork handoff file created under ``{worktree}/state/handoffs/`` via ``locked_rmw`` with
``missing_ok=True`` (atomic mkstemp + os.replace under flock).  Frontmatter populated via
``coordinator_core.frontmatter.primitives`` (no reimplemented frontmatter I/O).
After the provenance fields are written, ``handoff_normalize._normalize_one_text`` is
composed inline to fill in ``category``, ``summary``, ``deliverable_id``, ``initiative``.
``predecessor: none`` — fork handoffs are not a continuation.

MUTATING op: writes ONLY ``state/handoffs/`` in the caller's worktree.
No git commit from the handler.
Blocking FS I/O wrapped in ``asyncio.to_thread``.

``_OP_KEY_SCOPE: common_dir`` — handler receives ``git_common_dir(caller_worktree)`` via
ipc.py; derives worktree via ``main_worktree_root(repo_root)`` before any path construction.

Spec backlink: docs/plans/2026-07-07-claude-klabauter-fork-provenance-creation-path-tooling.md § C3
DR authority:  docs/decisions/DR-208-invoke-op-authz-model.md § 5

Negative-spec:
    - Does NOT enforce the PM spinoff gate — this is a primitive; gate-enforcement is the
      calling skill's responsibility (``/spinoff`` SKILL.md, example-doctrine-repo-owned).
    - Does NOT git-commit.  Pure new-file creation only.
    - Does NOT write outside ``state/handoffs/`` in the caller's worktree.
    - Does NOT use ``liveness.resolve_live_session_ids()`` to resolve origin_session —
      that returns a FrozenSet of ALL live sessions (liveness probe), not the current
      session identity.  See ``session_context.py`` for the correct chain.
    - Does NOT reimplement frontmatter parse/write — uses frontmatter.primitives and
      composes handoff_normalize._normalize_one_text.
    - Does NOT overwrite an existing handoff file — the generated filename is
      timestamp-keyed + uuid-suffixed; a collision triggers MutateAbort.
    - Does NOT surface plan and goal ambiguity simultaneously — plan resolved first;
      goal disambiguation only after plan is pinned.  Review: code-reviewer (F5).
    - Does NOT silently write a fork with null origin_handoff/origin_handoff_id when
      state/handoffs/ cannot be enumerated (permission-denied or similar) — an
      unreadable handoffs_dir is provenance-critical, so `_resolve_origin_handoff`
      raises OSError and `_handler` surfaces an explicit error reply instead.
"""

from __future__ import annotations
import sys

import asyncio
import datetime
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from coordinator_core.frontmatter.primitives import (
    _is_nested_block_key,
    insert_fm_field,
    read_fm_field,
    read_fm_field_unquoted,
    rebuild,
    replace_fm_field,
    replace_fm_field_raw,
    serialize_yaml_scalar,
    split_frontmatter,
)
from coordinator_core.claim_state import resolve_claim_state
from coordinator_core.handoff_creation_guard import (
    HandoffArchivedTwinError,
    assert_no_archived_twin,
)
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._fm_util import extract_frontmatter_scalar
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.goals_match import _collect_goals
from coordinator_core.ops.handoff_normalize import (
    _NO_FRONTMATTER,
    _normalize_one_text,
    _resolve_claimed_plan_deliverable_id,
)
from coordinator_core.ops.match_core import (
    AUTO_RESOLVE_MIN_GAP,
    AUTO_RESOLVE_MIN_SCORE,
    ResolutionReason,
    resolve_candidate,
)
from coordinator_core.ops.plan_match import _collect_plans
from coordinator_core.ops.session_context import resolve_current_session_id

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PROVENANCE_FIELD_MAP
#
# RATIFIED field names and cardinalities — spinoff-provenance-ancestry contract
# (example-doctrine-repo C6 ratification memo: cross-repo/inbox/2026-07-07-spinoff-provenance-claude-klabauter-ratified.md).
# The current ``origin_*`` keys already match the ratified shape — that part needed no
# rename.  It DID leave a gap, though: ``origin_handoff_id``, the C2 ID-companion for
# ``origin_handoff`` (example-doctrine-repo schema, ``docs/plans/2026-07-08-lifecycle-vocab-c2-durable-links-rollup.md``
# § C2), is included below because this op is the schema-designated author-time stamp
# point — ``origin_handoff`` is a path and paths are mutable (every baton is eventually
# archived to ``archive/handoffs/YYYY-MM/``); ``origin_handoff_id`` is the path-independent
# companion that survives that archival/rename.
#
#   origin_goal_id     → ARRAY   (string[] | null) — multi-goal forks are real (cockpit-flagged)
#   origin_session     → scalar  (string | null)
#   origin_handoff     → scalar  (string | null)
#   origin_handoff_id  → scalar  (string | null) — C2 ID-companion for origin_handoff
#   origin_plan_id     → scalar  (string | null)
#
# Consumers of this map: _build_provenance_fm (this module), test assertions.
# ---------------------------------------------------------------------------
PROVENANCE_FIELD_MAP: Dict[str, Dict] = {
    "origin_session":    {"key": "origin_session",    "cardinality": "scalar"},
    "origin_handoff":    {"key": "origin_handoff",    "cardinality": "scalar"},
    "origin_handoff_id": {"key": "origin_handoff_id", "cardinality": "scalar"},
    "origin_plan_id":    {"key": "origin_plan_id",    "cardinality": "scalar"},
    "origin_goal_id":    {"key": "origin_goal_id",    "cardinality": "array"},
}

# Regex for valid workstream slug chars (mirrors review_trail_write.py).
_WORKSTREAM_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]")


def _resolution_reason_text(reason: str, ranked_count: int) -> str:
    """Render a ``ResolutionReason`` value into the human-readable prefix of a
    ``degraded``/``needs_disambiguation`` reason string — the wording is
    per-case rather than a single "ambiguous match (N candidates)" that
    claims a judgment call happened when arithmetic (candidate count alone)
    decided instead.

    - ``BELOW_THRESHOLD`` — nothing scored well enough (uninformative query,
      or nothing in the directory actually matches).
    - ``TOO_CLOSE`` — a genuine tie between two-or-more plausible matches.

    ``NO_CANDIDATES`` is intentionally NOT handled here — that case resolves
    to null silently (mirrors the historical zero-candidates behaviour) and
    is never reported as degraded/ambiguous; callers must not call this
    helper for that reason.
    """
    if reason == ResolutionReason.BELOW_THRESHOLD:
        return (
            f"below-threshold: no candidate scored high enough to auto-resolve "
            f"({ranked_count} candidate(s) ranked, top score below "
            f"min_score={AUTO_RESOLVE_MIN_SCORE})"
        )
    if reason == ResolutionReason.TOO_CLOSE:
        return (
            f"too-close: top two candidates are too close to call "
            f"({ranked_count} candidates ranked, gap below min_gap={AUTO_RESOLVE_MIN_GAP})"
        )
    # Defensive fallback — should be unreachable given ResolutionReason's closed set.
    return f"unresolved ({reason}; {ranked_count} candidate(s) ranked)"


# ---------------------------------------------------------------------------
# Frontmatter helpers
# ---------------------------------------------------------------------------


def _serialize_yaml_inline_array(values: Optional[List[str]]) -> str:
    """Produce a YAML inline-sequence string for an array field.

    Uses ``serialize_yaml_scalar`` for each element so quoting rules are consistent
    with the existing frontmatter primitives.

    Returns:
        ``"null"`` when values is None.
        ``"[]"``   when values is an empty list.
        ``"[id1, id2]"`` for a non-empty list (elements quoted only when needed).
    """
    if values is None:
        return "null"
    if not values:
        return "[]"
    return "[" + ", ".join(serialize_yaml_scalar(v) for v in values) + "]"


def _append_fm_array_field(fm_text: str, key: str, values: Optional[List[str]]) -> str:
    """Append a YAML inline-sequence field to frontmatter text (append-only variant).

    Mirrors ``insert_fm_field(fm, key, v, after_key=None)`` but for array values that
    ``serialize_yaml_scalar`` cannot express (it handles scalars only).

    Trim trailing whitespace then append ``{key}: {inline_seq}\\n``.
    """
    yaml_val = _serialize_yaml_inline_array(values)
    trimmed = fm_text.rstrip()
    return trimmed + "\n" + f"{key}: {yaml_val}" + "\n"


def _build_fork_frontmatter(
    title: str,
    created_date: str,
    branch: str,
    kind: str,
    workstream: Optional[str],
    provenance: Dict[str, object],
    body: str,
) -> str:
    """Build the initial frontmatter string for a fork handoff.

    Generates a minimal well-formed frontmatter block with the required fields and
    all provenance fields from PROVENANCE_FIELD_MAP.  The caller then passes this
    through ``_normalize_one_text`` to fill in category, summary, deliverable_id,
    initiative.

    ``provenance`` keys mirror PROVENANCE_FIELD_MAP logical names:
        origin_session (str | None)
        origin_handoff (str | None)
        origin_handoff_id (str | None)
        origin_plan_id (str | None)
        origin_goal_id (list[str] | None)
    """
    fm = f"title: {serialize_yaml_scalar(title)}\n"
    fm += f"created: {created_date}\n"
    fm += f"branch: {serialize_yaml_scalar(branch)}\n"
    fm += "status: open\n"
    fm += "predecessor: none\n"
    fm += f"kind: {serialize_yaml_scalar(kind)}\n"
    if workstream is not None:
        fm += f"workstream: {serialize_yaml_scalar(workstream)}\n"

    # Provenance fields via PROVENANCE_FIELD_MAP (keys ratified per spinoff-provenance-ancestry
    # contract; origin_handoff_id is the C2 ID-companion added on top of that ratified set —
    # see the PROVENANCE_FIELD_MAP comment block above for why).
    # Review: code-reviewer — stale "PLACEHOLDER keys — Wave 2 swaps" contradicted ratification at module docstring; updated.
    for logical_name, meta in PROVENANCE_FIELD_MAP.items():
        key = meta["key"]
        cardinality = meta["cardinality"]
        value = provenance.get(logical_name)
        if cardinality == "array":
            fm = _append_fm_array_field(fm, key, value)  # type: ignore[arg-type]
        else:
            fm = insert_fm_field(fm, key, value)

    # Wrap in --- fences.
    full_text = "---\n" + fm.rstrip("\n") + "\n---\n"
    if body:
        full_text += "\n" + body.lstrip("\n")
    return full_text


def _stamp_fork_provenance(fm_text: str, provenance: Dict[str, object]) -> str:
    """Stamp the five ``PROVENANCE_FIELD_MAP`` fields onto a frontmatter TEXT
    block IN PLACE — the stamping counterpart to ``_build_fork_frontmatter``'s
    from-scratch build.

    Replaces an existing key's value line-for-line when the key is already
    present (idempotent re-stamp — never duplicates the key); appends a fresh
    line when absent. The spinoff docgen template
    (``ops/docgen/templates/spinoff.json``) does not scaffold any of these
    five keys at all, so the common case is append — the replace branch exists
    so re-stamping the same file twice (e.g. a retried directive) does not
    leave two ``origin_plan_id:`` lines.

    Every other key, key order, and the body are left untouched.
    """
    for logical_name, meta in PROVENANCE_FIELD_MAP.items():
        key = meta["key"]
        cardinality = meta["cardinality"]
        value = provenance.get(logical_name)
        if cardinality == "array":
            serialized = _serialize_yaml_inline_array(value)  # type: ignore[arg-type]
            current = read_fm_field(fm_text, key)
            if current is not None:
                # Same two guards replace_fm_field applies to every scalar
                # field in this function — this branch can't route through
                # replace_fm_field itself (it needs the pre-serialized inline
                # `[...]` text, not replace_fm_field's serialize_yaml_scalar
                # output), so the guards are reapplied explicitly here rather
                # than skipped. Review: code-reviewer (P1) — array-replace
                # branch bypassed both guards entirely.
                if current.startswith('>') or current.startswith('|'):
                    truncated = current[:40] + '...' if len(current) > 40 else current
                    raise ValueError(
                        f'_stamp_fork_provenance: field "{key}" uses a block-scalar '
                        f'YAML value ("{truncated}") — cannot safely replace single-line. '
                        f'Fix the frontmatter manually.'
                    )
                if _is_nested_block_key(fm_text, key):
                    raise ValueError(
                        f'_stamp_fork_provenance: field "{key}" holds a nested YAML '
                        f'block (sequence-of-mappings) — mutating only the key line '
                        f'would silently orphan its indented continuation lines.'
                    )
                # Routes through the shared primitive rather than re-forking its
                # regex: the hand-copy that used to live here carried the
                # pre-2026-07-28 `(?=[ \t]|$)\s*` shape, whose `\s*` crossed the
                # line break of a present-but-empty `origin_goal_id:` and
                # overwrote the FOLLOWING line. Only the raw (pre-serialized
                # inline-array) entry point is needed here; the two guards above
                # are replace_fm_field's, reapplied locally for their
                # domain-specific messages.
                fm_text = replace_fm_field_raw(fm_text, key, serialized)
            else:
                fm_text = _append_fm_array_field(fm_text, key, value)  # type: ignore[arg-type]
        else:
            if read_fm_field(fm_text, key) is not None:
                fm_text = replace_fm_field(fm_text, key, value)
            else:
                fm_text = insert_fm_field(fm_text, key, value)
    return fm_text


# ---------------------------------------------------------------------------
# Active-handoff resolution — find origin_handoff via claimed_by (consumed_by fallback)
# ---------------------------------------------------------------------------


def _resolve_origin_handoff(
    handoffs_dir: Path,
    session_id: Optional[str],
    *,
    repo_root: Optional[Path] = None,
) -> "tuple[Optional[str], Optional[str]]":
    """Find the handoff in ``state/handoffs/*.md`` whose LEDGER-FIRST claim
    holder (``coordinator_core.claim_state.resolve_claim_state``) == session_id.

    C6a (ledger-first authoritative read): a desynced baton — claimed on the
    branch-independent claim ledger but reverted on the tracked-frontmatter
    mirror by a branch switch (the incident ``claim_state.py``'s module
    docstring names) — used to resolve to no match here, silently authoring
    the fork with ``origin_handoff`` null: permanent, invisible provenance
    loss. Routing through the single ledger-first accessor instead of a raw
    mirror-only ``claimed_by``/``consumed_by`` read means this site now finds
    the origin baton even when only the ledger still holds the claim. This
    function's own search semantics (non-recursive ``iterdir()`` over
    ``state/handoffs/`` only, first match wins) are unchanged; only the claim
    read is delegated. A dead ledger holder degrades to the mirror (or
    "none") per ``resolve_claim_state``'s own contract — never surfaced as
    ``source == "ledger"`` for a dead session.

    Returns ``(origin_handoff, origin_handoff_id)``:
      - ``origin_handoff``    — the repo-relative path ``state/handoffs/<basename>``
        (forward slashes always, even on Windows) — the ratified contract shape
        enforced by ``schema_validate.py`` Rule C2-1b (``_HANDOFF_CROSS_FIELD_RULES``)
        and by ``dag.py``'s ``resolve_target``, which never appends an extension and
        so cannot resolve a bare stem. ``None`` when: ``session_id`` is None/empty,
        ``handoffs_dir`` is absent, or no handoff has ``claimed_by``/``consumed_by``
        equal to the session id.
      - ``origin_handoff_id`` — the C2 ID-companion: the matched baton's OWN
        ``handoff_id`` frontmatter scalar, read from the SAME file that produced
        ``origin_handoff`` (never a second/independent resolution).  ``None`` when
        ``origin_handoff`` is ``None``, or when the matched baton has no ``handoff_id``
        field (pre-existing artifacts are not backfilled — see PROVENANCE_FIELD_MAP
        comment block).  An empty-value ``handoff_id:`` line is also treated as absent.

    Only reads live state/handoffs/ (not archive/handoffs/) — the fork is being authored
    by the current session's chain, which is always in the live directory.

    Raises:
        OSError — when ``handoffs_dir`` cannot be enumerated (e.g. permission-denied).
        This resolution is provenance-critical: a silently-swallowed enumeration
        failure would stamp the new fork's ``origin_handoff``/``origin_handoff_id``
        as null even though the true origin baton exists and simply couldn't be
        read this pass — indistinguishable from the genuine "no origin handoff"
        case, corrupting provenance with no visible signal. Fail loud instead;
        the caller (``_handler``) surfaces this as an explicit error reply rather
        than writing a fork with silently-null provenance.

        NOTE: uses ``iterdir()``, NOT ``glob("*.md")`` — ``Path.glob()``'s selector
        silently swallows ``PermissionError`` while walking (verified: unreadable
        dir → ``glob()`` yields an empty iterator, no exception), which made the
        previous ``except OSError: return None, None`` here dead code for the
        exact permission-denied case it existed to guard.
    """
    if not session_id:
        return None, None
    if not handoffs_dir.is_dir():
        return None, None
    md_files = sorted(p for p in handoffs_dir.iterdir() if p.suffix == ".md" and p.is_file())
    for hfile in md_files:
        # Review: code-reviewer (Finding 1) -- before routing the claim read
        # through resolve_claim_state, EVERY unreadable candidate file was
        # read unconditionally here (via hfile.read_text()) and logged on
        # OSError. resolve_claim_state's own mirror read now swallows an
        # unreadable file's OSError silently (claim_state._read_mirror_claim),
        # so a candidate that is unreadable AND does not match session_id
        # (the common case) skipped with zero diagnostic -- a corrupted
        # handoff corpus became invisible during fork-origin resolution. A
        # cheap os.access() probe restores the lost signal without
        # reintroducing an unconditional full-file read of every candidate;
        # resolution still proceeds through resolve_claim_state exactly as
        # before (degrades to "no claim" for this file, matching prior
        # semantics for an unreadable/non-matching candidate).
        if not os.access(hfile, os.R_OK):
            _LOG.warning(
                "handoff.author_fork: candidate file %s is unreadable "
                "(permission denied) -- skipping for origin_handoff resolution",
                hfile,
            )
        # Ledger-first: resolve_claim_state consults the branch-independent
        # claim ledger before the tracked-frontmatter mirror (POSTURE: ledger
        # authoritative). repo_root is threaded through so git_common_dir
        # resolves against the caller's worktree rather than hfile's own
        # parent; git_common_dir is lru_cache'd, so per-file resolution here
        # costs a cached-dict lookup, not a subprocess spawn.
        claim_state = resolve_claim_state(hfile, repo_root=repo_root)
        claimed_by = claim_state.holder
        if claimed_by and claimed_by == session_id:
            try:
                text = hfile.read_text(encoding="utf-8", errors="replace")
            except OSError:
                # Review: code-reviewer (Finding 2) -- route through the module
                # logger instead of a raw print, matching the discipline used
                # elsewhere in this file for per-item skip-and-continue failures.
                _LOG.warning(
                    "handoff.author_fork: skipping unreadable candidate file %s: %s",
                    hfile, sys.exc_info()[1],
                )
                continue
            # C2: origin_handoff_id is derived from THIS SAME file/text — never a
            # separate lookup — so it can never disagree with origin_handoff.
            handoff_id = extract_frontmatter_scalar(text, "handoff_id") or None
            # Path shape, not a bare stem: "state/handoffs/" is a literal prefix,
            # not derived from handoffs_dir -- this function only ever reads the
            # live directory, and the id index keys on this same logical prefix
            # (archived records included). Built with an explicit forward slash
            # (never os.path.join/Path string coercion) so the emitted path never
            # picks up backslashes on Windows.
            return "state/handoffs/" + hfile.name, handoff_id
    return None, None


# ---------------------------------------------------------------------------
# Plan / goal candidate enumeration for disambiguation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Filename generation
# ---------------------------------------------------------------------------


def _fork_handoff_filename(title: str) -> str:
    """Generate a unique filename for a fork handoff.

    Format: ``{YYYY-MM-DD}_{HHMMSS}_{uuid4_short}.md``
    UUID suffix guarantees uniqueness even if two forks are authored in the same second.
    The title is NOT embedded to keep filenames machine-stable.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H%M%S")
    uid = uuid.uuid4().hex[:8]
    return f"{date_str}_{time_str}_{uid}.md"


# ---------------------------------------------------------------------------
# Stamping mode — d3's spinoff rewrite (Option A, ratified 2026-07-27)
#
# ``baton_assemble``'s kind="spinoff" brief mints the readable-slug artifact
# via d1 (``coordinator-doc-new --type=spinoff``) — that file is the one the
# operator keeps. d3 used to AUTHOR a second, separately-named file here with
# correct origin_* provenance that nothing downstream ever read, orphaning it
# while the operator's real file carried none. Fixed by rewiring d3 from an
# author into a STAMPER: it now writes the five origin_* fields onto d1's
# already-created file in place. See
# ``coordinator_core.baton_assemble.apply._dispatch_handoff_author_fork`` for
# the caller side of this contract.
#
# Triggered by "handoff_path" being present in params — no other
# ``handoff.author_fork`` caller (the from-scratch author path below, used by
# kinds other than spinoff and by any other direct caller of this op) ever
# supplies that key, so key presence alone discriminates the two modes
# without touching the author path's own contract.
# ---------------------------------------------------------------------------


def _resolve_stamp_match_text(contained: Path) -> str:
    """Stamp mode's ``match_text`` fallback: the target handoff's own
    frontmatter ``title`` when readable, else the filename stem.

    The target file already exists on disk in stamp mode (unlike author mode,
    which has only ``title``), so its own ``title`` is a far better ranking
    haystack than the timestamp+uuid filename stem (e.g.
    ``2026-08-02_141130_some-thing``), which shares almost nothing in common
    with a prose plan/goal title and drives every score toward zero.

    Negative-spec:
    - Does NOT raise — an unreadable file, absent frontmatter, or missing/
      empty ``title`` field all fall back to ``contained.stem`` silently.
    - Does NOT reimplement frontmatter parsing — reuses
      ``frontmatter.primitives.split_frontmatter`` / ``read_fm_field_unquoted``.
    """
    stem = contained.stem
    try:
        text = contained.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return stem
    split = split_frontmatter(text)
    if split is None:
        return stem
    title_val = read_fm_field_unquoted(split.fm_text, "title")
    if title_val:
        return title_val
    return stem


async def _handle_stamp(params: dict, repo_root: Optional[Path]) -> dict:
    """Stamping-mode entry point — writes the five ``origin_*`` provenance
    fields onto an ALREADY-EXISTING handoff file in place, rather than
    authoring a new one.

    Required params:
        handoff_path (str) — target file: absolute, or repo-relative under
            ``state/handoffs/``. Must already exist on disk.

    Optional params (each falls back to the SAME self-resolution the author
    path below uses when the caller does not supply a truthy value):
        origin_session     (str | None)
        origin_handoff     (str | None)
        origin_handoff_id  (str | None)
        origin_plan_id     (str | None)
        origin_goal_id     (list[str] | None)
        match_text         (str) — disambiguation ranking text; defaults to the
            target handoff's own frontmatter ``title`` when readable, else the
            target file's stem (see ``_resolve_stamp_match_text``).

    Returns ``{"status": "ok", ...}`` (or the error shape on a genuine
    failure), with ``handoff_path``/``handoff_id`` naming the STAMPED file
    (the caller-supplied target — never a freshly-minted path). Unlike the
    author path below, stamp mode never returns ``needs_disambiguation``:
    the target file already exists on disk by the time this op runs, so an
    early abort would leave it permanently unstamped with no error surfaced.
    Instead, an unresolved (below the score floor, or too close to call —
    see ``match_core.resolve_candidate``) ``origin_plan_id`` or
    ``origin_goal_id`` DEGRADES to ``null`` and stamping continues for every
    other field; the ok payload then carries a ``degraded`` list of
    ``{"field", "reason", "candidates"}`` entries — one per degraded field —
    so the caller can report or log it. Absence (zero candidates,
    ``ResolutionReason.NO_CANDIDATES``) is not reported as degraded; it
    resolves to ``null`` exactly as it always has.
    """
    handoff_path_raw = (params.get("handoff_path") or "").strip()
    if not handoff_path_raw:
        return _err("missing required param: handoff_path (stamp mode)")

    if repo_root is None:
        return _err(
            "no repo_root resolved — _origin_worktree required for handoff.author_fork"
        )
    worktree_root = main_worktree_root(repo_root)
    handoffs_dir = worktree_root / "state" / "handoffs"

    target = Path(handoff_path_raw)
    if not target.is_absolute():
        target = worktree_root / target
    contained = contained_path(target, [handoffs_dir])
    if contained is None:
        return _err(
            f"handoff_path escapes state/handoffs/ or is unresolvable: {handoff_path_raw!r}"
        )
    if not contained.is_file():
        return _err(f"handoff_path not found on disk: {handoff_path_raw}")

    match_text: str = (params.get("match_text") or "").strip() or _resolve_stamp_match_text(
        contained
    )

    # Ambiguity in stamp mode DEGRADES rather than aborts: d1's file already
    # exists on disk by the time this op runs, so an early needs_disambiguation
    # return here would leave the operator holding an unstamped file with no
    # error surfaced — the exact silent failure this stamping mode exists to
    # eliminate, merely relocated one step later. Each ambiguous field is
    # stamped null and recorded here so the caller can report/log it; the
    # from-scratch author path below is untouched and keeps aborting, since
    # nothing has been created yet on that path.
    degraded: List[Dict[str, object]] = []

    # --- origin_session --- caller-supplied value wins; else self-resolve.
    origin_session = params.get("origin_session") or None
    if not origin_session:
        origin_session = resolve_current_session_id(worktree_root)

    # --- origin_handoff / origin_handoff_id --- resolved TOGETHER when
    # origin_handoff is not supplied (never a separate lookup — mirrors
    # _resolve_origin_handoff's own C2 invariant). A caller-supplied
    # origin_handoff keeps whatever origin_handoff_id accompanied it
    # (possibly none), rather than re-deriving one independently.
    origin_handoff = params.get("origin_handoff") or None
    origin_handoff_id = params.get("origin_handoff_id") or None
    if not origin_handoff:
        try:
            origin_handoff, resolved_handoff_id = await asyncio.to_thread(
                _resolve_origin_handoff, handoffs_dir, origin_session, repo_root=worktree_root
            )
        except OSError as exc:
            return _err(f"cannot enumerate {handoffs_dir} to resolve origin_handoff: {exc}")
        if not origin_handoff_id:
            origin_handoff_id = resolved_handoff_id

    # --- origin_plan_id --- truthy caller value wins; else auto-resolve or
    # surface disambiguation candidates (identical contract to the author
    # path's plan-resolution branch).
    origin_plan_id_param = params.get("origin_plan_id")
    origin_plan_id: Optional[str]
    if origin_plan_id_param:
        origin_plan_id = str(origin_plan_id_param)
    else:
        plans_dir = worktree_root / "docs" / "plans"
        plan_items = await asyncio.to_thread(_collect_plans, plans_dir)
        plan_resolution = resolve_candidate(match_text, plan_items)
        origin_plan_id = plan_resolution["resolved_id"]
        reason = plan_resolution["reason"]
        if reason is not None and reason != ResolutionReason.NO_CANDIDATES:
            degraded.append({
                "field": "origin_plan_id",
                "reason": f"{_resolution_reason_text(reason, len(plan_resolution['ranked']))} — stamped null",
                "candidates": [
                    {"plan_id": e["id"], "title": e["title"], "score": e["score"]}
                    for e in plan_resolution["ranked"]
                ],
            })

    # --- origin_goal_id --- truthy caller value wins; else auto-resolve or
    # surface disambiguation candidates (identical contract to the author
    # path's goal-resolution branch).
    origin_goal_id_param = params.get("origin_goal_id")
    origin_goal_id: Optional[List[str]]
    if origin_goal_id_param:
        if isinstance(origin_goal_id_param, list):
            origin_goal_id = [str(g) for g in origin_goal_id_param if g]
        else:
            origin_goal_id = [str(origin_goal_id_param)]
    else:
        goals_dir = worktree_root / "state" / "goals"
        goal_items = await asyncio.to_thread(_collect_goals, goals_dir)
        goal_resolution = resolve_candidate(match_text, goal_items)
        resolved_goal_id = goal_resolution["resolved_id"]
        origin_goal_id = [resolved_goal_id] if resolved_goal_id is not None else None
        reason = goal_resolution["reason"]
        if reason is not None and reason != ResolutionReason.NO_CANDIDATES:
            degraded.append({
                "field": "origin_goal_id",
                "reason": f"{_resolution_reason_text(reason, len(goal_resolution['ranked']))} — stamped null",
                "candidates": [
                    {"goal_id": e["id"], "title": e["title"], "score": e["score"]}
                    for e in goal_resolution["ranked"]
                ],
            })

    provenance = {
        "origin_session": origin_session,
        "origin_handoff": origin_handoff,
        "origin_handoff_id": origin_handoff_id,
        "origin_plan_id": origin_plan_id,
        "origin_goal_id": origin_goal_id,
    }

    def _mutate(old_text: str) -> str:
        if not old_text:
            raise MutateAbort(
                f"handoff.author_fork (stamp mode): target unexpectedly empty/missing: "
                f"{contained}"
            )
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(
                f"no valid YAML frontmatter block in {contained} — cannot stamp"
            )
        try:
            new_fm_text = _stamp_fork_provenance(split.fm_text, provenance)
        except ValueError as exc:
            # _stamp_fork_provenance's block-scalar / nested-block guards
            # raise plain ValueError (matching replace_fm_field/
            # insert_fm_field's own contract) — locked_rmw does not wrap
            # non-MutateAbort exceptions from this callback, so translate
            # here to reach the structured _err(...) path every other
            # failure mode in this function uses instead of an unhandled
            # ValueError escaping the op boundary. Review: code-reviewer
            # (P1) — _handle_stamp's except clauses didn't catch this.
            raise MutateAbort(
                f"handoff.author_fork (stamp mode): cannot stamp fork "
                f"provenance onto {contained}: {exc}"
            ) from exc
        return rebuild(split, new_fm_text)

    try:
        await asyncio.to_thread(locked_rmw, contained, _mutate, repo_root=repo_root)
    except LockTimeout as exc:
        return _err(f"lock timeout acquiring file lock to stamp fork provenance: {exc}")
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "mutate aborted")
    except OSError as exc:
        return _err(f"I/O error stamping fork provenance onto {contained}: {exc}")

    _LOG.info(
        "handoff.author_fork (stamp mode): stamped %s (origin_session=%s, "
        "origin_handoff=%s, origin_handoff_id=%s, origin_plan_id=%s, "
        "origin_goal_id=%s)",
        contained,
        origin_session,
        origin_handoff,
        origin_handoff_id,
        origin_plan_id,
        origin_goal_id,
    )

    result = {
        "status": "ok",
        "handoff_path": str(contained),
        "handoff_id": contained.stem,
        "origin_session": origin_session,
        "origin_handoff": origin_handoff,
        "origin_handoff_id": origin_handoff_id,
        "origin_plan_id": origin_plan_id,
        "origin_goal_id": origin_goal_id,
    }
    if degraded:
        result["degraded"] = degraded
    return result


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------


@register_op("handoff.author_fork")
async def _handler(
    params: dict,
    repo_root: Optional[Path] = None,
) -> dict:
    """JSON-RPC ``handoff.author_fork`` handler — create a fork handoff with provenance.

    MUTATING (writes one new ``state/handoffs/*.md`` file; DR-213 additive-create pattern).
    Blocking FS I/O wrapped in ``asyncio.to_thread``.

    ``repo_root`` receives ``git_common_dir(caller_worktree)`` via
    ``_OP_KEY_SCOPE: common_dir`` (ipc.py).  Handler calls ``main_worktree_root(repo_root)``
    to derive the caller's worktree root before any path construction.

    Stamping mode: when ``params`` carries a ``handoff_path`` key, dispatch is
    routed to ``_handle_stamp`` instead — see that function's docstring. That
    mode stamps the five ``origin_*`` fields onto an ALREADY-EXISTING file
    rather than authoring a new one (the spinoff d3 rewrite, ratified
    2026-07-27); it is a distinct contract, not a variant of the params below.

    Required params (from-scratch author mode):
        title  (str)  — title of the fork handoff (required for a meaningful artifact).

    Optional params:
        branch         (str)       — branch context; default "none".
        kind           (str)       — handoff kind; default "fork-handoff".
        workstream     (str | null) — workstream slug; default null.
        origin_plan_id (str | null) — explicit plan id; absent → auto-resolve or disambiguate.
        origin_goal_id (list | null) — explicit goal id list; absent → auto-resolve or disambiguate.
        match_text     (str)       — text used for candidate ranking when disambiguating;
                                     defaults to ``title`` when absent.
        body           (str)       — optional body text for the new handoff.

    Returns on success:
        {
            "status":        "ok",
            "handoff_path":  str,         # absolute path to the new handoff file
            "handoff_id":    str,         # filename stem
            "origin_session": str | null,
            "origin_handoff": str | null,
            "origin_handoff_id": str | null,  # C2 ID-companion, same-baton-derived
            "origin_plan_id": str | null,
            "origin_goal_id": list[str] | null,
        }

    Returns on disambiguation needed:
        {
            "status":     "needs_disambiguation",
            "candidates": {
                "plans": [{"plan_id": str, "title": str, "score": float}, ...],  # when plan ambiguous
                "goals": [{"goal_id": str, "title": str, "score": float}, ...],  # when goal ambiguous
            },
        }

    Returns on error:
        {"exit_code": 1, "error": str}

    Negative-spec:
        - Does NOT enforce the PM spinoff gate — primitive; gate is skill-layer.
        - Does NOT commit.
        - Does NOT write outside state/handoffs/ in the caller's worktree.
        - Does NOT overwrite an existing handoff file.
        - Does NOT surface plan and goal ambiguity simultaneously — plan ambiguity is
          resolved first; goal ambiguity is surfaced only after the plan is pinned.
          A caller with both plan and goal ambiguous must invoke twice: first to resolve
          plans (``candidates.plans`` returned), then again (with plan pinned) to resolve
          goals (``candidates.goals`` returned).  Review: code-reviewer (F5).
    """
    if "handoff_path" in params:
        return await _handle_stamp(params, repo_root)

    # --- Param extraction ---
    title: str = (params.get("title") or "").strip()
    if not title:
        return _err("missing required param: title")

    branch: str = (params.get("branch") or "none").strip()
    kind: str = (params.get("kind") or "fork-handoff").strip()
    workstream: Optional[str] = params.get("workstream") or None
    if workstream:
        if _WORKSTREAM_SLUG_RE.search(workstream):
            return _err(
                f"workstream slug {workstream!r} contains invalid chars "
                "(only [A-Za-z0-9_-] permitted)"
            )
    body: str = params.get("body") or ""

    # ``origin_plan_id`` from params — sentinel None means "absent/auto-resolve".
    # Caller passes ``null`` (JSON null → Python None) to mean "explicitly no plan";
    # caller omits the key entirely to mean "auto-resolve".
    # Disambiguate: use key presence as the signal.
    plan_id_supplied = "origin_plan_id" in params
    origin_plan_id_raw = params.get("origin_plan_id")  # None if absent or null

    goal_id_supplied = "origin_goal_id" in params
    origin_goal_id_raw = params.get("origin_goal_id")  # None if absent or null

    match_text: str = (params.get("match_text") or title).strip()

    # --- Repo / worktree resolution ---
    if repo_root is None:
        return _err(
            "no repo_root resolved — _origin_worktree required for handoff.author_fork"
        )
    worktree_root = main_worktree_root(repo_root)

    # --- origin_session ---
    origin_session = resolve_current_session_id(worktree_root)

    # --- origin_handoff / origin_handoff_id --- (scan for claimed_by matching origin_session;
    # origin_handoff_id is derived from the SAME matched file, never a separate lookup — see
    # _resolve_origin_handoff docstring.)
    handoffs_dir = worktree_root / "state" / "handoffs"
    try:
        origin_handoff, origin_handoff_id = await asyncio.to_thread(
            _resolve_origin_handoff, handoffs_dir, origin_session, repo_root=worktree_root
        )
    except OSError as exc:
        # Fail loud (provenance-critical) — see _resolve_origin_handoff docstring.
        # A silently-swallowed enumeration failure here would write a fork with
        # falsely-null origin_handoff/origin_handoff_id provenance.
        return _err(f"cannot enumerate {handoffs_dir} to resolve origin_handoff: {exc}")

    # --- origin_plan_id disambiguation ---
    origin_plan_id: Optional[str]
    if plan_id_supplied:
        # Caller explicitly provided (or nulled) the plan id — honour it.
        origin_plan_id = origin_plan_id_raw if origin_plan_id_raw else None
    else:
        plans_dir = worktree_root / "docs" / "plans"
        # Review: code-reviewer (F2/F9) — call _collect_plans once; derive ids inline.
        # Eliminated _enumerate_plan_ids wrapper that forced a second filesystem scan
        # in the ambiguous branch.
        plan_items = await asyncio.to_thread(_collect_plans, plans_dir)
        plan_resolution = resolve_candidate(match_text, plan_items)
        origin_plan_id = plan_resolution["resolved_id"]
        plan_reason = plan_resolution["reason"]
        if plan_reason is not None and plan_reason != ResolutionReason.NO_CANDIDATES:
            # No candidate cleared the auto-resolve bar — surface to caller
            # for disambiguation (author mode has not written anything yet).
            return {
                "status": "needs_disambiguation",
                "candidates": {
                    "plans": [
                        {
                            "plan_id": e["id"],
                            "title": e["title"],
                            "score": e["score"],
                        }
                        for e in plan_resolution["ranked"]
                    ]
                },
            }

    # --- origin_goal_id disambiguation ---
    origin_goal_id: Optional[List[str]]
    if goal_id_supplied:
        # Caller explicitly provided (or nulled) the goal id list — honour it.
        if origin_goal_id_raw is None:
            origin_goal_id = None
        elif isinstance(origin_goal_id_raw, list):
            origin_goal_id = [str(g) for g in origin_goal_id_raw if g]
        else:
            # Scalar supplied — wrap in list (lenient caller normalisation).
            origin_goal_id = [str(origin_goal_id_raw)] if origin_goal_id_raw else None
    else:
        goals_dir = worktree_root / "state" / "goals"
        # Review: code-reviewer (F2/F9) — call _collect_goals once; derive ids inline.
        # Eliminated _enumerate_active_goal_ids wrapper that forced a second filesystem
        # scan in the ambiguous branch.
        goal_items = await asyncio.to_thread(_collect_goals, goals_dir)
        goal_resolution = resolve_candidate(match_text, goal_items)
        resolved_goal_id = goal_resolution["resolved_id"]
        origin_goal_id = [resolved_goal_id] if resolved_goal_id is not None else None
        goal_reason = goal_resolution["reason"]
        if goal_reason is not None and goal_reason != ResolutionReason.NO_CANDIDATES:
            # No candidate cleared the auto-resolve bar — surface to caller
            # for disambiguation (author mode has not written anything yet).
            return {
                "status": "needs_disambiguation",
                "candidates": {
                    "goals": [
                        {
                            "goal_id": e["id"],
                            "title": e["title"],
                            "score": e["score"],
                        }
                        for e in goal_resolution["ranked"]
                    ]
                },
            }

    # --- All origin ids resolved (or explicitly null) — proceed to write ---
    provenance = {
        "origin_session": origin_session,
        "origin_handoff": origin_handoff,
        "origin_handoff_id": origin_handoff_id,
        "origin_plan_id": origin_plan_id,
        "origin_goal_id": origin_goal_id,
    }

    created_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    filename = _fork_handoff_filename(title)

    handoffs_dir.mkdir(parents=True, exist_ok=True)
    out_path = handoffs_dir / filename

    # Review: code-reviewer (F2) — resolved once, in this function's own scope, so
    # a fork-handoff authored while a plan is claimed carries that plan's
    # deliverable_id instead of always minting a fresh one (DR-207 DD#1 second door).
    carried_deliverable_id = _resolve_claimed_plan_deliverable_id(worktree_root)

    # Guard: refuse to create a live handoff sharing an already-archived
    # record's filename (this op does not stamp its own handoff_id at
    # creation, so only the filename basis applies here — see
    # handoff_creation_guard's module docstring for the full invariant).
    try:
        assert_no_archived_twin(out_path, worktree_root)
    except HandoffArchivedTwinError as exc:
        return _err(str(exc))

    # --- Atomic file creation via locked_rmw(missing_ok=True) ---
    # The mutate function receives "" (file absent) and returns the full handoff content.
    # If the file somehow already exists (UUID collision), MutateAbort is raised.
    # asyncio.to_thread offloads the blocking flock + I/O off the event loop.
    _result: dict = {}

    def _mutate(old_text: str) -> str:
        if old_text:
            raise MutateAbort(
                f"handoff.author_fork: collision — file already exists: {filename}"
            )
        # Build initial content with provenance fields.
        content = _build_fork_frontmatter(
            title=title,
            created_date=created_date,
            branch=branch,
            kind=kind,
            workstream=workstream,
            provenance=provenance,
            body=body,
        )
        # Compose handoff.normalize (inline) to fill in category, summary,
        # deliverable_id, initiative — exactly the six-normalization pass from
        # normalize-handoff-frontmatter.js (no reimplemented frontmatter I/O).
        norm = _normalize_one_text(content, out_path, carried_deliverable_id)
        if norm is not None and norm is not _NO_FRONTMATTER:
            return norm["rebuilt"]
        return content

    try:
        await asyncio.to_thread(
            locked_rmw, out_path, _mutate, repo_root=repo_root, missing_ok=True
        )
    except LockTimeout as exc:
        return _err(f"lock timeout acquiring file lock for new handoff: {exc}")
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "mutate aborted")
    except OSError as exc:
        return _err(f"I/O error creating fork handoff: {exc}")

    _LOG.info(
        "handoff.author_fork: created %s (origin_session=%s, origin_handoff=%s, "
        "origin_handoff_id=%s, origin_plan_id=%s, origin_goal_id=%s)",
        out_path,
        origin_session,
        origin_handoff,
        origin_handoff_id,
        origin_plan_id,
        origin_goal_id,
    )

    return {
        "status": "ok",
        "handoff_path": str(out_path),
        "handoff_id": out_path.stem,
        "origin_session": origin_session,
        "origin_handoff": origin_handoff,
        "origin_handoff_id": origin_handoff_id,
        "origin_plan_id": origin_plan_id,
        "origin_goal_id": origin_goal_id,
    }


# ---------------------------------------------------------------------------
# Error-shape helper
# ---------------------------------------------------------------------------


def _err(msg: str) -> dict:
    """Return an exit_code=1 error reply dict."""
    _LOG.warning("handoff.author_fork: %s", msg)
    return {"exit_code": 1, "error": msg}
