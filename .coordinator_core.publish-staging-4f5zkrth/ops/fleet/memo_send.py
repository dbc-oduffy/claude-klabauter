"""
coordinator_core.ops.fleet.memo_send — memo.send MUTATING op handler
(command-type, spawn-per-call).

Purpose: Write one schema-valid memo into a registry-enumerated receiver's
cross-repo/inbox/ tree, then COMMIT the delivered memo into the receiver
repo with ALL receiver hooks neutralized via `-c core.hooksPath=<empty-tmpdir>`
(receiver-hook-independent durable delivery, committed-but-unpushed) — this
retires DR-211 D2 criterion 3 ("send is non-committing") for the send op
per PM directive 2026-07-21, amended 2026-07-21 (DR-214 amendment) to the
all-hooks-off mechanism per Patrik's approach-review REQUIRES_CHANGES
(`--no-verify` does not bypass `prepare-commit-msg`, the exact hook that
motivated this change, and also lets the receiver's OWN message hooks run
on a foreign delivery — injecting a false Session-Id trailer). Reached via
the command entrypoint (`python -m coordinator_core.invoke`) — HTTP/UDS
gating was vacated by DR-215's command-type, spawn-per-call execution
model, which retired the resident daemon and its transport surfaces
entirely. Registered as "memo.send" via @register_op;
classification and _OP_KEY_SCOPE entry are wired in C3.

Spec backlink:
    docs/plans/2026-07-05-strang-03-cross-repo-memo-send-strangle.md § C2
    DR-214: docs/decisions/DR-214-send-class-cross-tree-write-boundary.md (D2 admission)
    DR-211 D2 criterion 3 retirement (send only): PM directive 2026-07-21 —
    delivered-memo commit ported from DoE coordinator/bin/cross-repo-memo.py
    _commit_delivered_memo (~line 1590-1756), adapted to this file's async
    git-subprocess convention; mechanism amended 2026-07-21 (DR-214
    amendment, Patrik approach-review REQUIRES_CHANGES) to an all-hooks-off
    `-c core.hooksPath=<empty-tmpdir>` commit, replacing the initial
    `--no-verify` addition (which did not bypass `prepare-commit-msg`).
    Parity source: DoE coordinator/bin/cross-repo-memo.py (schema-valid emission + B3 guard)
    Lesson: 2026-07-05-common-dir-keyed-ops-must-derive-the-wor.yaml (worktree derivation)
    Lesson: 2026-07-05-externally-triggered-ops-must-contain-wi.yaml (wire-path containment)
    Precedent: pcore-11 traversal-rejection 5296973

Negative-spec:
  - Does NOT use _common.archive_and_commit — the delivered-memo commit is a
    plain scoped `git add -- <relpath>` / `git -c core.hooksPath=<empty-tmpdir>
    commit -- <relpath>` (see _commit_delivered_memo), not archive_and_commit's
    private-index rename-pathspec machinery (that helper is shaped for archive
    moves, not a single cross-tree delivery commit).
  - Does NOT grow a fleet-wide memo index (Q-d; store-less-ness invariant; AC8).
  - Does NOT fall back to direct-write if the engine is unreachable (Q-c HARD
    applies at the facade level in C3 — no legacy cross-repo-memo CLI fallback, AC4).
  - Does NOT use params.repo_root as the sender worktree — derives it via
    main_worktree_root(common_dir) (Key Decision 5 precedent; RECEIVER path is
    always registry-derived, never wire-derived).
  - Does NOT accept wire-supplied absolute paths or ../ as receiver targets —
    registry-enumerated allowed-set prevents the "trust the wire" gap (C2 containment spec).
  - Does NOT hardcode a single receiver (e.g. project-rag only) — the allowed-set
    is registry-derived generic substrate.
  - DOES commit into the receiver's tree with ALL hooks neutralized via
    `-c core.hooksPath=<empty-tmpdir>` (retires D2 criterion 3, PM directive
    2026-07-21; mechanism amended 2026-07-21 per Patrik REQUIRES_CHANGES —
    NOT `--no-verify`, which does not bypass `prepare-commit-msg` and lets
    the receiver's message hooks inject a foreign trailer) — see
    _commit_delivered_memo. This bullet replaces the retired "does not
    commit" claim; kept in the negative-spec block so a reader scanning this
    list for the commit behavior finds the corrected affirmation here rather
    than a stale claim.
  - Does NOT create a branch in the receiver repo (removed 2026-07-21 per
    Patrik REQUIRES_CHANGES — a headless engine switching a receiver's
    active branch, e.g. mid-bisect/mid-rebase, is an unacceptable foreign
    mutation). A receiver with no active branch (detached/bare/unborn HEAD)
    is left with the memo file written but UNCOMMITTED — see
    _commit_delivered_memo.
  - Does NOT push the receiver repo — the all-hooks-off commit mechanism
    also suppresses the receiver's own post-commit (e.g. auto-push) hook,
    so a delivered memo is committed-but-unpushed by design; propagation is
    left entirely to the receiver's own next push.
  - Does NOT accept an unrecognized frontmatter param silently (C9,
    `cross-repo/inbox/2026-07-21-claude-central-em-memo-send-drops-unknown-
    frontmatter-keys.md`) — _validate_send_params rejects any key outside
    `_KNOWN_PARAM_KEYS` with an exit_code:1 setup-error; a param is either
    declared-and-emitted or the whole send fails loud, never a silent
    exit_code:0 drop (A11).
  - Does NOT resolve a same-date+topic collision via in-place `--force`/
    `--replace` overwrite (C6, A6) — the sanctioned re-delivery path is a
    FRESH dated file carrying `supersedes:`, composed via `_redelivery_filename`
    only when the caller declares `supersedes:`; the collision itself, and its
    fail-loud O_EXCL semantics, are otherwise completely unchanged (still
    refuses without `supersedes:`, per C1 D2 criterion 4).
  - Does NOT require `scoped_to` for any `kind` (2026-07-21 fix, routed via
    cross-repo/inbox/2026-07-21-claude-central-em-debash-directive-cites-guard-
    plus-scoped-to-q.md) — there is no `if kind in ("ask", "proposal")` branch
    anywhere in this module. The gate is presence-triggered: `scoped_to`
    absent entirely passes (a directional/doctrine-establishing ask governs
    no versioned artifact); `scoped_to` present must be the COMPLETE triple
    (`artifact` + exactly one of `version`/`sha` + `seam`) or the send fails
    loud — see `_validate_scoped_to`. A blanket kind-based requirement was
    explicitly rejected because it forces a directional ask to fabricate a
    version pin, mis-kind, or downgrade to `consult`.
  - Does NOT silently truncate an EXPLICITLY authored `summary` over
    `_SUMMARY_MAX_CHARS` (2026-07-22 fix, root-caused via cross-repo/inbox/
    2026-07-22-claude-central-em-snippet-sync-adoption-and-body-drop-
    verdict.md). A DERIVED summary (the `summary` param omitted) is
    untouched — `derive_prose_summary` already self-caps. This is a
    DELIBERATE divergence from any clamp/truncate behavior in DoE's mirror
    (`cross-repo-memo:1810-1830`'s parity note) — the former silent
    `[:_SUMMARY_MAX_CHARS - 1] + "…"` clamp is exactly the defect the routed
    memo root-caused (a 120-char summary truncated mid-sentence on a
    delivered memo, with no notice to the sender).
    2026-08-07 PM ruling (AC9, supersedes AC7 — docs/plans/2026-08-07-memo-
    summary-cap-warn-at-draft.md § C4): `_validate_send_params` no longer
    fails loud on an over-cap explicit summary — it WARNS and SUBSTITUTES
    the body-derived summary, echoing the author's original text back
    verbatim on the result envelope (`summary_cap_advisory` /
    `summary_over_cap_original`). Substitution is never truncation — the
    invariant above survives unchanged, it is just enforced by substitution
    now rather than refusal. `_compose_memo`'s `ValueError` backstop still
    raises for a direct caller that bypasses `_validate_send_params` (no
    envelope exists there to carry an advisory through).
  - Does NOT accept a missing/empty `summary` and silently derive one from
    `body` (DEC-1, 2026-07-24 memo-ownership-and-redesign plan) — kind AND
    summary are now UNCONDITIONALLY required at send time (present +
    non-empty), a SEND-TIME gate only: it does not tighten receiver-side
    cross-field validation or the schema `required` array, so the existing
    corpus (memos lacking either field) still validates and actions
    normally. This is NOT a kind-conditional rule (no
    `if kind in ("ask", "proposal")` branch) — both fields are required for
    every kind, unconditionally.
  - Does NOT accept an `in_reply_to` value that fails to resolve against
    THIS repo's own `cross-repo/inbox/` or `cross-repo/archive/` (searched
    recursively — 2026-07-25 write-side addition, closing the gap that
    `coordinator_core.pickup_assemble._candidate_is_linked` already read this
    field but nothing wrote it) — see `_validate_in_reply_to_exists`. Omitted
    entirely when the caller does not supply it (optional field, no empty/null
    key ever emitted).
  - Does NOT leave the one-shot (flag-only, no `memo.draft`) send path with
    zero local evidence of having happened (2026-08-04 fix, routed via a
    cross-repo memo from doe-claude-em: a plan chunk in a sending repo whose
    deliverable is a memo had no local artifact for `close-out-and-stamp`'s
    anti-self-attestation `disposition_ref` ancestry check to point at) — see
    `_append_sent_ledger`. UNLIKE `_stamp_sender_outbox_sent`, which only
    fires when a `state/memo-outbox/<topic>.md` draft exists, the ledger
    write is UNCONDITIONAL: it appends a JSONL line to
    `state/memo-outbox/sent-ledger.jsonl` in the sender's own worktree on
    every send, lifecycle or one-shot, single-receiver or fan-out.

1->N fan-out (DEC-3, C7 — 2026-07-24 memo-ownership-and-redesign plan):
    `to` may be a single receiver string (unchanged single-receiver path,
    below) OR a non-empty list of receiver strings — the latter routes
    through `_memo_send_fan_out`, which iterates THIS SAME single-receiver
    path once per receiver (never a new batch-write primitive) and returns
    an extended envelope carrying `campaign_id` + a per-receiver `manifest`
    ([{receiver, outcome, error, campaign_id}, ...]) alongside the standard
    exit_code/candidates/acted/skipped/failed keys. Semantics: N
    independent, individually-atomic single-file writes within one
    invocation — each satisfies all seven DR-214 D2 admission bounds on its
    own via the unmodified single-receiver path. Failure mode is
    best-effort, fail-loud-PER-receiver: a failure on receiver K does NOT
    abort receivers K+1..N. `campaign_id` (caller-supplied or
    engine-generated when a fan-out omits it) is threaded into
    `_compose_memo` and so is PERSISTED TO DISK on every successful
    per-receiver write, not merely echoed in the manifest — a rag-side
    compliance query over the on-disk field is what DEC-3 specifies for
    "did all N act?" visibility (never a makima-local index, AC8).
    Negative-spec: `_memo_send_fan_out` does NOT re-derive or duplicate the
    single-receiver validation/compose/write/commit logic — every receiver
    write goes through the exact same `_memo_send(one_params, repo_root=...)`
    call the single-receiver caller would make.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime
import io
import json
import logging
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional

from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    read_fm_field_unquoted,
    rebuild as _rebuild_frontmatter,
    replace_fm_field,
    split_frontmatter,
)
from coordinator_core.git import git_state
from coordinator_core.git.git_dir import resolve_git_dir
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, locked_rmw
from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.fleet._common import (
    build_act_result,
    build_dry_run_result,
    build_setup_error_result,
    main_worktree_root,
    _make_git_env,
)
from coordinator_core.ops.fleet._memo_resolver import (
    AmbiguousReceiverError,
    RegistryReadError,
    canonical_receiver_id as _canonical_receiver_id,
    convention_repo_key as _convention_repo_key,
    read_central_receiver_ids as _read_central_receiver_ids,
    publish_mirror_path_match as _publish_mirror_path_match,
    read_publish_mirrors as _read_publish_mirrors,
    read_receiver_aliases as _read_receiver_aliases,
    read_registry_repos as _read_registry_repos,
    receiver_em_to_repo_key as _receiver_em_to_repo_key,
    registry_home as _registry_home,
    resolve_receiver_inbox as _resolve_receiver_inbox,
    suggest_nearest_receiver as _suggest_nearest_receiver,
)
from coordinator_core.ops.fleet._memo_summary import (
    _SUMMARY_MAX_CHARS,
    derive_prose_summary,
    is_placeholder_summary,
    validate_explicit_summary,
)
from coordinator_core.session import core as _session_core

_LOG = logging.getLogger(__name__)

# Mode constant for the envelope mode field (memo.send is a single-mode op).
_MODE = "send"

# sent_by (C7, docs/plans/2026-08-13-session-identity-earns-its-keep.md):
# explicit sentinel for "this send could not resolve its own session id" —
# a memo that cannot name its sender must SAY SO, never omit the field
# silently. Rendered into frontmatter/ledger the same as a resolved UUID
# would be; only the delivery-commit trailer is skipped for this sentinel
# (see _commit_delivered_memo's never-raise contract — an unresolved
# sender must not degrade a successful delivery commit).
_SENT_BY_UNRESOLVED = "unresolved"


def _resolve_sent_by(cwd: Optional[str] = None) -> str:
    """Resolve THIS send's session UUID via the canonical 3-tier chain
    (coordinator_core.session.core.resolve_session_id), substituting the
    explicit `_SENT_BY_UNRESOLVED` sentinel — never silent omission — when
    resolution fails.

    `resolve_session_id` already never raises (empty string signals
    "unresolvable"); the try/except here is belt-and-braces against an
    import-time or environment surprise, matching this module's other
    never-raise send-path helpers.

    Negative-spec (plan AC10): returns a durable session UUID or the
    unresolved sentinel — NEVER a resolved messaging address. Addresses are
    per-process and a replayed one is actively wrong, not merely stale; this
    function does not know about addresses at all.
    """
    try:
        session_id = _session_core.resolve_session_id(cwd)
    except Exception:
        session_id = ""
    return session_id or _SENT_BY_UNRESOLVED

# Topic slug: filesystem-safe, no path-traversal chars.
# Mirrors cross-repo-memo CLI _TOPIC_SLUG_RE exactly — enforces the same
# YYYY-MM-DD-<topic>.md filename contract (5-lockstep-site invariant).
_TOPIC_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*$")

# DR-026 sender-namespacing: byte-for-byte port of DoE cross-repo-memo
# _memo_filename's sanitization regexes (coordinator/bin/cross-repo-memo.py ~line 1410-1418).
_SENDER_SLUG_INVALID_RE = re.compile(r"[^a-z0-9-]+")
_SENDER_SLUG_RUN_DASH_RE = re.compile(r"-{2,}")
_TOPIC_DOUBLED_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}-)+")

# Engine actor-id for from: when the caller does not supply from_id.
# The asyncio engine has no EM session identity; this is the engine-actor sentinel.
# DoE Ask-1 concurrence: consumers key on the file at the path (schema-valid frontmatter),
# not on the writing process — an engine actor-id in from: is sufficient.
_ENGINE_ACTOR_ID = "makima-engine"

# Generator-provenance: primarily writes+commits into a registry-enumerated
# RECEIVER repo's cross-repo/inbox/ tree (a different repo, not fixed), and
# unconditionally appends one row per send to the sender's own
# state/memo-outbox/sent-ledger.jsonl -- a data-dependent set of tracked
# paths across repos, never one fixed target.
MUTATES = ["state/memo-outbox/sent-ledger.jsonl", "cross-repo/inbox/*.md"]


def resolve_sender_id(from_id: Optional[str]) -> str:
    """Resolve the caller-declared sender identity, defaulting to the engine actor.

    Single authority for the `from_id or _ENGINE_ACTOR_ID` default every
    memo-writing op applies to its own `from_id` param (memo.send here;
    memo.draft duplicates this exact expression independently — see that
    module's own from_id line, which is NOT a preview of this op and is out
    of scope for this factor-out). memo.list's resolution-mode preview
    (`memo_list._resolve_candidate`) calls this SAME function — rather than
    hardcoding `_ENGINE_ACTOR_ID` — so a caller that declares `from_id` to
    memo.list's preview and later to memo.send gets a byte-identical
    `resolved_filename`/actual-write filename pair (the defect this closes:
    memo.list previously computed its preview filename with the engine actor
    id unconditionally, regardless of what `from_id` a real send would use —
    reported by DoE/claude-central-em, `cross-repo-memo --dry-run` preview
    showed `makima-engine`-namespaced filenames for DoE-origin sends that
    actually land `claude-central-em`-namespaced).

    A falsy `from_id` (None or empty string) resolves to `_ENGINE_ACTOR_ID` —
    the asyncio engine has no EM session identity of its own; this is the
    sentinel it signs sends with when the caller declines to declare one (see
    the `_ENGINE_ACTOR_ID` module comment above for the DoE Ask-1 concurrence
    this rests on).

    Sender-side canonicalization: when the resolved identity is itself a
    central/redirect alias (the DoE seat sending FROM e.g. `claude-central-em`
    or a redirect alias), it is canonicalized to the SAME repo-matching
    central id `memo.send`'s receiver-side addressee gate uses
    (`_memo_resolver.canonical_receiver_id`) — otherwise outbound filenames
    from that one seat split across whichever alias each caller happened to
    pass as `from_id`, defeating the DR-026 sender-namespace de-duplication
    this function's filename consumers rely on. Degrades to the raw
    (uncanonicalized) identity on `RegistryReadError`/`AmbiguousReceiverError`
    — sender-slug canonicalization is a filename-namespacing convenience, not
    the addressee-gate correctness surface (that's the receiver-side `to:`
    stamp in `_compose_memo`), so it must never raise out of a function every
    memo-writing op's param validation calls unconditionally.
    """
    raw = from_id or _ENGINE_ACTOR_ID
    try:
        return _canonical_receiver_id(raw)
    except (RegistryReadError, AmbiguousReceiverError) as exc:
        _LOG.warning(
            "resolve_sender_id: sender-slug canonicalization degraded to raw "
            "id %r (falling back, NOT raising — sender-slug is filename "
            "namespacing, not the addressee gate); underlying error: %s: %s",
            raw,
            type(exc).__name__,
            exc,
        )
        return raw


# ---------------------------------------------------------------------------
# Registry-enumerated containment check (C1 D2 criterion 2; C2 wire-path guard)
# ---------------------------------------------------------------------------

def _containment_check(
    target_inbox_dir: Path,
    target_file: Path,
    all_repos: dict[str, str],
) -> Optional[str]:
    """Verify target_file is within a registry-enumerated receiver inbox.

    Spec backlink:
        docs/plans/2026-07-05-strang-03-cross-repo-memo-send-strangle.md § C2
        wire-path containment; lesson 2026-07-05-externally-triggered-ops-must-contain-wi.yaml;
        pcore-11 traversal-rejection precedent 5296973.

    Builds the allowed-set as {Path(repo).resolve() / "cross-repo" / "inbox"} for
    every registered repo. Then checks:
      1. resolved inbox dir is in the allowed-set (registry-enumerated; not wire-derived).
      2. resolved target file is_relative_to() the resolved inbox dir (no ../ escape).

    Returns None on pass; returns a human-readable reason string on failure.

    Negative-spec: does NOT trust the wire-supplied inbox path directly — always resolves
    against the registry-enumerated set. A wire-supplied absolute path, ../ escape, or
    reference to an unregistered repo fails this check BEFORE any filesystem mutation.
    """
    resolved_inbox = target_inbox_dir.resolve()
    resolved_file = target_file.resolve()

    # Build allowed-set: filesystem-enumerated registered-receiver inbox dirs.
    allowed_inboxes = {
        Path(repo_path).resolve() / "cross-repo" / "inbox"
        for repo_path in all_repos.values()
        if repo_path
    }

    # Check 1: inbox dir is in the registry-enumerated allowed-set.
    if resolved_inbox not in allowed_inboxes:
        return (
            f"target inbox {resolved_inbox} is not a registry-enumerated receiver inbox "
            f"(registered inboxes: {sorted(str(p) for p in allowed_inboxes)})"
        )

    # Check 2: target file is within the inbox dir (no ../ escape within the inbox path).
    if not resolved_file.is_relative_to(resolved_inbox):
        return (
            f"target file {resolved_file} escapes inbox dir {resolved_inbox}"
        )

    return None  # pass


# ---------------------------------------------------------------------------
# in_reply_to — write-side support for the linkage field
# compute_reply_closure/_candidate_is_linked (coordinator_core.pickup_assemble)
# already reads (basename or basename-minus-.md, case-insensitive match).
# Prior to this, nothing wrote it — a reply's linkage depended entirely on the
# sender remembering to paste the inbound memo's filename into the body
# (CLAUDE.md § North star: "the operator remembers" is not a discharge).
# ---------------------------------------------------------------------------

def _normalize_in_reply_to(value: str) -> str:
    """Normalize a caller-supplied `in_reply_to` value to a bare basename.

    Accepts either a bare basename (`2026-07-25-foo.md`) or a path
    (`cross-repo/inbox/2026-07-25-foo.md`, an absolute path, etc.) — the
    emitted frontmatter value is always just the basename, matching what
    `_candidate_is_linked` matches against (basename or
    basename-minus-`.md`).
    """
    return Path(value.strip()).name


def _validate_in_reply_to_exists(
    dry_run: bool, sender_worktree: Optional[Path], in_reply_to: str,
) -> Optional[dict]:
    """Fail-loud gate: `in_reply_to` must name a memo THIS repo actually
    received (its own `cross-repo/inbox/` or `cross-repo/archive/`, the
    latter searched recursively — archive is nested by date).

    Runs BEFORE any receiver-tree write (mirrors the existing fail-loud style
    of the summary-cap and scoped_to-triple checks) — a typo'd `in_reply_to`
    is worse than none: an unresolvable value would read as closure evidence
    to `compute_reply_closure` while linking to nothing, so this rejects
    while it's still cheap to fix.

    `sender_worktree` is `None` when repo_root wasn't supplied (unwired-C3 /
    direct-in-process test path — see `_memo_send`'s own None-tolerant
    precedent for `_sender_worktree`) — there is no tree to search in that
    case, so an `in_reply_to` value fails loud rather than silently skipping
    the check it exists to enforce.

    Returns None on pass, else a build_setup_error_result envelope.

    Matching discipline (mirrors branch_resolution._resolve_in_reply_to_target,
    the just-narrowed sibling — see that function's own docstring for the
    perf/safety rationale): `in_reply_to` is untrusted, caller-supplied
    frontmatter — matching stays a literal filename comparison, NEVER a glob
    PATTERN (a value containing `*`/`?`/`[...]` must not spuriously match an
    unrelated archived memo). The value is normalized to a bare basename via
    `_normalize_in_reply_to` BEFORE it is joined onto `archive_dir`/`inbox_dir`
    — required, not optional, now that matching is path-joining rather than
    `rglob(pattern)`: an unnormalized value carrying directory separators
    (e.g. `../../etc/passwd`) would otherwise be a path-traversal vector that
    the previous `rglob()` form did not expose. The archive is flat on disk
    (1,003 files, zero subdirs, measured 2026-08-13) — a direct
    `archive_dir / basename` check plus a bounded one-level `iterdir()` over
    immediate subdirectories (defensive shard support) replaces the prior
    unbounded recursive walk.
    """
    if sender_worktree is None:
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.send: in_reply_to={in_reply_to!r} could not be verified — "
            f"no sender worktree was resolved (repo_root not supplied) to "
            f"search cross-repo/inbox/ or cross-repo/archive/ against.",
        )

    basename = _normalize_in_reply_to(in_reply_to)
    inbox_dir = sender_worktree / "cross-repo" / "inbox"
    archive_dir = sender_worktree / "cross-repo" / "archive"

    if (inbox_dir / basename).is_file():
        return None
    if archive_dir.is_dir():
        if (archive_dir / basename).is_file():
            return None
        for entry in archive_dir.iterdir():
            if entry.is_dir() and (entry / basename).is_file():
                return None

    return build_setup_error_result(
        _MODE, dry_run,
        f"memo.send: in_reply_to={in_reply_to!r} does not match any memo in "
        f"this repo's own {inbox_dir} or {archive_dir} — in_reply_to must "
        f"name a memo THIS repo actually received. "
        f"Check for a typo, or omit in_reply_to if this send isn't a reply.",
    )


# ---------------------------------------------------------------------------
# Param validation (memo.send-specific; does NOT reuse fleet _common.validate_params)
# ---------------------------------------------------------------------------

# C9 (A11) — the known-key allowlist that makes emission total: every key a
# caller may legally pass is enumerated here, and _validate_send_params rejects
# anything outside this set with exit_code:1. This is deliberately NOT an
# emission allowlist (the bug this closes) — it is a REJECTION allowlist. The
# two are opposite shapes: an emission allowlist silently drops what it doesn't
# recognize and returns exit_code:0; a rejection allowlist fails the whole send
# the moment it sees a key it doesn't recognize, so nothing declared here can
# ever silently vanish between accepted-param and emitted-frontmatter-field.
_KNOWN_PARAM_KEYS = frozenset({
    "dry_run", "topic", "to", "title", "body", "kind",
    "from_id", "summary", "supersedes", "scoped_to", "campaign_id",
    "in_reply_to", "space",
})

# scoped_to sub-keys — presence-triggered completeness (2026-07-21 fix, routed
# via cross-repo/inbox/2026-07-21-claude-central-em-debash-directive-cites-
# guard-plus-scoped-to-q.md): scoped_to as a WHOLE is optional (a directional /
# doctrine-establishing ask governs no versioned artifact and supplies none of
# these sub-keys at all); the moment the caller supplies ANY scoped_to_* field
# it is declaring a change-control memo, and the FULL triple —
# artifact + exactly one of (version|sha) + seam — becomes required. A partial
# triple (some sub-keys but not the complete shape) fails loud; it is never
# treated as "no scoped_to" nor silently completed.
#
# Negative-spec: does NOT gate this on `kind` (no `if kind in ("ask",
# "proposal")` branch) — the discriminator is presence of scoped_to content,
# not the memo's kind. A blanket kind-based requirement was rejected (see the
# routed memo) because it forces a directional ask to fabricate a version pin,
# mis-kind, or downgrade to `consult` just to satisfy the gate.
_SCOPED_TO_KNOWN_SUBKEYS = frozenset({"artifact", "version", "sha", "seam"})


@dataclasses.dataclass(frozen=True)
class SendParams:
    """Validated memo.send params (C9 — replaces the prior 9-tuple return shape).

    The 9-tuple was retired because adding a field (e.g. `scoped_to`) to a
    positional tuple is an arity refactor at every call site — a dataclass
    makes adding a field a one-line schema change instead (C9 spec point 3).
    """

    dry_run: bool
    topic: str
    to: str
    title: str
    body: str
    from_id: str
    kind: str
    summary: str
    supersedes: Optional[str | list[str]]
    scoped_to: Optional[dict] = None
    campaign_id: Optional[str] = None
    in_reply_to: Optional[str] = None
    space: Optional[str] = None
    # 2026-08-07 PM ruling (AC9, docs/plans/2026-08-07-memo-summary-cap-warn-
    # at-draft.md § C4): warn-and-substitute additive fields. Both None on a
    # clean/absent/placeholder summary. summary_cap_advisory carries the
    # over-cap message when an explicit summary was substituted for a
    # body-derived one; summary_over_cap_original carries the author's
    # original text VERBATIM (never truncated — see the substitution note at
    # the `summary` field's over-cap branch in _validate_send_params).
    summary_cap_advisory: Optional[str] = None
    summary_over_cap_original: Optional[str] = None


def _validate_scoped_to(dry_run: bool, value: Any):
    """Validate the optional `scoped_to` param; return None on pass, else an error envelope.

    Presence-triggered completeness (2026-07-21 fix — see the `_SCOPED_TO_KNOWN_SUBKEYS`
    comment above for the full rationale and the routed memo it answers): `scoped_to`
    as a whole is optional — a directional / doctrine-establishing ask passes with NO
    `scoped_to` at all. The moment the caller supplies the block, it must be the
    COMPLETE change-control triple: `artifact` (non-empty str, required),
    exactly one of `version`/`sha` (non-empty str, required — both together or
    neither is a fail-loud partial shape), and `seam` (non-empty str, required).
    Unknown sub-keys fail loud under the same rule as top-level unknown params
    (C9 spec point 4).

    Negative-spec: does NOT accept a partial triple (e.g. `artifact` alone, or
    `artifact` + `version` with no `seam`) as "good enough" — an incomplete pin
    is exactly the shape this gate exists to reject; it is never coerced into
    either "treat as absent" or "treat as complete".
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.send: scoped_to must be a mapping, got {type(value).__name__}",
        )
    unknown_subkeys = set(value.keys()) - _SCOPED_TO_KNOWN_SUBKEYS
    if unknown_subkeys:
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.send: scoped_to has unrecognized sub-key(s) {sorted(unknown_subkeys)} "
            f"— known sub-keys: {sorted(_SCOPED_TO_KNOWN_SUBKEYS)}",
        )
    for subkey in _SCOPED_TO_KNOWN_SUBKEYS:
        sub_val = value.get(subkey)
        if sub_val is not None and not isinstance(sub_val, str):
            return build_setup_error_result(
                _MODE, dry_run,
                f"memo.send: scoped_to.{subkey} must be a string, got "
                f"{type(sub_val).__name__}",
            )
    artifact = value.get("artifact")
    if not artifact:
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.send: scoped_to.artifact is required (non-empty string) "
            "whenever scoped_to is present — a memo declaring scoped_to at all "
            "is a change-control memo and must carry the complete triple "
            "(artifact + exactly one of version|sha + seam), or omit scoped_to "
            "entirely for a directional ask",
        )
    seam = value.get("seam")
    if not seam:
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.send: scoped_to.seam is required (non-empty string) "
            "whenever scoped_to is present — see scoped_to.artifact error for "
            "the complete-triple rationale",
        )
    version = value.get("version")
    sha = value.get("sha")
    if bool(version) == bool(sha):
        # Neither present, or both present — either way this is not
        # "exactly one of version|sha".
        reason = "neither version nor sha was supplied" if not version and not sha \
            else "both version and sha were supplied"
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.send: scoped_to requires exactly one of version|sha ({reason}) "
            f"— see scoped_to.artifact error for the complete-triple rationale",
        )
    return None


# ---------------------------------------------------------------------------
# space / supersedes — shared param validation (memo.send AND memo.draft)
#
# Review: code-reviewer (Finding 2, slice 1) — memo_draft._validate_draft_params
# duplicated this validation near-verbatim (same shape check, same list
# normalization, same "fail loud rather than prune" rule) with its own,
# non-overlapping test coverage. Factored out here (memo_send.py is already
# memo_draft's shared-helper home — see the module's other cross-imports:
# _render_extra_field, _yaml_quote, _normalize_in_reply_to, _SUMMARY_MAX_CHARS)
# so there is exactly one implementation of each rule to test and keep
# correct. Both callers build their own build_setup_error_result envelope
# from the returned error string so each retains its own op-namespaced
# message prefix ("memo.send: ..." vs "memo.draft: ...") — see op_mode below.
# ---------------------------------------------------------------------------

def _validate_space_param(op_mode: str, value: Any, dry_run: bool):
    """Validate/normalize the optional `space` param — shared by memo.send
    and memo.draft.

    `space` (2026-07-28) is a sender-declared thread/problem-space hint,
    deliberately unvalidated against any vocabulary: it is a grouping hint
    the receiver may override, not a taxonomy. Only the "non-empty string
    when supplied" shape check applies (mirrors campaign_id's posture).

    Args:
        op_mode: the caller's own `_MODE` constant ("send" or "draft") — used
            both as the `build_setup_error_result` mode field and to compose
            the op-namespaced message prefix ("memo.<op_mode>: ...").
        value: the raw `space` param (params.get("space")).
        dry_run: passed straight through to build_setup_error_result.

    Returns:
        (normalized_value_or_None, error_envelope_or_None) — normalized_value
        is the stripped string on pass (or None when value was None/absent);
        error_envelope is a build_setup_error_result dict on failure, else
        None. Exactly one of the two return slots is non-None.
    """
    if value is None:
        return None, None
    if not isinstance(value, str) or not value.strip():
        return None, build_setup_error_result(
            op_mode, dry_run,
            f"memo.{op_mode}: space must be a non-empty string when supplied",
        )
    return value.strip(), None


def _validate_supersedes_param(op_mode: str, supersedes_raw: Any, dry_run: bool):
    """Validate/normalize the optional `supersedes` param — shared by
    memo.send and memo.draft.

    `supersedes` accepts a bare string (the original shape) or a list of
    references (widened 2026-07-28 — one memo can retire several earlier
    ones, the observed shape of a thread that ends in a correction).

    Unified rule (EM correction, 2026-07-28 — the two pre-extraction callers
    disagreed on the bare-string branch; this was an authoring-time oversight,
    not a deliberate divergence, so it is unified rather than preserved):

      - A bare BLANK/whitespace-only string is treated as ABSENT — normalizes
        to `None`, no error. An empty scalar naturally means "no
        supersession"; this is the lenient (memo.send-originated) reading,
        loosening memo.draft's prior strict rejection, which is the safe
        direction (fewer callers newly break).
      - A blank or non-string ENTRY INSIDE A NON-EMPTY LIST still fails loud,
        with the index in the message. That is not absence, it is a
        malformed list — silently pruning it would leave a live ask looking
        retired, which is the whole reason this rule exists.
      - A non-string, non-list `supersedes` (e.g. an int) still fails loud in
        both paths.

    A single-element list collapses to the bare string so every downstream
    consumer (filename disambiguation, frontmatter rendering) sees the
    pre-existing shape unchanged.

    Args:
        op_mode: the caller's own `_MODE` constant ("send" or "draft") — see
            `_validate_space_param` for the same convention.
        supersedes_raw: the raw `supersedes` param.
        dry_run: passed straight through to build_setup_error_result.

    Returns:
        (normalized_value_or_None, error_envelope_or_None) — same two-slot
        contract as `_validate_space_param`.
    """
    supersedes: Optional[str | list[str]] = None
    if isinstance(supersedes_raw, list):
        cleaned: list[str] = []
        for idx, entry in enumerate(supersedes_raw):
            if not isinstance(entry, str) or not entry.strip():
                return None, build_setup_error_result(
                    op_mode, dry_run,
                    f"memo.{op_mode}: supersedes[{idx}] must be a non-empty "
                    f"string (got {entry!r}) — a supersession list is never "
                    f"silently pruned; fix or drop the entry",
                )
            cleaned.append(entry.strip())
        if cleaned:
            supersedes = cleaned[0] if len(cleaned) == 1 else cleaned
    elif supersedes_raw is not None:
        if not isinstance(supersedes_raw, str):
            return None, build_setup_error_result(
                op_mode, dry_run,
                f"memo.{op_mode}: supersedes must be a string or a list of "
                f"strings, got {type(supersedes_raw).__name__}",
            )
        # Blank/whitespace-only bare string is ABSENCE, not an error — see
        # unified rule above.
        supersedes = supersedes_raw.strip() or None
    return supersedes, None


def _validate_send_params(params: dict):
    """Validate memo.send params; return a SendParams or a build_setup_error_result dict.

    Required params: dry_run (bool), topic (slug), to (str — the CALLER of this
    function is always the single-receiver path; a list-shaped `to` is intercepted
    and routed to `_memo_send_fan_out` BEFORE this validator ever runs, see
    `_memo_send`), title (str), body (str), kind (str — presence is enforced here;
    ENUM MEMBERSHIP against _VALID_KINDS is validated later, in _compose_memo's
    self-validation — see Review: code-reviewer Finding 5, the false-green this
    workstream fixed traced back to exactly this presence/enum-membership split),
    summary (str, non-empty — DEC-1, 2026-07-24 memo-ownership-and-redesign plan;
    a send-time gate only, not a schema change).
    Optional params: from_id, supersedes, scoped_to, campaign_id (str, non-empty
    when supplied — DEC-3/C7, threaded to `_compose_memo` and persisted to disk
    when present; single-receiver callers may pass it directly, and the fan-out
    path always supplies one).

    Returns a SendParams instance on success, or an exit_code:1 setup-error
    envelope dict on any validation failure — including a key not in
    `_KNOWN_PARAM_KEYS` (C9 A11: fail loud on unknown params rather than
    silently dropping them from the composed frontmatter).
    """
    dry_run = params.get("dry_run")
    if not isinstance(dry_run, bool):
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.send: dry_run must be bool, got "
            + repr(type(dry_run).__name__),
        )

    # C9 (A11) — reject any param key this handler does not declare, BEFORE
    # any other validation. This is what converts a silent drop into a loud
    # failure: a key that would previously survive params.get()-based
    # extraction unnoticed now stops the send outright.
    unknown_keys = set(params.keys()) - _KNOWN_PARAM_KEYS
    if unknown_keys:
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.send: unrecognized param(s) {sorted(unknown_keys)} — "
            f"known params: {sorted(_KNOWN_PARAM_KEYS)}. A param must be "
            f"declared to be accepted; it is never silently dropped.",
        )

    topic = params.get("topic")
    if not topic or not isinstance(topic, str):
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.send: topic is required (non-empty string)",
        )
    if not _TOPIC_SLUG_RE.fullmatch(topic):
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.send: topic {topic!r} is invalid — must match [a-z0-9][a-z0-9-]* "
            f"(lowercase alphanum and hyphens only, starting with alphanum). "
            f"Path chars (/, .., absolute paths) are not permitted.",
        )

    to = params.get("to")
    if not to or not isinstance(to, str):
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.send: to (receiver EM identity) is required (non-empty string)",
        )

    title = params.get("title")
    if not title or not isinstance(title, str):
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.send: title is required (non-empty string)",
        )

    body = params.get("body")
    if not isinstance(body, str):
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.send: body is required (string; empty string is permitted for body-less memos)",
        )

    from_id: str = resolve_sender_id(params.get("from_id"))

    # Review: code-reviewer — kind: is required per DR-214 D4 and D2-6 affirmation;
    # previously accepted as Optional but that left the handler emitting schema-invalid
    # memos (D2 criterion 6 requires kind: in frontmatter). Resolve by ENFORCEMENT.
    kind = params.get("kind")
    if not kind or not isinstance(kind, str):
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.send: kind is required (non-empty string — ask | consult | fyi | proposal)"
            " — DR-214 D4 and D2-6 affirmation require kind: in memo frontmatter",
        )

    # DEC-1 (2026-07-24 memo-ownership-and-redesign plan) — summary is now
    # UNCONDITIONALLY required at send time (present + non-empty), mirroring
    # kind's existing presence gate above. This is a SEND-TIME gate only —
    # it does not touch receiver-side validation or the schema `required`
    # array (existing on-disk memos lacking summary still validate/action).
    # Omit-and-derive via memo.send is retired: a caller must supply an
    # explicit, non-empty summary. derive_prose_summary/_compose_memo's own
    # None-fallback stays as defense-in-depth for direct (non-op) callers of
    # _compose_memo, e.g. tests, which bypass this validator entirely.
    summary_raw = params.get("summary")
    if not summary_raw or not isinstance(summary_raw, str):
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.send: summary is required (non-empty string, <= "
            f"{_SUMMARY_MAX_CHARS} chars) — DEC-1 makes summary a send-time "
            "required field alongside kind; omit-and-derive is no longer "
            "permitted through memo.send",
        )
    # A placeholder-valued summary (memo.draft's self-measuring ruler) is
    # ABSENT, not an explicit value — sentinel to None so it reaches
    # `_compose_memo`'s `if summary is None` derivation branch rather than
    # the length-check/emit path (AC5, docs/plans/2026-08-07-memo-summary-
    # cap-warn-at-draft.md § C3). The ruler can never reach a delivered memo.
    summary_cap_advisory: Optional[str] = None
    summary_over_cap_original: Optional[str] = None
    if is_placeholder_summary(summary_raw):
        summary: Optional[str] = None
    else:
        # 2026-08-07 PM ruling (AC9, supersedes AC7 — docs/plans/2026-08-07-
        # memo-summary-cap-warn-at-draft.md § C4): an over-cap EXPLICITLY
        # authored summary no longer refuses the send — it WARNS and
        # SUBSTITUTES the body-derived summary in its place, echoing the
        # author's original text back verbatim (never truncated — see
        # Anti-scope: substitution is not truncation). The 2026-07-22
        # body-drop invariant survives unchanged: `summary` below is set to
        # None (never to a clamped/truncated value) so `_compose_memo`'s own
        # `if summary is None` branch derives it from body, exactly as if the
        # caller had omitted summary entirely.
        #
        # If the body has no derivable prose either, substitution has
        # nothing to substitute — refuse rather than let an EMPTY summary
        # reach `_compose_memo` (a naive substitution would: unlike
        # memo.compose's DEC-1 gate, `_self_validate_frontmatter_fields`
        # below checks `summary is None`, not `summary == ""`, so a derived
        # empty string would otherwise sail through onto a DELIVERED memo —
        # a new defect this chunk closes rather than introduces).
        error = validate_explicit_summary("send", summary_raw)
        if error:
            derived = derive_prose_summary(body)
            if not derived:
                return build_setup_error_result(
                    _MODE, dry_run,
                    f"{error} — and the body has no derivable prose sentence "
                    "to substitute in its place (DEC-1 requires a non-empty "
                    "summary before a memo can be sent; over-cap + "
                    "underivable-body cannot both be true and still deliver).",
                )
            summary_cap_advisory = error
            summary_over_cap_original = summary_raw
            summary = None  # substitute: _compose_memo derives from body
        else:
            summary = summary_raw
    # supersedes / space (2026-07-28) — shared validation, see
    # _validate_supersedes_param / _validate_space_param above.
    supersedes, supersedes_error = _validate_supersedes_param(
        _MODE, params.get("supersedes"), dry_run,
    )
    if supersedes_error is not None:
        return supersedes_error

    space, space_error = _validate_space_param(_MODE, params.get("space"), dry_run)
    if space_error is not None:
        return space_error

    scoped_to = params.get("scoped_to")
    scoped_to_error = _validate_scoped_to(dry_run, scoped_to)
    if scoped_to_error is not None:
        return scoped_to_error

    # DEC-3/C7 — campaign_id is optional; when supplied it must be a non-empty
    # string. The fan-out path (_memo_send_fan_out) always supplies one; a
    # direct single-receiver caller may also pass one explicitly.
    campaign_id = params.get("campaign_id")
    if campaign_id is not None and (
        not isinstance(campaign_id, str) or not campaign_id.strip()
    ):
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.send: campaign_id must be a non-empty string when supplied",
        )

    # in_reply_to (2026-07-25 write-side addition — the reader,
    # pickup_assemble._candidate_is_linked, already accepted this field; this
    # is the first writer). Optional; when supplied, normalized to its
    # BASENAME here (a caller may pass either a bare basename or a full path —
    # see _normalize_in_reply_to). Existence of the named memo in THIS repo's
    # own cross-repo/{inbox,archive}/ is verified later in _memo_send (that
    # check needs the sender worktree, which this param-only validator does
    # not have) — see the in_reply_to existence gate there.
    in_reply_to_raw = params.get("in_reply_to")
    in_reply_to: Optional[str] = None
    if in_reply_to_raw is not None:
        if not isinstance(in_reply_to_raw, str) or not in_reply_to_raw.strip():
            return build_setup_error_result(
                _MODE, dry_run,
                "memo.send: in_reply_to must be a non-empty string when supplied",
            )
        in_reply_to = _normalize_in_reply_to(in_reply_to_raw)

    return SendParams(
        dry_run=dry_run,
        topic=topic,
        to=to,
        title=title,
        body=body,
        from_id=from_id,
        kind=kind,
        summary=summary,
        supersedes=supersedes,
        scoped_to=scoped_to,
        campaign_id=campaign_id,
        in_reply_to=in_reply_to,
        space=space,
        summary_cap_advisory=summary_cap_advisory,
        summary_over_cap_original=summary_over_cap_original,
    )


# ---------------------------------------------------------------------------
# DR-026 sender-namespaced receiver filename
# ---------------------------------------------------------------------------

def _sender_slug(sender: str) -> str:
    """Slug-sanitize a sender identity for filename namespacing (DR-026).

    Byte-for-byte port of DoE cross-repo-memo._memo_filename's sanitization:
    lowercase, collapse any run of non-[a-z0-9-] chars to a single dash,
    collapse consecutive dashes, strip leading/trailing dashes.

    Spec backlink: DR-026 (DoE-claude docs/decisions/DR-026-cross-repo-memo-
    receiver-filename-namespace.md); DoE coordinator/bin/cross-repo-memo.py
    _memo_filename (~line 1410-1412).
    """
    if not sender:
        return ""
    return _SENDER_SLUG_RUN_DASH_RE.sub(
        "-", _SENDER_SLUG_INVALID_RE.sub("-", sender.lower())
    ).strip("-")


def _memo_filename(today: str, sender: str, topic: str) -> str:
    """Compose the DR-026 sender-namespaced receiver filename: <date>-<sender>-<topic>.md.

    DR-026: folds the sender into the receiver filename so N-repo broadcast
    replies with an identical topic slug on the same day do not collide
    (cross-sender both survive; same-sender still fails loud via the existing
    O_EXCL guard in _write_memo_file — this function only changes the
    pre-collision filename shape, not the collision semantics).

    Also ports DoE's doubled-date-prefix strip: a topic may already carry a
    leading YYYY-MM-DD- prefix (e.g. reused from a prior dated filename) —
    strip a RUN of leading date prefixes before prepending today's date, so
    the result is never a doubled <date>-<date>-<topic>.md.

    Negative-spec / deviation from DoE: DoE's _memo_filename falls back to a
    bare <date>-<topic>.md when the sanitized sender reduces to empty (its
    "defensive empty-sender fallback"). This port does NOT replicate that
    fallback — memo.send's from_id always resolves to a non-empty default
    (_ENGINE_ACTOR_ID) before this function is called, so a sender that
    sanitizes to empty here means a caller-supplied from_id consisting
    entirely of punctuation/non-ASCII chars. Silently degrading to the
    pre-DR-026 filename shape in that case would silently defeat the
    namespacing guarantee this port exists to provide; failing loud instead
    surfaces the malformed from_id to the caller.

    Raises:
        ValueError: if sender sanitizes to an empty slug (see deviation note above).
    """
    sanitized = _sender_slug(sender)
    if not sanitized:
        raise ValueError(
            f"from_id {sender!r} sanitizes to an empty sender slug — a "
            f"DR-026 namespaced filename requires a non-empty sender identity."
        )
    stripped_topic = _TOPIC_DOUBLED_DATE_PREFIX_RE.sub("", topic)
    return f"{today}-{sanitized}-{stripped_topic}.md"


def _supersedes_ref_basename(supersedes_ref: str) -> str:
    """Reduce a `supersedes:` reference to its filename-safe identifying stem.

    `supersedes:` accepts either a bare topic slug or a path to the predecessor
    memo (absolute paths are the common shape — that is what the delivered file
    is addressed by). Only the predecessor's basename identifies it; the
    directory prefix is sender-machine layout, and both POSIX `/` and Windows
    `\\` separators reach here (a Windows sender's reference is not a POSIX path,
    so `PurePosixPath` alone would keep the whole drive-qualified string).

    Also strips the `.md` suffix and the leading `YYYY-MM-DD-` run: the
    predecessor of a same-date re-delivery carries today's date already, and
    repeating it in the disambiguator adds length without adding identity.

    Spec backlink: `_redelivery_filename` negative-spec (2026-08-17
    project-opticon-em report — absolute-path leak into the receiver's tree).
    """
    if not supersedes_ref:
        return ""
    tail = re.split(r"[\\/]", supersedes_ref.strip())[-1]
    if tail.lower().endswith(".md"):
        tail = tail[: -len(".md")]
    return _TOPIC_DOUBLED_DATE_PREFIX_RE.sub("", tail)


# Disambiguator budget: the base DR-026 name is already <date>-<sender>-<topic>,
# and a predecessor basename repeats that shape almost verbatim, so an uncapped
# disambiguator roughly doubles the basename — 161 chars for a real send, against
# a 260-char Windows path ceiling that the receiver's inbox prefix eats into.
# Truncation stays deterministic (same reference -> same name) and can only ever
# make two distinct predecessors converge, which the residual-collision check
# refuses fail-loud — the safe direction, never a silent clobber.
_SUPERSEDES_SLUG_MAX_CHARS = 48


def _truncate_supersedes_slug(slug: str) -> str:
    """Cap a supersedes disambiguator at `_SUPERSEDES_SLUG_MAX_CHARS`, on a dash
    boundary where one is available so the tail stays a readable word run.
    """
    if len(slug) <= _SUPERSEDES_SLUG_MAX_CHARS:
        return slug
    clipped = slug[:_SUPERSEDES_SLUG_MAX_CHARS]
    boundary = clipped.rfind("-")
    if boundary > _SUPERSEDES_SLUG_MAX_CHARS // 2:
        clipped = clipped[:boundary]
    return clipped.rstrip("-")


def _redelivery_filename(
    today: str, sender: str, topic: str, supersedes: str | list[str]
) -> str:
    """Sanctioned supersedes: re-delivery filename (C6, footgun #5, A6).

    Same DR-026 `<date>-<sender>-<topic>.md` shape as `_memo_filename`, with the
    `topic` segment disambiguated by a slug of the `supersedes` reference itself
    (e.g. `<date>-<sender>-<topic>--supersedes-<supersedes-slug>.md`). This is
    what lets a same-date+same-topic re-send land as a FRESH file (PM decision
    2026-07-21, A6) rather than the hand-edit-the-receiver workaround — WITHOUT
    an in-place `--force`/`--replace` clobber and WITHOUT touching the DR-026
    filename contract itself (still `<date>-<sender>-<topic...>.md`, still
    O_EXCL-guarded, still fails loud on any further collision).

    Negative-spec: the disambiguator is the caller-declared `supersedes` value,
    NOT a nonce or content-hash — `_write_memo_file`'s existing "no nonce/hash
    suffix" negative-spec is about the UNCONDITIONAL collision path (no
    `supersedes:` declared) and is untouched by this opt-in mechanism; a
    `supersedes:` reference is a meaningful, traceable pointer to the memo it
    replaces, not an arbitrary disambiguator.

    Callers MUST check this path for a *further* collision themselves — two
    re-deliveries superseding the identical prior memo on the same day would
    otherwise collide here too, and per the module negative-spec that residual
    collision still refuses fail-loud rather than layering on a second
    disambiguator.

    Negative-spec (2026-08-17, project-opticon-em cross-repo report): the
    disambiguator is derived from the predecessor's BASENAME, never the raw
    reference. `supersedes:` legitimately accepts an absolute path, and slugging
    that verbatim wrote the sender's own `/Users/<name>/...` layout into a peer
    repo's committed tree and produced a 190-char basename hostile to Windows'
    260-char path ceiling. Basename-derivation keeps the pointer traceable
    without exporting sender-machine layout; do NOT reintroduce whole-reference
    slugging.
    """
    base = _memo_filename(today, sender, topic)
    # List form (2026-07-28): disambiguate on the FIRST reference only. Slugging
    # the whole list would produce an unbounded filename that grows with the
    # thread; the first reference stays a meaningful, traceable pointer, and the
    # residual-collision refusal below is unchanged either way.
    supersedes_ref = supersedes[0] if isinstance(supersedes, list) else supersedes
    supersedes_slug = _truncate_supersedes_slug(
        _sender_slug(_supersedes_ref_basename(supersedes_ref))
    )
    if not supersedes_slug:
        return base
    stem = base[: -len(".md")]
    return f"{stem}--supersedes-{supersedes_slug}.md"


# ---------------------------------------------------------------------------
# Memo composition — inline, no DoE CLI import
# ---------------------------------------------------------------------------

def _yaml_quote(value: str) -> str:
    """Double-quote a string for YAML, escaping backslashes, double-quotes, control chars.

    Mirrors memo_compose._yaml_quote (DoE shared lib, bin/lib/memo_compose.py).
    Inlined here to avoid a cross-repo import dependency while the DoE resolver
    surface is pending. Both implementations must stay in sync with the memo schema.

    Sync note: this copy adds ASCII control-char escaping (0x00-0x08, 0x0B, 0x0C,
    0x0E-0x1F, 0x7F → \\uXXXX) that the DoE memo_compose._yaml_quote may lack —
    if DoE's copy is updated to fix the same gap, re-sync the two implementations.
    Review: code-reviewer — NUL and other bare control chars produce invalid YAML 1.1
    double-quoted strings; escaped to \\uXXXX form.

    Negative-spec: ALWAYS wraps in double-quotes (never bare YAML) — memo frontmatter
    requires unambiguous quoting. Do not switch to bare YAML or single-quote form.
    """
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    # Escape remaining ASCII control chars (0x00-0x08, 0x0B, 0x0C, 0x0E-0x1F, 0x7F)
    # that are invalid in YAML 1.1 double-quoted strings.
    escaped = re.sub(
        r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]",
        lambda m: f"\\u{ord(m.group()):04x}",
        escaped,
    )
    return f'"{escaped}"'


def _yaml_scalar(value: Any) -> str:
    """Render a leaf scalar (str/bool/int/float/None) for YAML — the leaf case
    the structural renderer below bottoms out at.

    Strings recurse through `_yaml_quote` (the always-double-quote scalar
    renderer, unchanged by C9). Non-string scalar types get their normal YAML
    literal spelling — booleans/null are lowercase, numbers are bare.
    """
    if isinstance(value, str):
        return _yaml_quote(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return repr(value)
    # Review: code-reviewer — the caller (_memo_send's try/except around
    # _compose_memo) depends on catching this TypeError alongside ValueError;
    # a future extra field added without equally strict pre-validation could
    # reach this branch, so the catch-site coupling is noted here to keep it
    # visible.
    raise TypeError(
        f"memo.send: cannot render {value!r} ({type(value).__name__}) as a "
        f"YAML scalar — unsupported extra-field value type."
    )


def _render_yaml_block(value: Any, indent: int) -> list[str]:
    """Render dict/list `value` as indented YAML block lines, recursing to
    `_yaml_scalar` at the leaves.

    Sibling structural renderer to `_yaml_quote` (C9 A11 point 2): `_yaml_quote`
    stays the SCALAR renderer (its always-double-quote negative-spec is correct
    for scalars and is not touched here); this function is what lets a nested
    mapping (e.g. `scoped_to: {artifact, version, seam}`) round-trip as a real
    YAML mapping instead of being forced through `_yaml_quote` into a single
    double-quoted scalar string (the structural gap the C9 memo names).
    """
    pad = " " * indent
    lines: list[str] = []
    if isinstance(value, dict):
        for key, sub_value in value.items():
            if isinstance(sub_value, (dict, list)):
                lines.append(f"{pad}{key}:")
                lines.extend(_render_yaml_block(sub_value, indent + 2))
            else:
                lines.append(f"{pad}{key}: {_yaml_scalar(sub_value)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                nested = _render_yaml_block(item, indent + 2)
                lines.append(f"{pad}-")
                lines.extend(nested)
            else:
                lines.append(f"{pad}- {_yaml_scalar(item)}")
    return lines


def _render_extra_field(key: str, value: Any) -> str:
    """Render one declared-extra frontmatter field (e.g. `scoped_to`) as a
    single multi-line YAML fragment, dict/list values as a nested block,
    scalars inline on the `key:` line.
    """
    if isinstance(value, (dict, list)):
        block_lines = _render_yaml_block(value, 2)
        return "\n".join([f"{key}:"] + block_lines)
    return f"{key}: {_yaml_scalar(value)}"


# kind enum — mirrors DoE cross-repo-memo._VALID_KINDS (~line 1774).
_VALID_KINDS = ("ask", "consult", "fyi", "proposal")

# Single source of truth for the two frontmatter literals that matter most for
# receiver-lifecycle correctness. _compose_memo's self-validation call and its
# emitted `lines` list both reference these constants (never separate literals)
# so the validated values and the emitted values cannot silently diverge.
# Review: patrik — decoupled-literal finding; status/delivery_mode were previously
# validated via parallel literals independent of what was actually emitted.
_STATUS_OPEN = "open"
_DELIVERY_MODE_RECEIVER_REPO = "receiver-repo"


def _self_validate_frontmatter_fields(
    *,
    title: str,
    from_id: str,
    to: str,
    created: str,
    status: str,
    delivery_mode: str,
    summary: Optional[str],
    kind: Optional[str],
) -> list[str]:
    """Defense-in-depth frontmatter self-check before write (invariant b).

    The engine bypasses the session-side PreToolUse Write hook that would
    otherwise validate outgoing memo frontmatter against the (DoE-owned,
    NOT vendored here) cross-repo memo schema — so memo.send must self-
    enforce the required-field shape before every write.

    Mirrors DoE cross-repo-memo._validate_outbox_frontmatter's field-presence
    semantics (~line 1777-1815): title/from/to/created/delivery_mode must be
    non-empty; status must literally equal "open" (this is always a receiver-
    side delivery memo — never a draft, so DoE's "draft" acceptance does not
    apply here); summary's KEY must be present but MAY be empty (mirrors
    DoE's allowance that summary can be present-but-empty — memo.send permits
    an empty body, which derives to an empty summary); kind is valid-or-absent
    against the DR-214/D2-6 enum (in practice memo.send always supplies a
    non-empty kind — _validate_send_params requires it — so "absent" is not
    reachable through the handler; the absent branch exists for direct callers
    of _compose_memo, e.g. tests).

    Returns a list of error strings; empty list = valid.
    """
    errors: list[str] = []
    for field_name, value in (
        ("title", title),
        ("from", from_id),
        ("to", to),
        ("created", created),
        ("delivery_mode", delivery_mode),
    ):
        if not value:
            errors.append(f"required field '{field_name}' missing or empty")
    if summary is None:
        errors.append("required field 'summary' missing")
    if status != "open":
        errors.append(f"status must be 'open', got: {status!r}")
    if kind is not None and kind not in _VALID_KINDS:
        errors.append(
            f"kind {kind!r} is not a valid enum value "
            f"(must be one of: {', '.join(_VALID_KINDS)}; absent is also valid)"
        )
    return errors


def _compose_memo(
    *,
    from_id: str,
    to: str,
    topic: str,
    title: str,
    body: str,
    kind: str,
    summary: Optional[str],
    supersedes: Optional[str | list[str]],
    today: str,
    scoped_to: Optional[dict] = None,
    campaign_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    space: Optional[str] = None,
    sent_by: Optional[str] = None,
) -> str:
    """Compose a schema-valid cross-repo memo document (frontmatter + body).

    Schema-valid: to: / from: / status: open / delivery_mode: receiver-repo / kind:
    frontmatter per the cross-repo memo schema (D2 criterion 6, DoE Ask-1
    concurrence condition 1). topic lives in the filename, NOT in frontmatter
    (same as cross-repo-memo CLI convention).

    Review: code-reviewer — kind: is now a required parameter (str, not Optional);
    D2-6 affirmation requires kind: present in every schema-valid emission.
    today: is passed in from _memo_send (single datetime.date.today() call) so the
    filename date and created: frontmatter field cannot diverge across midnight.

    Divergence note (C9, supersedes the prior "keep in sync with memo_compose"
    sync note): after C9 this composer is DELIBERATELY AHEAD of DoE's
    memo_compose — total emission over declared params, fail-loud on unknown
    params, and nested-mapping support for `scoped_to` are makima-owned
    ergonomic divergences (A11), not a mirror to keep byte-identical with DoE's
    copy. The nine canonical fields below keep their CURRENT fixed order and
    quoting (DR-026 / schema lockstep + the strang-03 round-trip fixture both
    depend on it) — `scoped_to` renders strictly AFTER `kind:`/`supersedes:`.

    Negative-spec: status is ALWAYS 'open' (never 'actioned', 'draft', or 'closed') —
    this is a delivery memo, not a self-receipt. Self-receipt is out of scope for memo.send.

    campaign_id (DEC-3/C7, optional, additive): when supplied, renders as its own
    frontmatter line AFTER `supersedes:` and BEFORE `scoped_to:` — the fixed nine-field
    core above is untouched. Never validated for shape beyond non-empty-string (enforced
    in `_validate_send_params`/`_memo_send_fan_out`, not here) — this composer only
    renders what it is given.

    in_reply_to (2026-07-25, optional, additive): when supplied, renders as its
    own frontmatter line AFTER `campaign_id:` and BEFORE `scoped_to:` — the
    value this composer receives has already been normalized to a bare
    basename and existence-checked against the sender's own
    cross-repo/{inbox,archive}/ by `_validate_send_params`/`_memo_send`
    (`_normalize_in_reply_to` / `_validate_in_reply_to_exists`); this composer
    only renders what it is given. Consumed by
    `coordinator_core.pickup_assemble._candidate_is_linked` (basename or
    basename-minus-`.md`, case-insensitive match).

    sent_by (C7, docs/plans/2026-08-13-session-identity-earns-its-keep.md):
    when supplied, renders as its own frontmatter line AFTER `in_reply_to:`
    and BEFORE `scoped_to:` — mirrors `picked_up_by` on the receive path.
    Resolved by the CALLER (`_memo_send`, via `_resolve_sent_by`) at SEND
    time — this composer never resolves session identity itself, same
    negative-spec as every other identity-bearing field it only renders.
    Optional in the schema (never required) — omitted entirely when falsy.
    """
    # today is caller-supplied — single datetime.date.today() call in _memo_send prevents
    # filename date / created: field divergence across midnight (Review: code-reviewer F3).

    # Derive summary via the shared prose-first rule (footgun #4) when not
    # provided — skips ATX headings/blank/HTML-comment lines and takes the
    # first prose sentence, same derivation memo.compose uses, so the two
    # paths stay consistent (a body opening with a Markdown heading no
    # longer emits the literal `# Heading` line as the summary).
    # A placeholder-valued summary reaching this defense-in-depth backstop
    # (e.g. a direct caller of _compose_memo that bypasses
    # _validate_send_params) is ABSENT, not an explicit value — sentinel to
    # None so it falls into the `if summary is None` derivation branch below
    # rather than the length-check one (AC5/AC7, docs/plans/2026-08-07-memo-
    # summary-cap-warn-at-draft.md § C3).
    if summary is not None and is_placeholder_summary(summary):
        summary = None
    if summary is None:
        summary = derive_prose_summary(body)
    else:
        # Fail loud, never truncate an EXPLICITLY authored summary (2026-07-22
        # body-drop verdict memo — see the module-level docstring backlink and
        # _validate_send_params, which is the primary gate a normal
        # memo.send call goes through and returns a build_setup_error_result
        # BEFORE this function is ever reached with an over-cap explicit
        # summary). This raise is the defense-in-depth backstop for direct
        # callers of _compose_memo (e.g. tests) that bypass
        # _validate_send_params — it deliberately replaces the former
        # `summary[:_SUMMARY_MAX_CHARS - 1] + "…"` silent clamp, which is the
        # exact defect this memo root-caused (an explicitly-authored 120-char
        # summary silently clamped mid-sentence on the delivered memo).
        error = validate_explicit_summary("send_backstop", summary)
        if error:
            raise ValueError(error)

    # Invariant b — self-validate before composing (defense-in-depth; the engine
    # bypasses the session-side PreToolUse Write hook, so nothing else checks this).
    fm_errors = _self_validate_frontmatter_fields(
        title=title,
        from_id=from_id,
        to=to,
        created=today,
        status=_STATUS_OPEN,
        delivery_mode=_DELIVERY_MODE_RECEIVER_REPO,
        summary=summary,
        kind=kind,
    )
    if fm_errors:
        raise ValueError(
            "memo.send: composed frontmatter failed self-validation: "
            + "; ".join(fm_errors)
        )

    lines = [
        "---",
        f"title: {_yaml_quote(title)}",
        f"from: {_yaml_quote(from_id)}",
        f"to: {_yaml_quote(to)}",
        f"created: {today}",
        f"status: {_STATUS_OPEN}",
        f"delivery_mode: {_DELIVERY_MODE_RECEIVER_REPO}",
        f"summary: {_yaml_quote(summary)}",
        f"kind: {_yaml_quote(kind)}",   # required field — D2-6 (Review: code-reviewer F1)
    ]
    if supersedes:
        # List form (2026-07-28) renders through _render_extra_field as a real
        # nested YAML sequence — never _yaml_quote'd into one scalar string,
        # which would round-trip as a single bogus reference rather than N.
        # The string form keeps its exact pre-existing single-line shape.
        if isinstance(supersedes, list):
            lines.append(_render_extra_field("supersedes", supersedes))
        else:
            lines.append(f"supersedes: {_yaml_quote(supersedes)}")
    if space:
        lines.append(f"space: {_yaml_quote(space)}")
    if campaign_id:
        lines.append(f"campaign_id: {_yaml_quote(campaign_id)}")
    if in_reply_to:
        lines.append(f"in_reply_to: {_yaml_quote(in_reply_to)}")
    if sent_by:
        lines.append(f"sent_by: {_yaml_quote(sent_by)}")
    if scoped_to:
        lines.append(_render_extra_field("scoped_to", scoped_to))
    lines.append("---")
    frontmatter = "\n".join(lines) + "\n"
    return frontmatter + "\n" + body.rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# Cross-tree file-write helper — the O_EXCL write itself is non-committing;
# the COMMIT is a distinct step performed after it by _commit_delivered_memo
# (see that function). The op as a WHOLE now commits (DR-211 D2 criterion 3
# retired for send, PM directive 2026-07-21) — only this file-write primitive
# stays a bare, uncommitted create.
# ---------------------------------------------------------------------------

def _write_memo_file(target_path: Path, content: str) -> None:
    """Write memo content to target_path with O_EXCL-style exclusive create.

    This helper itself is non-committing: it writes ONE file into the
    receiver's cross-repo/inbox/ and nothing else. The op's act-branch calls
    _commit_delivered_memo AFTER this succeeds to commit the delivered file
    into the receiver repo — the two steps are deliberately separate (a write
    failure must never reach the commit step; a commit failure must never
    unwind an already-durable write).

    Negative-spec:
      - Does NOT use git mv / git commit itself — plain cross-tree file-write
        only. _common.archive_and_commit is NOT used anywhere in this module
        (its private-index rename-pathspec machinery is shaped for archive
        moves, not a single delivery commit) — the commit step uses a plain
        scoped `git add` / `git commit -c core.hooksPath=<empty-tmpdir>`,
        see _commit_delivered_memo.
      - Does NOT use a nonce or content-hash suffix — the YYYY-MM-DD-<topic>.md
        filename shape is a 5-site lockstep contract; changing it requires
        DoE-coordinated filename-contract change across all 5 sites.

    Raises:
        FileExistsError: if target_path already exists — fail-loud (C1 D2 criterion 4,
            ratified 2026-07-05 as DoE-normative; O_EXCL is the atomic guard).
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(target_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    # newline="\n" is load-bearing: this write lands in the RECEIVER's working
    # tree, and Python text mode translates "\n" to "\r\n" on a Windows host --
    # so without it every delivered memo arrives CRLF no matter what the
    # receiver's .gitattributes declares. Neither side gets a signal: their
    # core.autocrlf=true normalizes CRLF back out on the way into the index, so
    # the damage is working-tree-only and `git status` stays clean on both ends.
    # Reported by project-opticon-em 2026-08-19 after observing it on a real
    # Windows host; unobservable in principle from macOS.
    # Negative-spec: do NOT drop newline= -- this write crosses into a sibling repo.
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


# ---------------------------------------------------------------------------
# B3 gitignore delivery guard (D2 criterion 7; DoE Ask-1 concurrence condition 3)
# ---------------------------------------------------------------------------

async def _git_check_ignore(receiver_repo_path: Path, rel_path: str) -> bool:
    """Run git check-ignore against the receiver repo; return True if path is gitignored.

    B3 delivery guard — port from cross-repo-memo CLI bin/cross-repo-memo:1244-1258.
    A receiver .gitignore silently swallows a memo (invisible in `git status`) without
    this check. It MUST survive the CLI→engine cut per D2 criterion 7.

    git check-ignore exit-code contract:
        0 = path IS gitignored → refuse delivery (return True)
        1 = path is NOT gitignored → safe to write (return False)
        >1 = git error → treat as NOT ignored (best-effort; logged at WARNING)

    Negative-spec: does NOT use blocking subprocess.run (DR-211 D4 async mandate).
    Does NOT suppress git errors silently — they are logged at WARNING.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(receiver_repo_path), "check-ignore", rel_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Review: code-reviewer — harden env: strip GIT_EXEC_PATH, GIT_SSH_COMMAND,
            # GIT_PROXY_COMMAND, GIT_TEMPLATE_DIR etc. (same discipline as _common.py ops).
            # A subverted check-ignore could suppress or bypass the B3 delivery guard.
            env=_make_git_env(),
        )
        _out, stderr = await proc.communicate()
        if proc.returncode > 1:
            _LOG.warning(
                "memo_send: git check-ignore error for %s/%s "
                "(returncode=%d stderr=%r) — treating as not ignored (best-effort guard)",
                receiver_repo_path,
                rel_path,
                proc.returncode,
                stderr.decode(errors="replace").strip(),
            )
        return proc.returncode == 0  # 0 = ignored; 1 = not ignored; >1 = error (not ignored)
    except OSError as exc:
        _LOG.warning(
            "memo_send: git check-ignore OSError for %s/%s: %s "
            "— treating as not ignored (best-effort guard)",
            receiver_repo_path,
            rel_path,
            exc,
        )
        return False


# ---------------------------------------------------------------------------
# scoped_to.sha resolvability gate (F14 fix — refuse BEFORE the receiver-tree
# write, not after)
#
# Narrower than the shape check in `_validate_scoped_to` above: that function
# only checks the triple is COMPLETE (artifact + exactly one of version/sha +
# seam), never whether the pinned value is actually TRUE. This gate resolves
# a supplied `sha` against the receiver's own clone and refuses the send when
# it definitively does not resolve as a commit — the sender asserted a pin,
# the assertion is false, and delivering it anyway would publish a claim the
# receiver provably cannot resolve (state/audits/2026-08-07-makima-install-
# dogfood-friction.md § F14).
#
# Standing PM ruling (2026-08-03, see `_scoped_to_errors`/`_validate_scoped_to`
# docstrings and cross-repo/inbox/2026-07-21-claude-central-em-debash-
# directive-cites-guard-plus-scoped-to-q.md): scoped_to as a WHOLE stays
# OPTIONAL, unconditionally — an absent `sha` is UNCHANGED by this gate and
# never blocks. This function only ever fires when `sha` was supplied; it
# does not reintroduce a presence gate and must never be widened to do so.
#
# Unreachable-vs-unresolvable (the judgment call named in this fix's dispatch
# brief): a receiver clone this process cannot even query (git not
# installed, the probe erroring, or an ambiguous non-0/non-1 exit) is an
# ENVIRONMENT problem, not evidence the pin is wrong — blocking the send on
# that would refuse a possibly-good memo because of a local git hiccup. Only
# a DEFINITIVE "no" (git ran cleanly and returned exit 1, meaning it
# positively could not find the sha as a commit) is treated as a false
# assertion worth refusing over. This mirrors the tri-state discipline
# `_git_premise_probe` (coordinator/bin/cross-repo-memo.py) already applies to
# the post-hoc advisory this gate now runs ahead of.
# ---------------------------------------------------------------------------

async def _verify_scoped_to_sha_resolvable(
    dry_run: bool, receiver_repo_path: Path, scoped_to: Optional[dict],
) -> Optional[dict]:
    """Refuse the send when `scoped_to.sha` is supplied but does not resolve
    as a commit in the receiver's clone. Returns None on pass (including when
    `scoped_to`/`sha` is absent, or the receiver's clone could not be
    queried) — see the module-comment block above this function for the
    unreachable-vs-unresolvable split and the standing 2026-08-03 ruling this
    gate does NOT reintroduce a presence requirement on.

    Must be called BEFORE `_write_memo_file`/`_commit_delivered_memo` — see
    the `_memo_send` call site, placed alongside the other before-any-write
    fail-loud gates (`_validate_in_reply_to_exists`, the own-inbox refusal).
    """
    if not scoped_to:
        return None
    sha = (scoped_to.get("sha") or "").strip()
    if not sha:
        return None

    env = _make_git_env()

    async def _rev_parse(rev: str) -> Optional[int]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(receiver_repo_path), "rev-parse",
                "--verify", "--quiet", rev,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            await proc.communicate()
            return proc.returncode
        except OSError as exc:
            _LOG.warning(
                "memo.send: could not run git to verify scoped_to.sha %r "
                "against receiver clone %s (%s) — degrading to advisory, "
                "not blocking (an unreachable clone is an environment "
                "problem, not a false pin).",
                sha, receiver_repo_path, exc,
            )
            return None

    commit_rc = await _rev_parse(f"{sha}^{{commit}}")
    if commit_rc is None or commit_rc == 0:
        # commit_rc is None: git could not even be run — advisory only, per
        # the unreachable-vs-unresolvable split above. commit_rc == 0: the
        # sha resolves as a commit — pin is good.
        return None
    if commit_rc != 1:
        # Neither a clean "yes" (0) nor a clean, definitive "no" (1) — git
        # ran but could not answer (e.g. GIT_DIR poisoning, a locked repo).
        # Not a claim the sha is missing; degrade to advisory only.
        _LOG.warning(
            "memo.send: git rev-parse --verify against receiver clone %s "
            "exited %d (neither 0 nor 1) verifying scoped_to.sha %r — could "
            "not determine resolvability, degrading to advisory rather than "
            "blocking a possibly-good send.",
            receiver_repo_path, commit_rc, sha,
        )
        return None

    # Definitive "no" — the sha does not resolve as a commit. Distinguish
    # "not found at all" from "found, but as a blob" (the observed operator
    # error this fix's dispatch brief names: a blob SHA supplied where a
    # commit SHA was wanted) for a more actionable refusal message.
    blob_hint = ""
    blob_rc = await _rev_parse(f"{sha}^{{blob}}")
    if blob_rc == 0:
        blob_hint = (
            f" {sha} IS present in their clone, but as a BLOB, not a commit "
            f"— scoped_to.sha wants a commit SHA (what `git log` names), not "
            f"a blob/file-content SHA (e.g. from `git hash-object` or a tree "
            f"entry). Re-pin with the commit SHA that introduced or last "
            f"touched the artifact."
        )

    return build_setup_error_result(
        _MODE, dry_run,
        f"memo.send: scoped_to.sha {sha!r} does not resolve as a commit in "
        f"the receiver's clone ({receiver_repo_path}) — the pin resolves "
        f"against the RECEIVER's history, not yours. Nothing was written to "
        f"their tree.{blob_hint} Re-pin with a commit their clone contains, "
        f"or use scoped_to.version, and re-send.",
    )


# ---------------------------------------------------------------------------
# Delivered-memo commit (retires DR-211 D2 criterion 3 for send; PM directive
# 2026-07-21)
# ---------------------------------------------------------------------------

# Full 40-hex-char sha shape, guarding `_commit_delivered_memo`'s resolved
# sha (see `_resolve_committed_sha` below, and the pathspec-scoped fallback
# spawn it falls back to) before it is trusted as `committed_sha` — mirrors
# `ops/ceremony/commit_pipeline.py`'s own `_FULL_SHA_RE` use, kept as a
# separate module-local constant rather than imported (this module does not
# otherwise depend on commit_pipeline).
_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")

# `git commit`'s own stdout carries the new commit's ABBREVIATED sha inside
# the leading `[<branch> [(root-commit) ]<abbrev-sha>] <subject>` banner —
# e.g. `[main 3e83778] x` or `[main (root-commit) 3e83778] x`. Captures the
# bracket's contents; the abbreviated sha is always its LAST whitespace
# token (branch names never contain spaces, and `(root-commit)` is the only
# other token git ever inserts there).
_COMMIT_STDOUT_BRACKET_RE = re.compile(r"\[([^\]]+)\]")

# Floor of 7, NOT git's own minimum of 4, and the difference is a
# correctness one rather than a style one. `_resolve_committed_sha` trusts
# this token only as a PREFIX of HEAD's full sha, so a short abbreviation
# makes that prefix test cheap to satisfy by accident: at 4 hex chars a
# concurrent sibling's commit collides with ours once in ~65k, and when it
# does the function returns THEIR sha as ours — silently, and precisely in
# the concurrent-sibling window the whole design exists to survive. The
# abbreviation width is the RECEIVER's `core.abbrev`, in a foreign repo
# this op does not own and must not reconfigure; `core.abbrev = 4` really
# does emit `[main c7e6] subject` (measured 2026-08-21). Rejecting a short
# token here costs one fallback spawn in such a repo and keeps the answer
# correct, which is the same trade this function already makes everywhere
# else. 7 is git's own default floor, where a collision is ~1 in 268M.
_MIN_TRUSTED_ABBREV_LEN = 7
_ABBREV_SHA_TOKEN_RE = re.compile(
    r"^[0-9a-f]{%d,40}$" % _MIN_TRUSTED_ABBREV_LEN
)


def _parse_abbrev_sha_from_commit_stdout(commit_stdout: str) -> Optional[str]:
    """Extract the abbreviated sha `git commit` printed for its own new
    commit, or `None` if the banner is not in the expected shape. Never
    raises — an unparseable banner degrades to `None`, which
    `_resolve_committed_sha` treats as "fall back to the spawn", never as a
    resolved (and therefore wrong-width) sha.
    """
    match = _COMMIT_STDOUT_BRACKET_RE.search(commit_stdout)
    if not match:
        return None
    tokens = match.group(1).split()
    if not tokens:
        return None
    candidate = tokens[-1]
    return candidate if _ABBREV_SHA_TOKEN_RE.fullmatch(candidate) else None


async def _resolve_committed_sha(
    receiver_repo_path: Path, memo_relpath: str, commit_stdout: str, env: dict,
) -> Optional[str]:
    """Resolve the full 40-char sha `_commit_delivered_memo`'s own `git
    commit` call just created, in-process on the common path and with
    exactly one fallback spawn on the rare path — see `CommitOutcome.
    committed_sha`'s docstring for why a blind HEAD read is wrong here.

    Common path (zero spawns): parse the ABBREVIATED sha out of `git
    commit`'s own stdout (`_parse_abbrev_sha_from_commit_stdout`; that
    commit is already paid for and the sha is ours by construction), then
    ask `git_state.head_sha` for HEAD's current full sha. If HEAD's sha
    STARTS WITH our abbreviated prefix, HEAD still names our commit and the
    full-width value is trustworthy — resolved, not padded, not guessed.

    Rare path (one fallback spawn): the prefix check fails to confirm —
    either the abbreviated sha could not be parsed, or a concurrent sibling
    committed to the SAME path in the narrow window between our `git
    commit` returning and this read, moving HEAD out from under us (the
    exact hazard `CommitOutcome.committed_sha`'s docstring names). Falls
    back to the original pathspec-scoped `git log -1 --format=%H --
    <memo_relpath>` spawn, which is immune to that race by construction —
    correctness over spawn count whenever the two are actually in tension.
    """
    abbrev_sha = _parse_abbrev_sha_from_commit_stdout(commit_stdout)
    if abbrev_sha:
        head = git_state.head_sha(receiver_repo_path)
        if head and head.startswith(abbrev_sha) and _FULL_SHA_RE.fullmatch(head):
            return head

    try:
        # Pathspec-scoped, not a blind HEAD read — see `CommitOutcome.
        # committed_sha`'s own docstring for the concurrent-sibling
        # rationale. Best-effort: an unresolved sha degrades to None, never
        # a failed send (this function's own never-raise contract).
        sha_proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(receiver_repo_path),
            "log", "-1", "--format=%H", "--", memo_relpath,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        sha_out, _sha_err = await sha_proc.communicate()
        if sha_proc.returncode == 0:
            resolved = sha_out.decode(errors="replace").strip()
            if _FULL_SHA_RE.fullmatch(resolved):
                return resolved
    except OSError:
        pass
    return None


@dataclasses.dataclass(frozen=True)
class CommitOutcome:
    """Structured outcome of `_commit_delivered_memo` — replaces the prior
    `Optional[Tuple[str, bool]]` return shape (C1, docs/plans/2026-08-04-
    delivery-commit-silent-failure.md) so no failure arm can discard its git
    failure reason.

    Fields mirror the pinned `delivery_commit` envelope sub-shape (see
    `_memo_send`'s act-result construction) minus `retried`, which is a
    property of the CALLER's retry wrapper (C2), not of a single commit
    attempt — a single `_commit_delivered_memo` call never retries itself.

    committed: True iff the memo path is committed in the receiver repo
        (including the idempotent "nothing to commit" no-op — the path is
        already committed as-is, which is a real success, not a failure).
    branch: the receiver's active branch name on success; None on any
        failure arm (deliberately NOT populated from a resolved-but-unused
        branch name on a git add/commit failure — branch is a success fact).
    reason: the git failure reason (stderr/stdout text, or the OSError/
        no-active-branch description) on failure; None on success.
    committed_sha: the RECEIVER-repo sha this call's own commit landed at
        (docs/plans cross-repo memo, project-rag-em, 2026-08-15, "pickup
        cannot resolve a memo by its delivery sha") — `None` whenever the
        sha cannot be attributed to THIS call with confidence, never a
        guess. Deliberately NEVER a blind post-commit HEAD read on its own
        (same concurrent-sibling hazard `ops/ceremony/commit_pipeline.py::
        CommitOutcome.committed_sha` documents at C11 — a receiver repo is
        a foreign, concurrently-written tree, so HEAD alone could pick up a
        peer's own commit landing in the same window). Resolved instead via
        `_resolve_committed_sha`: in-process, spawn-free on the common path
        by cross-checking `git_state.head_sha` against the abbreviated sha
        `git commit`'s own stdout already carries, falling back to the
        original pathspec-scoped `git log -1 --format=%H -- <memo_relpath>`
        spawn — which IS collision-free by construction, unlike a bare HEAD
        read — only when that cross-check cannot confirm HEAD is still ours
        (unparseable commit banner, or a concurrent sibling's commit moved
        HEAD in the narrow post-commit window). `None` on the idempotent "nothing to commit" no-op
        (a real success, but not THIS call's own commit — the file was
        already committed by an earlier call or a peer, so backfilling the
        pre-existing HEAD here would misattribute it) and on any failure
        arm (mirrors `branch`'s own None-on-failure rule).
    """

    committed: bool
    branch: Optional[str]
    reason: Optional[str]
    committed_sha: Optional[str] = None


async def _commit_delivered_memo(
    receiver_repo_path: Path, memo_relpath: str, sender: str, title: str,
    sent_by: Optional[str] = None,
) -> CommitOutcome:
    """Stage+commit ONLY the just-delivered memo file in the RECEIVER repo.

    NOT A DUPLICATE of `coordinator/bin/cross-repo-memo.py::_commit_delivered_memo`
    — negative spec, do not "dedupe" the two. That copy serves the
    `--self-receipt` arm only, which is single-repo by construction, so it
    deliberately keeps the receiver's hooks running and retains branch
    creation; both are wrong HERE, where the receiver is a foreign tree (see
    the Mechanism and Branch-creation-REMOVED paragraphs below for the
    rulings). Collapsing the two would break one contract or the other. The
    scoped single-path add/commit, the never-raise contract, the AC3
    unstage-on-failure, and the three-phrasing idempotent-no-op guard ARE held
    in common and should be kept in step across both sites.

    Claim-release ineligible (C3, docs/plans/2026-08-11-claim-release-and-
    the-gate-that-cannot-clear.md): this commit lands in the RECEIVER's
    repo, a foreign worktree relative to this session. `release_committed_
    claims` releases the CALLING session's own claims against `cwd`'s own
    ledger — releasing the local sid's claims against a peer worktree is
    meaningless (there is no local claim ledger scoped to a repo this
    session does not own), and `release_committed_claims` must never be
    passed a peer sid (self/other boundary, pinned by test). No release
    call is added here.

    Retires DR-211 D2 criterion 3 ("send is non-committing") per PM directive
    2026-07-21: a dirty delivered file previously relied on the receiver's
    session-init sweep noticing it in `git status` — a soft signal. This
    commits the memo so delivery is a durable, cross-device-visible fact
    rather than a hope.

    Mechanism (amended 2026-07-21 per Patrik's approach-review REQUIRES_CHANGES;
    DR-214 amendment): single all-hooks-off path via
    `git -c core.hooksPath=<empty-tmpdir> commit`, NO `--no-verify` anywhere.
    RATIONALE: a memo DELIVERY into a foreign repo must not execute the
    receiver's own commit-time machinery at all —
      1. the foreign repo's hooks are not ours to honor;
      2. the receiver's OWN message hooks (pre-commit / prepare-commit-msg /
         commit-msg) must never run on a delivery commit — a hook that
         stamps e.g. a Session-Id trailer meaningful only to that repo's own
         commits would inject a FALSE trailer onto a foreign delivery;
      3. `--no-verify` does NOT bypass `prepare-commit-msg` (verified
         empirically) — this was the exact hook whose failure motivated
         this change in the first place, so `--no-verify` alone never fixed
         the motivating bug;
      4. suppressing `post-commit` too means this op does NOT drive the
         receiver's own auto-push hook — the memo lands
         committed-but-unpushed, and propagation is deliberately left to the
         receiver's own next push (this op is not a foreign write to the
         receiver's remote).
    An empty, ephemeral hooks dir (`tempfile.TemporaryDirectory()`, cleaned
    up automatically) passed via `-c core.hooksPath=<dir>` neutralizes ALL
    four hook classes for this one invocation only — no permanent change to
    the receiver's git config.

    Explicit single-path commit: `git add -- <memo_relpath>` then, inside
    the `TemporaryDirectory` context,
    `git -c core.hooksPath=<empty-tmpdir> commit -m "<subject>" -- <memo_relpath>`.
    NEVER `git add -A` / `git add .` here — a dirty receiver tree (routine
    under concurrent-EM git operations) must never be swept into the
    delivery commit.

    Branch-creation REMOVED (2026-07-21 per Patrik REQUIRES_CHANGES): a
    receiver repo with NO active branch (detached HEAD, bare repo, or no
    commits yet) is no longer switched onto a newly-created work branch — a
    headless engine mutating a receiver's branch state (e.g. a receiver
    mid-bisect or mid-rebase, or simply on a branch the engine doesn't know
    about) is an unacceptable foreign mutation. That case now uses the same
    graceful-degradation shape as every other unrecoverable case below: skip
    the commit, log a WARNING, leave the file written+uncommitted for the
    receiver's own session-init sweep to notice.

    Graceful degradation (best-effort, NEVER fails the send):
      - detached HEAD (receiver's `HEAD` file is not a symbolic ref — the
        in-process equivalent of `git symbolic-ref -q HEAD` failing) → SKIP
        the commit, log WARNING, leave the file written+uncommitted. No
        branch is created or switched.
      - on `main`/`master` (existing active branch) → still commit (branch
        discipline is the receiver's to resolve, not this op's) — WARNING
        notes it so the caller can log it.
      - nothing to commit (git reports no change to the memo path, e.g. an
        already-committed identical file) → treat as success, no-op.
      - any git subprocess failure or OSError → SKIP, log WARNING, leave
        file as-is.

    Never raises: the memo is already durably written by the time this
    runs — a commit failure here must NOT turn a successful delivery into
    a reported failure. The handler always returns build_act_result for a
    successful write regardless of this function's outcome.

    Args:
        receiver_repo_path: absolute path to the receiver repo root (from
            _resolve_receiver_inbox — registry-derived, never wire-derived).
        memo_relpath: the memo's path RELATIVE to receiver_repo_path (e.g.
            "cross-repo/inbox/<filename>.md") — this is the exact scoped
            pathspec passed to both `git add` and `git commit`.
        sender: the memo's from_id — used in the commit subject.
        title: the memo's title — used in the commit subject.
        sent_by: the sender's session UUID (C7, docs/plans/2026-08-13-
            session-identity-earns-its-keep.md), or None/the unresolved
            sentinel. When it names a real UUID, this function appends a
            `Session-Id: <uuid>` trailer to the commit message it builds
            itself — NEVER via a receiver-side hook (see the Mechanism
            paragraph above: this commit runs with ALL of the receiver's
            hooks disabled, for exactly the reason a hook-authored trailer
            would be wrong here). An absent/unresolved sent_by means an
            absent trailer, never a failed commit — this never-raise
            contract is unchanged.

    Returns:
        CommitOutcome — `committed=True, branch=<name>, reason=None` on
        success (including the idempotent nothing-to-commit no-op case);
        `committed=False, branch=None, reason=<git failure text>` on any
        failure arm or the no-active-branch skip (still logged at WARNING —
        this adds a channel, it does not move one — and NEVER raises).
    """
    env = _make_git_env()

    async def _unstage_delivered_memo() -> None:
        """Undo `git add` of memo_relpath (AC3) so a failed delivery leaves
        the receiver's index exactly as it found it. Best-effort and never
        raises, mirroring this function's own never-raise contract — an
        unstage failure must not turn an already-reported commit failure
        into a crash. `git reset -- <path>` is a safe no-op when the path
        was never staged (e.g. the `git add` step itself failed).
        """
        try:
            reset_proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(receiver_repo_path), "reset", "--", memo_relpath,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            await reset_proc.communicate()
        except OSError:
            pass

    # In-process equivalent of `git symbolic-ref -q HEAD` (spawn removed,
    # docs/plans/2026-08-21-memo-send-stops-asking-git-what-it-already-
    # knows.md C1): `symbolic-ref -q HEAD` succeeds iff `HEAD` is a symbolic
    # ref (its content starts with `ref:`), REGARDLESS of whether the target
    # ref resolves to a commit — empirically true for a bare repo and an
    # unborn branch alike (both hold `ref: refs/heads/<x>` in `HEAD`), and it
    # fails only on a detached HEAD (`HEAD` holds a raw sha, no `ref:`
    # prefix). `resolve_git_dir` is used directly rather than
    # `git_state.head_sha` — that function follows the ref hop and would
    # collapse "symref present, target unresolved" (success here) into
    # `None` (which would misread as failure), a behaviour change this
    # in-process read must not make.
    gitdir = resolve_git_dir(receiver_repo_path)
    try:
        head_content = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError as exc:
        reason = f"could not read receiver HEAD (symbolic-ref): {exc}"
        _LOG.warning(
            "memo_send: could not commit delivered memo in receiver repo %s (%s) "
            "— file was written but left uncommitted.",
            receiver_repo_path, exc,
        )
        return CommitOutcome(committed=False, branch=None, reason=reason)

    if not head_content.startswith("ref:"):
        # Detached HEAD only. `symbolic-ref -q HEAD` fails on a detached HEAD
        # and SUCCEEDS on a bare repo and an unborn branch alike — all three
        # verified 2026-08-21 (rc=1, rc=0, rc=0; bare and unborn both hold
        # `ref: refs/heads/<x>`), so the older comment here claiming it "fails
        # silently on all three" was wrong before the spawn was ever removed,
        # and the in-process `ref:` test above reproduces git's real behaviour
        # exactly rather than the comment's.
        #
        # The `reason` string below still enumerates all three and is
        # DELIBERATELY LEFT ALONE: it is not prose. It is written verbatim
        # into `state/memo-outbox/sent-ledger.jsonl` as
        # `delivery_commit_reason`, has a byte-identical twin at
        # `ops/tracker/push_suggestion.py`, and is quoted in DR-214. Narrowing
        # it is a contract change with three call sites, not a comment fix —
        # do not "correct" it in passing.
        #
        # SKIP rather than create/switch a branch
        # (removed 2026-07-21 per Patrik REQUIRES_CHANGES: branch-creation in
        # a foreign repo is an unacceptable mutation). Leave the file
        # written+uncommitted for the receiver's own session-init sweep.
        reason = "no active branch (detached HEAD, bare repo, or unborn HEAD)"
        _LOG.warning(
            "memo_send: receiver repo %s has no active branch (detached HEAD, "
            "bare, or unborn) — delivered memo left uncommitted for the "
            "receiver's session-init sweep.",
            receiver_repo_path,
        )
        return CommitOutcome(committed=False, branch=None, reason=reason)

    ref = head_content[len("ref:"):].strip()
    branch_name = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
    if branch_name in ("main", "master"):
        _LOG.warning(
            "memo_send: receiver repo %s is on '%s' — committing the "
            "delivered memo there anyway; branch discipline is the "
            "receiver's to resolve.",
            receiver_repo_path, branch_name,
        )

    try:
        add_proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(receiver_repo_path), "add", "--", memo_relpath,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _add_out, add_err = await add_proc.communicate()
        if add_proc.returncode != 0:
            reason = f"git add failed: {add_err.decode(errors='replace').strip()}"
            _LOG.warning(
                "memo_send: 'git add' of delivered memo failed in receiver repo "
                "%s (%s); file was written but left uncommitted.",
                receiver_repo_path, add_err.decode(errors="replace").strip(),
            )
            return CommitOutcome(committed=False, branch=None, reason=reason)

        subject = f"cross-repo: deliver {title} memo from {sender}"
        # sent_by trailer (C7): authored HERE, in-process, never via a
        # receiver-side hook — this commit runs with ALL of the receiver's
        # hooks disabled (see function docstring RATIONALE), so a hook is
        # never a legitimate way to add this. An absent/unresolved sent_by
        # means an absent trailer, never a commit failure.
        commit_message = subject
        if sent_by and sent_by != _SENT_BY_UNRESOLVED:
            commit_message = f"{subject}\n\nSession-Id: {sent_by}\n"
        with tempfile.TemporaryDirectory() as empty_hooks_dir:
            # All-hooks-off commit: -c core.hooksPath=<empty-tmpdir> neutralizes
            # pre-commit, prepare-commit-msg, commit-msg AND post-commit for
            # this ONE invocation — see function docstring RATIONALE. This is
            # the sanctioned replacement for --no-verify (which does not
            # bypass prepare-commit-msg and would still run the receiver's
            # own message hooks, injecting a foreign trailer).
            commit_proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(receiver_repo_path),
                "-c", f"core.hooksPath={empty_hooks_dir}",
                "-c", "commit.gpgsign=false",  # GAP-6: neutralise repo/global signing config for this TTY-less invocation
                "commit", "-m", commit_message, "--", memo_relpath,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            commit_out, commit_err = await commit_proc.communicate()
        if commit_proc.returncode != 0:
            combined = (
                commit_out.decode(errors="replace")
                + commit_err.decode(errors="replace")
            ).lower()
            if (
                "nothing to commit" in combined
                or "nothing added to commit" in combined
                or "no changes added to commit" in combined
            ):
                # Idempotent no-op — the memo path is already committed as-is.
                # All three phrasings are git's "nothing staged/changed"
                # family: "nothing to commit" (clean tree), "nothing added to
                # commit" (untracked only), and "no changes added to commit"
                # (tracked-but-unstaged, emitted when the tree has OTHER dirty
                # files — routine under concurrent-EM git). Missing the third
                # let an already-committed memo read as an uncommitted-
                # delivery failure.
                return CommitOutcome(
                    committed=True, branch=branch_name, reason=None,
                    committed_sha=None,
                )
            await _unstage_delivered_memo()
            commit_reason = (
                commit_err.decode(errors="replace")
                or commit_out.decode(errors="replace")
            ).strip()
            _LOG.warning(
                "memo_send: 'git commit' of delivered memo failed in receiver "
                "repo %s (%s); file was written but left uncommitted.",
                receiver_repo_path,
                commit_reason,
            )
            return CommitOutcome(
                committed=False, branch=None,
                reason=f"git commit failed: {commit_reason}",
            )
    except OSError as exc:
        # add and commit are both inside this try — an exception could land
        # either before or after `git add` staged the path, so unstage
        # unconditionally (AC3); see _unstage_delivered_memo's no-op note.
        await _unstage_delivered_memo()
        reason = f"could not run git add/commit: {exc}"
        _LOG.warning(
            "memo_send: could not commit delivered memo in receiver repo %s (%s) "
            "— file was written but left uncommitted.",
            receiver_repo_path, exc,
        )
        return CommitOutcome(committed=False, branch=None, reason=reason)

    committed_sha = await _resolve_committed_sha(
        receiver_repo_path, memo_relpath,
        commit_out.decode(errors="replace"), env,
    )

    return CommitOutcome(
        committed=True, branch=branch_name, reason=None,
        committed_sha=committed_sha,
    )


# ---------------------------------------------------------------------------
# index.lock retry — exactly one retry on lock contention (C2, docs/plans/
# 2026-08-04-delivery-commit-silent-failure.md)
# ---------------------------------------------------------------------------

# Signature matched case-insensitively against the failure reason — git's
# own message is "Unable to create '<path>/.git/index.lock': File exists."
# regardless of platform, so a substring match on the literal filename is
# both sufficient and immune to git's surrounding phrasing changing.
_INDEX_LOCK_SIGNATURE = "index.lock"

# Delay between attempts, and the attempt cap (AC4). This machine's documented
# norm is 50-70 concurrently active LLMs (docs/wiki/machine-load-norm.md), and
# that doc explicitly calls out index.lock contention as a fleet-wide constant
# under that load, with hold times running to multiple seconds — the retired
# 200ms/single-attempt shape was sized against an idle box the doc says bounds
# nothing. 5 attempts (1 initial + 4 retries) spaced 0.5s apart bounds total
# added wait at ~2.0s: enough attempts to outlast a multi-second hold without
# an unbounded loop, and well inside the 30s DISPATCH_TIMEOUT_SECS runaway
# guard (coordinator_core/ipc.py) so this never itself becomes the hang.
_INDEX_LOCK_RETRY_DELAY_SECONDS = 0.5
_INDEX_LOCK_MAX_ATTEMPTS = 5


async def _commit_delivered_memo_with_retry(
    receiver_repo_path: Path, memo_relpath: str, sender: str, title: str,
    sent_by: Optional[str] = None,
) -> tuple[CommitOutcome, bool]:
    """Wrap `_commit_delivered_memo`, retrying up to `_INDEX_LOCK_MAX_ATTEMPTS`
    times (bounded total wait) when the failure reason carries the
    `.git/index.lock` contention signature.

    Rationale (C1, docs/plans/2026-08-13-memo-send-delivery-commit-verify-
    hole.md, AC4): a shared receiver tree with several concurrent live
    sessions is this fleet's NORMAL operating condition, not an edge case —
    a transient lock collision previously became a permanent silent orphan
    (the defect this plan makes observable and non-destructive). The prior
    single 200ms attempt was sized against an idle box; see
    `_INDEX_LOCK_RETRY_DELAY_SECONDS`'s comment for the multi-second-hold
    rationale behind the current bound.

    HARD CONSTRAINTS (do not relax without a plan amendment):
      - Bounded total wait via a fixed attempt cap and fixed delay — no
        unbounded loop, no exponential backoff, no configurable count.
      - Matched on the OBSERVED REASON STRING only, case-insensitively, on the
        `index.lock` signature — a non-index.lock failure (a real conflict,
        a hook rejection, a permissions error, including the gpgsign arm
        AC5 handles directly) is never retried; retrying an unrelated
        failure just adds latency before the identical outcome.
      - The never-raise contract still binds: `_commit_delivered_memo` itself
        never raises, and this wrapper adds no new raise path.

    Returns:
        (final_outcome, retried) — `retried` is True iff at least one retry
        attempt actually fired (i.e. some attempt before the last failed with
        an index.lock reason), regardless of whether the final attempt
        succeeded.
    """
    outcome = await _commit_delivered_memo(
        receiver_repo_path, memo_relpath, sender, title, sent_by=sent_by,
    )
    retried = False
    attempt = 1
    while (
        not outcome.committed
        and outcome.reason
        and _INDEX_LOCK_SIGNATURE in outcome.reason.lower()
        and attempt < _INDEX_LOCK_MAX_ATTEMPTS
    ):
        attempt += 1
        retried = True
        _LOG.info(
            "memo_send: delivered-memo commit hit index.lock contention in receiver "
            "repo %s — retrying (attempt %d/%d) after a short delay.",
            receiver_repo_path, attempt, _INDEX_LOCK_MAX_ATTEMPTS,
        )
        await asyncio.sleep(_INDEX_LOCK_RETRY_DELAY_SECONDS)
        outcome = await _commit_delivered_memo(
            receiver_repo_path, memo_relpath, sender, title, sent_by=sent_by,
        )

    return outcome, retried


# ---------------------------------------------------------------------------
# Sender-outbox sent-stamp — write-back onto the SENDER's own draft copy
# ---------------------------------------------------------------------------

# Sender-side outbox draft location — mirrors memo_draft._OUTBOX_DIRNAME
# (coordinator_core/ops/fleet/memo_draft.py) byte-for-byte. Duplicated here
# rather than imported: memo_draft.py imports FROM this module (its own
# shared-helper home — see this module's docstring "cross-imports" list), so
# importing the reverse direction here would create a circular import.
_SENDER_OUTBOX_DIRNAME = ("state", "memo-outbox")


def _sender_outbox_path(sender_worktree: Path, topic: str) -> Path:
    """The sender-side outbox draft path a `memo.send` call for `topic` MAY
    have originated from — `<sender_worktree>/state/memo-outbox/<topic>.md`.

    Purely a path computation; the caller (`_stamp_sender_outbox_sent`) still
    has to check the file actually exists — a flag-only/campaign send (no
    prior `memo.draft`) legitimately has nothing at this path.
    """
    return sender_worktree.joinpath(*_SENDER_OUTBOX_DIRNAME, f"{topic}.md")


def _portable_delivered_to_form(receiver_repo_path: Path, delivered_path: Path) -> str:
    """Render `delivered_path` as the portable form stamped into
    `delivered_to` — receiver-repo-relative when possible, falling back to a
    `~/`-prefixed home-relative form, and only then to the absolute string.

    Root cause this closes (verified cross-repo/inbox finding, doe-claude-em):
    `str(delivered_path)` stamped a machine-absolute path
    (`/Users/<name>/...`) into tracked frontmatter, which reddens DoE's
    `test_no_posix_home_path_citations` portability gate on every tracked
    sent memo. The receiver is already unambiguous from the memo's `to:` plus
    `delivery_mode: receiver-repo`, so a receiver-repo-relative path carries
    no less information than the absolute one did.

    Separators are always normalized to `/` (Windows is first-class in this
    repo) regardless of which of the three forms below is produced.
    """
    try:
        rel = delivered_path.resolve().relative_to(receiver_repo_path.resolve())
        return rel.as_posix()
    except (ValueError, OSError):
        pass
    try:
        home_rel = delivered_path.resolve().relative_to(Path.home().resolve())
        return "~/" + home_rel.as_posix()
    except (ValueError, OSError):
        pass
    return str(delivered_path).replace("\\", "/")


def _stamp_sender_outbox_sent(
    sender_worktree: Path,
    topic: str,
    delivered_path: Path,
    receiver_repo_path: Path,
) -> bool:
    """Best-effort write-back: stamp the sender's OWN outbox draft copy
    `status: sent` plus delivery evidence, once the receiver-side write has
    actually succeeded.

    Returns True iff the stamp write actually happened (used by the caller,
    `_memo_send`, to build `_scope_touch_paths` — this op's own written-path
    self-declaration to the engine's scope-claim mechanism; only an actual
    write may be declared, never the merely-attempted no-op/conditional-skip
    paths below). False on every no-op/skip/failure branch.

    Root cause this closes (verified cross-repo/inbox finding,
    doe-claude-em): `cross-repo-memo send` / `memo.send` dispatched to the
    receiver and removed the sender's local draft, but nothing ever wrote
    delivery evidence back onto the sender's own outbox copy — the CLI's
    `os.remove(outbox_path)` (coordinator/bin/cross-repo-memo.py `_send_via_
    engine`) is the only sender-side effect today, and it destroys the
    would-be stamp target rather than populating it. This is the engine-side
    half of the fix: `memo.send` is the ONE choke point every send path
    (single-receiver, redelivery, fan-out) already funnels through, so
    stamping here — rather than in the CLI veneer — covers every caller
    without duplicating the write-back per call site.

    Ordering (why this call sits where it does in `_memo_send`, AFTER the
    O_EXCL write and the delivered-memo commit attempt, never before):
    a stamp claiming delivery it cannot yet prove would be worse than no
    stamp at all — a sender reading `status: sent` must be able to trust
    that the receiver-side file in `delivered_path` really exists. Placing
    this call after `_write_memo_file` has already returned successfully is
    what makes that true; it does NOT wait on `_commit_delivered_memo`'s
    outcome, because the O_EXCL write (not the receiver-repo commit) is what
    this module treats as "delivered" everywhere else (`build_act_result`
    itself reports success once the write succeeds, regardless of the
    commit's own best-effort outcome — see `_commit_delivered_memo`'s
    docstring).

    Never raises (mirrors `_commit_delivered_memo`'s never-raise contract):
    the receiver-side delivery is ALREADY a durable fact by the time this
    runs, so a stamp failure (missing/malformed outbox file, a frontmatter
    shape the primitives refuse to touch, a permissions/OSError on write)
    must not turn a successful send into a reported failure. Every failure
    branch below logs at WARNING and returns; none re-raises.

    Negative-spec:
      - Does NOT create an outbox file that does not already exist — a
        flag-only or `--campaign-to` send with no prior `memo.draft` has no
        outbox copy to stamp, and a `FileNotFoundError` here is the
        ordinary, silent, expected case (not logged).
      - Does NOT stamp a file whose `status:` is not literally `"draft"` —
        this refuses BOTH an already-stamped copy (a second fan-out receiver
        landing on the same shared `topic`, or a re-run against an
        already-sent topic) and a file that happens to exist at this path
        but is some other shape entirely. Re-stamping an already-`sent` copy
        would let a later, unrelated send overwrite the FIRST send's
        delivery evidence with its own — the guard is what keeps
        `delivered_to`/`sent_at` naming the actual first delivery.
      - Does NOT touch `title`/`to`/`body`/any other outbox field — only
        `status`, `sent_at` (added), and `delivered_to` (added) are written.
      - Does NOT stamp `delivered_to` as a machine-absolute path — see
        `_portable_delivered_to_form`. A receiver-repo-relative path is
        preferred (the receiver is already unambiguous from `to:` plus
        `delivery_mode: receiver-repo`); an absolute home path tracked into
        a sent memo reddens DoE's `test_no_posix_home_path_citations`
        portability gate downstream, which is the whole reason this function
        never emits one when a relative form is available.
      - Does NOT use `_yaml_quote`/hand-rolled YAML text — reuses the shared
        `coordinator_core.frontmatter.primitives` mutation primitives
        (`split_frontmatter`/`replace_fm_field`/`insert_fm_field`/`rebuild`),
        the same toolkit every other frontmatter-mutating op in this engine
        already uses, rather than hand-writing a parallel YAML writer.
    """
    outbox_path = _sender_outbox_path(sender_worktree, topic)
    try:
        text = outbox_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False  # no outbox draft for this send — nothing to stamp, not an error
    except OSError as exc:
        _LOG.warning(
            "memo_send: could not read sender outbox draft %s to stamp it "
            "sent (%s) — delivery already succeeded; the stamp is "
            "best-effort and never turns a successful send into a failure.",
            outbox_path, exc,
        )
        return False

    split = split_frontmatter(text)
    if split is None:
        _LOG.warning(
            "memo_send: sender outbox draft %s has no parseable frontmatter "
            "— skipping sent-stamp (delivery already succeeded).",
            outbox_path,
        )
        return False

    fm = split.fm_text
    if read_fm_field_unquoted(fm, "status") != "draft":
        # Already stamped by an earlier send of this same topic, or not a
        # draft-shaped file at all — never clobber either case (see
        # negative-spec above).
        return False

    try:
        fm = replace_fm_field(fm, "status", "sent")
        sent_at = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        fm = insert_fm_field(fm, "sent_at", sent_at, after_key="status")
        fm = insert_fm_field(
            fm, "delivered_to",
            _portable_delivered_to_form(receiver_repo_path, delivered_path),
            after_key="sent_at",
        )
        # newline="\n": Path.write_text is text mode, so on a Windows host this
        # rewrites the whole outbox draft with CRLF terminators. This is the
        # instance project-opticon-em caught in the wild (2026-08-19) -- their
        # state/memo-outbox/sent/goal-append-citation-correction.md carried 56
        # CRLF sequences against their own `*.md text eol=lf`. Invisible to git
        # on both sides: core.autocrlf=true normalizes it back out of the index.
        outbox_path.write_text(
            _rebuild_frontmatter(split, fm), encoding="utf-8", newline="\n"
        )
        return True
    except (ValueError, OSError) as exc:
        _LOG.warning(
            "memo_send: could not stamp sender outbox draft %s as sent (%s) "
            "— delivery already succeeded; the stamp is best-effort and "
            "never turns a successful send into a failure.",
            outbox_path, exc,
        )
        return False


# Sent-memo ledger filename — same sender-worktree directory
# (`_SENDER_OUTBOX_DIRNAME`) the outbox draft/stamp already uses; a distinct
# file so an unconditional append-only log never collides with (or has to
# reason about) the conditional, per-topic outbox draft lifecycle.
_SENT_LEDGER_FILENAME = "sent-ledger.jsonl"


def _sender_sent_ledger_path(sender_worktree: Path) -> Path:
    """The sender-side append-only sent-memo ledger path —
    `<sender_worktree>/state/memo-outbox/sent-ledger.jsonl`.

    Purely a path computation; the caller (`_append_sent_ledger`) still
    creates parent directories as needed — unlike the outbox draft (which
    only ever exists because a prior `memo.draft` created it), this file may
    not exist yet on a repo's first-ever send.
    """
    return sender_worktree.joinpath(*_SENDER_OUTBOX_DIRNAME, _SENT_LEDGER_FILENAME)


def _append_sent_ledger(
    sender_worktree: Path,
    *,
    topic: str,
    to: str,
    kind: str,
    delivered_path: Path,
    receiver_repo_path: Path,
    in_reply_to: Optional[str],
    delivery_commit_reason: Optional[str] = None,
    delivery_commit_retried: Optional[bool] = None,
    delivery_commit_sha: Optional[str] = None,
    sent_by: Optional[str] = None,
) -> Optional[str]:
    """Append-only local evidence that a send happened: one JSONL line per
    delivered receiver, in `<sender_worktree>/state/memo-outbox/sent-ledger.jsonl`.

    Returns the FULL ledger text after the append on success (used by the
    caller, `_memo_send`, both to build `_scope_touch_paths` — see the
    analogous docstring note on `_stamp_sender_outbox_sent`'s return value —
    and, per C2, as the exact bytes the sender-side ledger commit leg
    commits, with no separate read-back). Returns `None` only on the
    best-effort OSError branch below.

    C2 (docs/plans/2026-08-06-memo-send-sender-side-commit-leg.md):
    routed through `locked_rmw` (coordinator_core/locked_write.py) rather
    than a bare `open(mode="a")` — the append itself is now the SAME
    critical section a caller can commit the returned text from, with no
    separate unlocked read that could observe a torn final line mid-append
    by a concurrent peer session. Every writer of this file goes through
    this one function, so this lock is the single point of mutual exclusion
    for the whole ledger, not merely this process's own appends.

    ONE-TIME LINE-ENDING NORMALIZATION, observed live (f7e8778062181fd9, the
    first send after this leg landed): `locked_rmw` is asymmetric by
    construction — it reads via `Path.read_text()` (text mode, universal
    newlines, so CRLF on disk arrives as `\n`) and writes raw
    `new_text.encode("utf-8")` (no translation). A ledger written by the
    PREVIOUS `open(mode="a")` path on Windows is therefore CRLF on disk, and
    the first append routed through here rewrites every line to bare LF. That
    commit reads as a whole-file rewrite (1539 insertions / 1502 deletions
    here) and is NOT this leg sweeping peer content — every one of those rows
    is byte-identical apart from its terminator. It happens exactly ONCE per
    ledger: LF read back as LF re-encodes to LF, so every subsequent append is
    a clean +1/-0. A sibling repo adopting this shape will see the same
    one-time churn on its own first send; that is expected, not a defect.

    Root cause this closes (cross-repo memo, doe-claude-em, 2026-08-04): a
    plan chunk in a SENDING repo whose deliverable is a cross-repo memo had
    no local evidence the memo shipped, so the chunk could never close —
    `close-out-and-stamp` only honours a `disposition_ref` that resolves to a
    real commit object in the sending repo and is an ancestor of HEAD (the
    anti-self-attestation rule, which is correct and stays untouched). The
    `draft` -> `compose` -> `send` lifecycle already leaves a sender copy
    under `state/memo-outbox/` that `_stamp_sender_outbox_sent` marks `sent`,
    so those chunks stamp fine — but the legacy one-shot flag form
    (`--to/--topic/--title/--body-file`, no draft stage) archives nothing
    sender-side, so a genuinely delivered memo was genuinely unprovable at
    home. This ledger is the fix: a durable, append-only, sender-local record
    that exists independent of whether an outbox draft ever did.

    UNCONDITIONAL where `_stamp_sender_outbox_sent` is conditional — that is
    the entire point. The stamp only fires when a `state/memo-outbox/<topic>.md`
    draft exists (and is still `status: draft`); this ledger write has no such
    gate and fires for every send this function is called from, lifecycle or
    one-shot alike. This is also why it is a SEPARATE function rather than an
    unconditional branch bolted onto the stamp: the stamp's existing
    conditional-on-draft behavior is unchanged by this addition.

    Fires once per delivered receiver — the caller in `_memo_send` runs once
    per `_memo_send` invocation, and `_memo_send_fan_out` calls `_memo_send`
    once per receiver, so a fan-out naturally produces one ledger line per
    receiver without this function knowing anything about fan-out.

    Never raises (mirrors `_stamp_sender_outbox_sent`'s and
    `_commit_delivered_memo`'s never-raise contract): the receiver-side
    delivery is ALREADY a durable fact by the time this runs (called only
    after the O_EXCL write to the receiver tree has succeeded — see the call
    site in `_memo_send`), so a ledger write failure (unwritable path,
    permissions, disk full) must log at WARNING and return, never turn a
    successful send into a reported failure.

    Negative-spec:
      - Does NOT stamp `delivered_to` as a machine-absolute path — reuses
        `_portable_delivered_to_form` (receiver-repo-relative, falling back to
        `~/`-relative, only then absolute), the same rule
        `_stamp_sender_outbox_sent` follows and for the same reason: an
        absolute home path tracked into this file would redden DoE's
        `test_no_posix_home_path_citations` portability gate.
      - Does NOT truncate, rotate, or de-duplicate the ledger — strictly
        append-only; a second send of the same topic appends a second line
        rather than replacing the first (the sent-stamp's single mutable
        `state:sent` field is a different, separate mechanism for that case).
      - Does NOT gate on `in_reply_to` being present — omitted sends emit
        `"in_reply_to": null`, never a missing key, so every line has the
        same schema regardless of which fields the memo itself supplied.

    AC8 (docs/plans/2026-08-13-memo-send-delivery-commit-verify-hole.md):
    `delivery_commit_reason`/`delivery_commit_retried` add two OPTIONAL keys
    (`delivery_commit_reason`, `retried`) to the written row, defaulting to
    `None` when the caller has no commit outcome to report (e.g. the
    fan-out/test call paths that never pass them). ~1719 existing rows on
    disk predate this pair and simply lack the keys — any reader MUST treat
    their absence as `None`/unknown, never as a parse error or a required
    field. This function does not itself read the ledger back, so it makes
    no claim about existing readers; that tolerance is a reader-side
    contract this docstring only records for future ledger consumers.

    sent_by (C7, docs/plans/2026-08-13-session-identity-earns-its-keep.md):
    OPTIONAL key, same tolerance rule as `delivery_commit_reason`/`retried`
    above — existing rows on disk predate this field and simply lack the
    key; a reader treats absence as unknown, never a parse error. When the
    caller passes it, it is the SAME value already stamped into the
    delivered memo's `sent_by:` frontmatter line for this send (durable
    session UUID, or the explicit unresolved sentinel — never a resolved
    messaging address).

    delivery_commit_sha (project-rag-em cross-repo memo, 2026-08-15,
    "pickup cannot resolve a memo by its delivery sha"): adds the
    `delivery_commit_sha` key to the written row — OPTIONAL key, same
    tolerance rule as `delivery_commit_reason`/`retried`/`sent_by` above;
    ~1719+ pre-existing rows on disk predate this field and simply lack
    the key, and any reader MUST treat its absence as `None`/unknown,
    never as a parse error. This is the SAME value as
    `CommitOutcome.committed_sha` (see that field's own docstring for the
    concurrent-sibling attribution rule) threaded through
    `delivery_commit["sha"]` — `None` whenever the receiver-side commit
    landed but its sha could not be attributed with confidence, or the
    idempotent no-op/failure arms, never a guess. Recording it here turns
    the SHA↔memo mapping into data on the sender's own ledger row instead
    of the `git log --diff-filter=A` archaeology the memo's root-cause
    report had to fall back to.
    """
    ledger_path = _sender_sent_ledger_path(sender_worktree)
    line = {
        "sent_at": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "to": to,
        "topic": topic,
        "kind": kind,
        "delivered_to": _portable_delivered_to_form(receiver_repo_path, delivered_path),
        "in_reply_to": in_reply_to,
        "delivery_commit_reason": delivery_commit_reason,
        "retried": delivery_commit_retried,
        "delivery_commit_sha": delivery_commit_sha,
        "sent_by": sent_by,
    }
    appended_line = json.dumps(line, ensure_ascii=False) + "\n"

    def _mutate(old_text: str) -> str:
        return old_text + appended_line

    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        return locked_rmw(
            ledger_path, _mutate, repo_root=sender_worktree, missing_ok=True,
        )
    except (OSError, LockTimeout, RuntimeError) as exc:
        _LOG.warning(
            "memo_send: could not append sent-ledger line to %s (%s) — "
            "delivery already succeeded; the ledger write is best-effort and "
            "never turns a successful send into a failure.",
            ledger_path, exc,
        )
        return None


# ---------------------------------------------------------------------------
# C2 (docs/plans/2026-08-06-memo-send-sender-side-commit-leg.md) — the
# sender-side ledger commit leg. Commits state/memo-outbox/sent-ledger.jsonl
# in the SENDER's own tree via git_native.commit_authored_content (DR-272
# § 3.3 form-3, no-worktree-read), never a mirror of _commit_delivered_memo's
# hand-rolled `git add -- <path> && git commit -- <path>` (Problem § Reason 2
# of the plan — that raw form is the exact one DR-211 § D3 found laundering
# foreign worktree content; commit_authored_content never reads the worktree
# at all, closing that hazard structurally rather than by convention).
# ---------------------------------------------------------------------------

# CAS-failure diagnostic signature emitted by commit_authored_content's
# update-ref step (git_native.py) — distinguishes the EXPECTED, retryable
# "HEAD moved concurrently" failure from every other (non-retryable) refusal,
# most notably the HEAD-existence refusal on a fresh, untracked-ledger clone
# ("does not exist in HEAD"), which retrying can never fix.
_LEDGER_CAS_FAILURE_SIGNATURE = "compare-and-swap failed"

# Repo-relative, posix-separated pathspec commit_authored_content expects —
# matches _sender_sent_ledger_path's own _SENDER_OUTBOX_DIRNAME + filename.
_SENT_LEDGER_RELPATH = "/".join((*_SENDER_OUTBOX_DIRNAME, _SENT_LEDGER_FILENAME))


def _read_sent_ledger_locked(sender_worktree: Path) -> Optional[str]:
    """Read the sender's ledger file back under the SAME `locked_rmw` lock
    `_append_sent_ledger` appends through — a locked, race-safe read with no
    mutation (the `mutate` callback is the identity function, so `locked_rmw`
    always takes its no-op/no-write branch).

    Used only for the fan-out hoist's single post-loop read (see
    `_memo_send_fan_out`) and for the CAS-failure retry's fresh-HEAD re-read
    below — the inline (non-fan-out) path never calls this, because
    `_append_sent_ledger` already returns the exact post-append bytes from
    its own single critical section (no separate read-back needed there).

    Never raises: returns `None` on any lock/read failure, logged at WARNING
    — mirrors every other best-effort branch on this leg.
    """
    ledger_path = _sender_sent_ledger_path(sender_worktree)
    try:
        return locked_rmw(
            ledger_path, lambda old_text: old_text,
            repo_root=sender_worktree, missing_ok=True,
        )
    except (OSError, LockTimeout, RuntimeError) as exc:
        _LOG.warning(
            "memo_send: could not read sender ledger %s under lock (%s) — "
            "the sender-side ledger commit leg is best-effort and never "
            "turns a successful send into a failure.",
            ledger_path, exc,
        )
        return None


def _commit_ledger_once(sender_worktree: Path, content: str) -> git_native.GitResult:
    """One `commit_authored_content` attempt against `sender_worktree`,
    committing EXACTLY `content` at `_SENT_LEDGER_RELPATH` — sync (subprocess
    via `git_native._git`), always called through `asyncio.to_thread` by this
    leg's async callers (DR-211 D4; never blocks the event loop).

    Precedent for the msg-file + cleanup shape: `queue_close._commit_close`.
    """
    message = (
        f"memo.send: append sent-ledger row(s) in {_SENT_LEDGER_RELPATH}\n"
    )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(message)
        msg_path = fh.name
    try:
        return git_native.commit_authored_content(
            _SENT_LEDGER_RELPATH, content, msg_path, sender_worktree,
        )
    finally:
        try:
            Path(msg_path).unlink()
        except OSError:
            pass


async def _commit_sender_ledger(sender_worktree: Path, ledger_text: str) -> Optional[str]:
    """Commit `ledger_text` (the exact, already-read bytes of the sender's
    `sent-ledger.jsonl`) into the sender's own tree; return the new commit
    SHA on success, else `None`.

    Async wrapping (DR-211 D4): `commit_authored_content` is a private-index/
    CAS/trailer-replay Python sequence, not a shell-out — it is wrapped via
    `asyncio.to_thread`, never reimplemented via `asyncio.create_subprocess_exec`
    (that hand-rolled form is this file's OWN local precedent for
    `_commit_delivered_memo`, which shells out to plain `git` directly; this
    leg does not).

    CAS-failure handling: `commit_authored_content`'s compare-and-swap
    `update-ref` fails loud with a distinctive diagnostic
    (`_LEDGER_CAS_FAILURE_SIGNATURE`) when HEAD moved concurrently since the
    private index was seeded — the EXPECTED failure mode on this shared,
    multi-session machine, not an exotic one. On exactly that failure, the
    ledger is re-read under a FRESH `locked_rmw` critical section
    (`_read_sent_ledger_locked`) and the commit is retried EXACTLY ONCE
    against the new HEAD (mirrors `_commit_delivered_memo_with_retry`'s own
    one-retry shape on the receiver leg). Any other failure — most notably
    the non-retryable HEAD-existence refusal on a fresh, untracked-ledger
    clone — falls straight through to the WARNING-and-continue branch
    without a retry.

    Never raises: the receiver-side delivery is already a durable fact by
    the time this leg runs (see module docstring negative-spec and
    `_commit_delivered_memo`'s own never-raise contract, the identical
    rationale on the sibling leg) — every failure, including an unexpected
    exception from the thread-offloaded call itself, logs at WARNING and
    returns `None` rather than propagating.
    """
    try:
        result = await asyncio.to_thread(_commit_ledger_once, sender_worktree, ledger_text)
    except Exception as exc:  # never-raise contract — see docstring
        _LOG.warning(
            "memo_send: sender ledger commit raised unexpectedly in %s (%s: %s) "
            "— the send stays successful; this leg is best-effort.",
            sender_worktree, type(exc).__name__, exc,
        )
        return None

    if result.ok:
        return result.stdout.strip()

    reason = (result.stderr or "")
    if _LEDGER_CAS_FAILURE_SIGNATURE not in reason.lower():
        _LOG.warning(
            "memo_send: sender ledger commit in %s failed (non-retryable): %s",
            sender_worktree, reason,
        )
        return None

    _LOG.info(
        "memo_send: sender ledger commit in %s hit a concurrent HEAD move — "
        "retrying once against the new HEAD.",
        sender_worktree,
    )
    try:
        fresh_text = await asyncio.to_thread(_read_sent_ledger_locked, sender_worktree)
    except Exception as exc:  # never-raise contract — see docstring
        _LOG.warning(
            "memo_send: sender ledger CAS-retry re-read raised unexpectedly in "
            "%s (%s: %s) — the send stays successful; this leg is best-effort.",
            sender_worktree, type(exc).__name__, exc,
        )
        return None
    if fresh_text is None:
        return None  # _read_sent_ledger_locked already logged the reason

    try:
        retry_result = await asyncio.to_thread(_commit_ledger_once, sender_worktree, fresh_text)
    except Exception as exc:  # never-raise contract — see docstring
        _LOG.warning(
            "memo_send: sender ledger commit retry raised unexpectedly in %s "
            "(%s: %s) — the send stays successful; this leg is best-effort.",
            sender_worktree, type(exc).__name__, exc,
        )
        return None
    if retry_result.ok:
        return retry_result.stdout.strip()

    _LOG.warning(
        "memo_send: sender ledger commit retry in %s also failed: %s",
        sender_worktree, retry_result.stderr,
    )
    return None


# ---------------------------------------------------------------------------
# DEC-3/C7 — 1->N fan-out over the single-receiver path
# ---------------------------------------------------------------------------

def _generate_campaign_id(topic: str) -> str:
    """Engine-generated campaign_id fallback when a fan-out caller omits one.

    `<today>-<topic>-<8 hex chars>` — human-scannable (date + topic) with a
    short random suffix so two same-day same-topic campaigns cannot collide.
    Not itself validated as a topic slug (topic is caller-declared free text
    here, only used for readability) — callers that want a stable/predictable
    id should pass `campaign_id` explicitly instead.
    """
    return f"{datetime.date.today().isoformat()}-{topic}-{uuid.uuid4().hex[:8]}"


async def _memo_send_fan_out(params: dict, *, repo_root) -> dict:
    """1->N fan-out over `_memo_send` (DEC-3/C7): N independent,
    individually-atomic single-receiver writes sharing one campaign_id.

    NOT a new batch-write primitive — each receiver's write is a full,
    ordinary call back into `_memo_send(one_params, repo_root=repo_root)`
    with `to` rebound to that one receiver string, so every write
    independently satisfies all seven DR-214 D2 admission bounds exactly as
    a plain single-receiver send would (this loop is the caller-side
    iteration DR-214 criterion 1 already permits — see module docstring).

    Failure mode: best-effort, fail-loud-PER-receiver. A failure on receiver
    K (whether an exception, a setup-error envelope, or an act-path
    collision/gitignore/write refusal) does NOT abort receivers K+1..N —
    every receiver is attempted exactly once and gets exactly one manifest
    entry.

    campaign_id: caller-supplied (params["campaign_id"]) or, when absent,
    generated once here via `_generate_campaign_id` and shared by every
    receiver in this invocation. Threaded into each per-receiver call so
    `_compose_memo` persists it to disk on every SUCCESSFUL write (DEC-3 —
    the rag compliance query's soundness depends on this being an on-disk
    field, not transient send-time output only).

    Returns an envelope extending the standard exit_code/mode/dry_run/
    candidates/acted/skipped/failed shape with two additional keys:
        campaign_id (str): the shared id stamped on this invocation.
        manifest (list[dict]): one {receiver, outcome, error, campaign_id}
            entry per receiver, in `to` order — outcome is one of
            "delivered" / "previewed" (dry_run) / "error". This is what
            lets a caller (or a rag compliance query, DEC-3) distinguish
            not-yet-acted (delivered, no disposition yet) from
            never-delivered (write failed) receivers.

    Negative-spec: does NOT retry a failed receiver, does NOT reorder
    receivers, and does NOT short-circuit on the first failure — every
    entry in `to` gets exactly one attempt and exactly one manifest row.
    """
    to_list = params.get("to")
    dry_run = params.get("dry_run")
    if not isinstance(dry_run, bool):
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.send: dry_run must be bool, got " + repr(type(dry_run).__name__),
        )
    if not isinstance(to_list, list) or not to_list:
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.send: to must be a non-empty string (single receiver) or a "
            "non-empty list of receiver-id strings (1->N fan-out, DEC-3/C7)",
        )
    bad_entries = [r for r in to_list if not isinstance(r, str) or not r.strip()]
    if bad_entries:
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.send: to[] fan-out list contains non-string/empty entries: "
            f"{bad_entries!r}",
        )
    if len(to_list) != len(set(to_list)):
        dupes = sorted({r for r in to_list if to_list.count(r) > 1})
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.send: to[] fan-out list contains duplicate receiver(s) "
            f"{dupes} — each receiver must appear at most once per campaign",
        )

    campaign_id = params.get("campaign_id")
    if campaign_id is not None and (
        not isinstance(campaign_id, str) or not campaign_id.strip()
    ):
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.send: campaign_id must be a non-empty string when supplied",
        )
    topic_for_id = params.get("topic") if isinstance(params.get("topic"), str) else "campaign"
    if not campaign_id:
        campaign_id = _generate_campaign_id(topic_for_id)

    manifest: list[dict] = []
    candidates: list[dict] = []
    acted: list[dict] = []
    failed: list[dict] = []
    any_ledger_appended = False

    # Review: code-reviewer (Finding 3) — ONE today() capture shared by every
    # receiver in this campaign, threaded through via the `today` kwarg (see
    # _memo_send's docstring) rather than a `today` wire-param — a receiver
    # processed after local midnight must not land on a different
    # filename/`created:` date than a receiver processed before it under the
    # same campaign_id.
    today = datetime.date.today().isoformat()

    for receiver in to_list:
        one_params = dict(params)
        one_params["to"] = receiver
        one_params["campaign_id"] = campaign_id

        # `_memo_send`'s single-receiver error paths route their diagnostic
        # TEXT through stderr/logging only — the frozen wire envelope
        # (build_setup_error_result / build_act_result) deliberately carries
        # no top-level 'reason' field for setup-class refusals (see
        # _common.build_setup_error_result docstring). Capturing stderr here
        # is how this NEW aggregate (fan-out-only) envelope recovers that
        # already-emitted diagnostic for its own per-receiver manifest —
        # it does not change or expand the single-receiver contract itself.
        captured_stderr = io.StringIO()
        try:
            with contextlib.redirect_stderr(captured_stderr):
                # C2 fan-out constraint (plan body, not an executor choice):
                # the sender-side ledger commit leg is hoisted OUT of this
                # per-receiver call (_defer_ledger_commit=True) and fired
                # exactly once below, after every receiver in this campaign
                # has appended its own ledger row — otherwise this loop would
                # fire N sequential commit_authored_content CAS round-trips
                # (and N auto-push replays) for one operator gesture. A
                # direct, non-fan-out caller of `_memo_send` never sets this
                # kwarg and keeps firing the commit inline (see `_memo_send`).
                one_result = await _memo_send(
                    one_params, repo_root=repo_root, today=today,
                    _defer_ledger_commit=True,
                )
        except Exception as exc:  # best-effort, fail-loud-PER-receiver
            reason = f"{type(exc).__name__}: {exc}"
            failed.append({"id": receiver, "reason": reason})
            manifest.append({
                "receiver": receiver, "outcome": "error",
                "error": reason, "campaign_id": campaign_id,
            })
            continue

        exit_code = one_result.get("exit_code") if isinstance(one_result, dict) else None
        # Review: code-reviewer (P2-2) — track whether ANY receiver in this
        # campaign actually appended a ledger row, so the post-loop commit
        # below fires only when there is something new to commit. Checking
        # `ledger_text is not None` after a bare re-read (the prior shape)
        # cannot distinguish "this campaign appended nothing" from "some
        # earlier campaign already wrote the ledger" — this flag can.
        if isinstance(one_result, dict) and one_result.get("_ledger_appended"):
            any_ledger_appended = True
        if exit_code == 0:
            if dry_run:
                candidates.extend(one_result.get("candidates") or [])
                manifest.append({
                    "receiver": receiver, "outcome": "previewed",
                    "error": None, "campaign_id": campaign_id,
                })
            else:
                acted.extend(one_result.get("acted") or [])
                manifest.append({
                    "receiver": receiver, "outcome": "delivered",
                    "error": None, "campaign_id": campaign_id,
                })
            continue

        one_failed = one_result.get("failed") if isinstance(one_result, dict) else None
        if isinstance(one_failed, list) and one_failed and isinstance(one_failed[0], dict):
            reason = one_failed[0].get("reason") or "memo.send: per-receiver send failed"
        else:
            stderr_text = captured_stderr.getvalue().strip()
            reason = stderr_text or (
                f"memo.send: per-receiver send failed (exit_code={exit_code!r})"
            )
        failed.append({"id": receiver, "reason": reason})
        manifest.append({
            "receiver": receiver, "outcome": "error",
            "error": reason, "campaign_id": campaign_id,
        })

    result_exit_code = 2 if failed else 0

    # C2 fan-out constraint — fire the sender-side ledger commit leg exactly
    # ONCE for the whole campaign here, after every receiver's own
    # `_append_sent_ledger` call above has landed (each was deferred via
    # `_defer_ledger_commit=True`). `repo_root=None` (direct-in-process/test
    # call path — see `_memo_send`'s own None-tolerant precedent) has no
    # sender worktree to commit in, so this leg is skipped entirely, same
    # guard the append itself already sits behind.
    sender_ledger_commit: Optional[str] = None
    # Review: code-reviewer (P2-2) — gated on `any_ledger_appended`, not just
    # `ledger_text is not None`. If every receiver in this campaign failed
    # before reaching its own append, `_read_sent_ledger_locked` returns the
    # pre-existing (unchanged) ledger text unchanged, and
    # `commit_authored_content` has no unchanged-tree short-circuit — firing
    # the commit anyway would land a genuine no-op commit for a campaign that
    # delivered nothing.
    if not dry_run and repo_root is not None and any_ledger_appended:
        _sender_worktree = main_worktree_root(Path(repo_root))
        ledger_text = await asyncio.to_thread(_read_sent_ledger_locked, _sender_worktree)
        if ledger_text is not None:
            sender_ledger_commit = await _commit_sender_ledger(_sender_worktree, ledger_text)

    # EM ruling, C2 follow-up (test_act_success_top_level_envelope_keys_unchanged
    # pins the top-level key set — no new top-level key, ever): stamp the ONE
    # campaign-wide SHA onto EVERY acted entry, as a sibling of that entry's
    # own `delivery_commit`, rather than a new top-level key. This single
    # commit genuinely covers every receiver in this campaign (it commits the
    # ledger AFTER all N appends have landed), so the same SHA on every entry
    # is correct, not a placeholder repeated by accident — and it keeps the
    # acted-entry shape identical between the single-receiver and fan-out
    # paths (both carry `sender_ledger_commit` on the entry, never above it).
    for entry in acted:
        if isinstance(entry, dict):
            entry["sender_ledger_commit"] = sender_ledger_commit

    return {
        "exit_code": result_exit_code,
        "mode": _MODE,
        "dry_run": dry_run,
        "candidates": candidates,
        "acted": acted,
        "skipped": [],
        "failed": failed,
        "campaign_id": campaign_id,
        "manifest": manifest,
    }


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

@register_op("memo.send")
async def _memo_send(
    params: dict, repo_root=None, today: str | None = None,
    _defer_ledger_commit: bool = False,
) -> dict:
    """JSON-RPC 'memo.send' MUTATING op handler (command-type, spawn-per-call).

    Write one schema-valid memo into the receiver's cross-repo/inbox/ tree,
    then COMMIT the delivered memo into the receiver repo with ALL receiver
    hooks neutralized via `-c core.hooksPath=<empty-tmpdir>` (see
    _commit_delivered_memo) — this retires DR-211 D2 criterion 3 ("send
    is non-committing") for the send op per PM directive 2026-07-21,
    mechanism amended 2026-07-21 (DR-214 amendment) per Patrik's
    approach-review REQUIRES_CHANGES. The delivery lands
    committed-but-unpushed — propagation is the receiver's own next push
    (the all-hooks-off commit also suppresses the receiver's post-commit
    hook, e.g. its own auto-push). The O_EXCL write and the commit are
    distinct steps (_write_memo_file is itself still non-committing); a
    commit failure never turns a successful write into a failed send
    (_commit_delivered_memo never raises).

    Placement: fleet/ is the write-op home (alongside archive_plans, archive_handoffs,
    prune_bugs). memo.send is a cross-tree write op fitting the fleet/ sub-package.

    repo_root arg: git common dir from _OP_KEY_SCOPE = "common_dir" (wired in C3).
    Used to derive the sender's worktree via main_worktree_root(common_dir).
    RECEIVER path is always registry-derived — never from repo_root.

    today arg: Python-level kwarg, NOT a wire-supplied param (never appears in
    _KNOWN_PARAM_KEYS / the JSON-RPC params dict) — a caller passing it through
    `params` would trip the C9 A11 unknown-param rejection. Review: code-reviewer
    (Finding 3) — `_memo_send_fan_out` captures ONE `datetime.date.today()` per
    campaign and threads it here so every receiver in a fan-out shares the same
    filename/`created:` date even if the loop straddles local midnight; a direct
    single-receiver caller omits it and this function computes today() itself.

    _defer_ledger_commit arg: Python-level kwarg (same non-wire status as
    `today`), C2 (docs/plans/2026-08-06-memo-send-sender-side-commit-leg.md).
    When True, this call still appends the sender-side ledger row via
    `_append_sent_ledger` but does NOT fire the `commit_authored_content`
    ledger commit inline — `_memo_send_fan_out` sets this on every
    per-receiver call it makes and fires the commit itself exactly once,
    after its loop over all receivers completes (the fan-out constraint: N
    receivers must not multiply into N ledger commits). A direct
    single-receiver caller never sets this and keeps the inline commit.

    dry_run:true  → validate params + containment; read collision state; compose
                    + frontmatter self-validate (shared with act path — see the
                    inline comment above the `if dry_run:` branch); return
                    candidate preview envelope WITHOUT any filesystem write.
    dry_run:false → validate + containment + compose + frontmatter self-validate
                    (shared with act path) + B3 gitignore guard + O_EXCL write
                    + delivered-memo commit (all-hooks-off via core.hooksPath)
                    into the receiver repo.

    Params (all wire-supplied via JSON-RPC params dict):
        dry_run   (bool, required): preview (true) vs. act (false).
        topic     (str, required):  topic slug — [a-z0-9][a-z0-9-]* only.
        to        (str, required):  receiver EM identity (e.g. "project-rag-em").
        title     (str, required):  memo title.
        body      (str, required):  memo body text (empty string is permitted).
        kind      (str, required):  ask | consult | fyi | proposal — DR-214 D4 / D2-6.
        from_id   (str, optional):  sender identity; defaults to "makima-engine".
        summary   (str, required): tl;dr, non-empty, ≤120 chars — DEC-1
                                    (2026-07-24 memo-ownership-and-redesign plan)
                                    makes summary a send-time required field
                                    alongside kind; omit-and-derive is retired.
        supersedes (str | list[str], optional): prior memo topic/ref(s) this
                                    supersedes. Also the sanctioned same-date+topic
                                    re-delivery trigger (C6, footgun #5, A6) — when
                                    set AND the plain DR-026 filename would collide,
                                    the write lands at a supersedes-disambiguated
                                    filename instead of refusing (see
                                    _redelivery_filename; the list form
                                    disambiguates on the FIRST reference). A list
                                    entry that is not a non-empty string fails loud
                                    rather than being pruned — a silently-shortened
                                    supersession list leaves a live ask looking
                                    retired.
        space     (str, optional):  sender-declared thread/problem-space hint,
                                    deliberately non-authoritative and validated
                                    only for non-empty-string shape — the receiver
                                    may override it. Exists so a batch inbox pass
                                    can GROUP BY instead of reconstructing threads
                                    from bodies (see memo.blitz_buckets).
        scoped_to (dict, optional): {artifact, exactly one of version|sha, seam} —
                                    nested mapping, round-trips as YAML (C9 A11).
                                    Presence-triggered completeness (2026-07-21
                                    fix): omit entirely for a directional ask;
                                    supply the COMPLETE triple for a change-
                                    control memo — a partial triple fails loud.
        campaign_id (str, optional): shared correlation id (DEC-3/C7). A direct
                                    single-receiver caller may pass one; the
                                    fan-out path below always supplies one.
        in_reply_to (str, optional): basename (or path, normalized to
                                    basename) of the inbound memo this send
                                    replies to — must name a memo present in
                                    THIS repo's own cross-repo/inbox/ or
                                    cross-repo/archive/ (recursive), verified
                                    before any receiver-tree write (see
                                    _validate_in_reply_to_exists). Consumed by
                                    coordinator_core.pickup_assemble's
                                    _candidate_is_linked reply-closure check.

    `to` may ALSO be a non-empty list of receiver strings (DEC-3/C7 1->N
    fan-out) — that shape is intercepted at the very top of this function,
    BEFORE any of the single-receiver validation/params above runs, and
    routed to `_memo_send_fan_out`. Everything below this point (params
    validation onward) is the single-receiver path; the fan-out path never
    duplicates it — it calls right back into this same function once per
    receiver with `to` rebound to one string.

    Negative-spec (see module docstring for the full set):
        - Does NOT use _common.archive_and_commit for the delivered-memo
          commit — a plain scoped `git add` / `git -c core.hooksPath=<empty-tmpdir>
          commit`, see _commit_delivered_memo (retires D2 criterion 3, PM
          directive 2026-07-21; all-hooks-off mechanism per the 2026-07-21
          DR-214 amendment).
        - Does NOT grow a fleet-wide memo index (Q-d store-less-ness invariant, AC8).
        - Does NOT fall back to direct-write (Q-c HARD at facade; C3 wires refuse-when-down).
        - Does NOT accept wire-supplied paths as receiver targets (registry-enumerated only).
        - Does NOT accept a param key outside the declared set (C9 A11 — fail loud,
          never a silent exit_code:0 drop).
    """
    # ── DEC-3/C7 1->N fan-out interception (BEFORE single-receiver validation) ──
    # A list-shaped `to` is not a valid single-receiver value — reroute to the
    # fan-out helper, which itself calls back into this function once per
    # receiver (with `to` rebound to a plain string) so every write goes
    # through the SAME single-receiver path validated/tested below.
    if isinstance(params.get("to"), list):
        return await _memo_send_fan_out(params, repo_root=repo_root)

    # ── Param validation ─────────────────────────────────────────────────────
    validated = _validate_send_params(params)
    if isinstance(validated, dict):
        return validated  # exit_code:1 setup-error envelope

    (dry_run, topic, to, title, body, from_id, kind, summary, supersedes,
     scoped_to, campaign_id, in_reply_to, space, summary_cap_advisory,
     summary_over_cap_original) = (
        validated.dry_run, validated.topic, validated.to, validated.title,
        validated.body, validated.from_id, validated.kind, validated.summary,
        validated.supersedes, validated.scoped_to, validated.campaign_id,
        validated.in_reply_to, validated.space,
        validated.summary_cap_advisory, validated.summary_over_cap_original,
    )

    # ── Sender worktree derivation (ancillary; for future sender-id use) ─────
    # memo.send is common_dir-scoped (op_scopes.py:336, C3 committed), so repo_root —
    # the git common dir — is supplied on every dispatched path (i.e. every
    # `python -m coordinator_core.invoke` spawn); sender worktree is ancillary here — the
    # RECEIVER path is always registry-derived regardless.
    # Lesson: 2026-07-05-common-dir-keyed-ops-must-derive-the-wor.yaml — repo_root is
    # the .git common_dir, NOT the worktree; do NOT use repo_root as a path directly.
    # Review: patrik — stale comment reworded; C3 is committed, repo_root=None is
    # reachable only via direct in-process _memo_send(params) calls (i.e. tests).
    if repo_root is not None:
        _sender_worktree = main_worktree_root(Path(repo_root))
    else:
        _sender_worktree = None  # direct-in-process/test path only, not a pending-C3 gap

    # ── sent_by resolution (C7, docs/plans/2026-08-13-session-identity-earns-
    # its-keep.md) ────────────────────────────────────────────────────────────
    # Resolved once, up front, so the composed frontmatter, the dry_run
    # preview, the sent-ledger row, and the delivery commit trailer all agree
    # on the SAME value for this one send — never re-resolved per write site.
    sent_by = _resolve_sent_by(str(_sender_worktree) if _sender_worktree else None)

    # ── in_reply_to existence gate (BEFORE any receiver-tree write) ──────────
    # See _validate_in_reply_to_exists docstring — a typo'd in_reply_to reads
    # as closure evidence to compute_reply_closure while linking to nothing,
    # so this fails loud before anything is written to the receiver tree.
    if in_reply_to is not None:
        in_reply_to_error = _validate_in_reply_to_exists(dry_run, _sender_worktree, in_reply_to)
        if in_reply_to_error is not None:
            return in_reply_to_error

    # ── Resolve receiver inbox + build containment allowed-set ────────────────
    # Review: code-reviewer F4 — unpack receiver_repo_path from resolver directly;
    # avoids the implicit inbox_dir.parent.parent structural navigation assumption.
    # C3: resolution now lives in the shared _memo_resolver module (consumed by
    # memo.list/draft/compose too) — fail-loud, NO folder-scan fallback. Both
    # RegistryReadError (genuine registry-read failure) and AmbiguousReceiverError
    # (central-id fan-in disagreement) MUST surface as a fail-loud setup-error
    # envelope here, never as an uncaught traceback and never as a silent fallback.
    try:
        inbox_dir, receiver_repo_path, all_repos = _resolve_receiver_inbox(to)
    except RegistryReadError as exc:
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.send: machine-local registry could not be read: {exc.reason} "
            f"(no folder-scan fallback — fix the registry file or re-run "
            f"machine-local setup).",
        )
    except AmbiguousReceiverError as exc:
        return build_setup_error_result(_MODE, dry_run, f"memo.send: {exc}")
    if inbox_dir is None:
        if to.strip().lower() in _read_central_receiver_ids():
            return build_setup_error_result(
                _MODE, dry_run,
                f"memo.send: receiver {to!r} is a central receiver id "
                f"(identity.centralReceiverIds) that resolves to the DoE-claude "
                f"repo, but none of the manifest's central receiver ids is "
                f"registered in the machine-local registry. "
                f"Register the central repo first, e.g.: "
                f"machine-local set repos.doe_claude <abs-path-to-DoE-claude-repo>",
            )
        repo_key = _receiver_em_to_repo_key(to)
        # C4 (footgun #2, design-as-offers): suggest the nearest REGISTERED
        # receiver id, if any — a suggestion surface only, never an
        # auto-selected resolution. See _memo_resolver.suggest_nearest_receiver.
        suggestion = _suggest_nearest_receiver(to, all_repos)
        suggestion_clause = (
            f" Did you mean {suggestion!r}?" if suggestion else ""
        )
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.send: receiver {to!r} resolves to registry key {repo_key!r} "
            f"which is not registered in the machine-local registry."
            f"{suggestion_clause} "
            f"Register the receiver repo first: "
            f"machine-local set {repo_key} <abs-path-to-repo>",
        )

    # ── Publish-mirror path cross-check (2026-08-07 incident fix) ─────────────
    # `resolve_receiver_inbox()` above resolves purely through `repos.*` — it
    # never consults `publish.mirrors.*`, so a repo double-registered as BOTH
    # an ordinary `repos.<key>` receiver AND a `publish.mirrors.<key>` OSS
    # mirror (with or without `.owner` set) resolved and delivered here
    # uncaught. `publish_mirror_path_match()` is path-based and
    # OWNER-INDEPENDENT (fires on `.path` alone), unlike the CLI's
    # `.owner`-gated `_is_publish_target_em` — closing exactly the gap that
    # let two memos land in `claude-klabauter`'s `cross-repo/inbox/` before
    # `publish.mirrors.claude_klabauter.owner` was ever set.
    mirror_key = _publish_mirror_path_match(receiver_repo_path)
    if mirror_key is not None:
        mirror_owner = _read_publish_mirrors().get(mirror_key, {}).get("owner")
        owner_clause = (
            f" Its owning EM is {mirror_owner!r} — send there instead."
            if mirror_owner
            else " No owner is declared for it "
            f"(publish.mirrors.{mirror_key}.owner is unset) — set that key, "
            "then re-address the memo to the declared owner."
        )
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.send: receiver {to!r} resolves to {receiver_repo_path}, "
            f"which is a declared publish mirror ({mirror_key!r}) — a "
            f"publish DESTINATION, never a working tree. A memo delivered "
            f"there sits unactioned; no EM reads it.{owner_clause}",
        )

    # ── Canonicalize the receiver identity BEFORE it is stamped (addressee gate) ──
    # `to` resolved above via whatever central/redirect alias the caller typed
    # (identity.centralReceiverIds / identity.redirectAliases both fan in to
    # the same registered repo — see _memo_resolver docstring). The frontmatter
    # `to:` field must record the ONE canonical id for that seat, not the
    # caller's literal string, so a reader can verify by inspection that two
    # differently-addressed memos went to the same receiver. Both readers this
    # canonicalization depends on already succeeded above (via
    # _resolve_receiver_inbox's own read_registry_repos() call against the
    # SAME registry file), so a RegistryReadError here would indicate the
    # registry changed underfoot between those two reads — still fail loud,
    # never silently stamp the caller's raw alias in that case.
    try:
        canonical_to = _canonical_receiver_id(to)
    except RegistryReadError as exc:
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.send: machine-local registry could not be read: {exc.reason} "
            f"(no folder-scan fallback — fix the registry file or re-run "
            f"machine-local setup).",
        )
    except AmbiguousReceiverError as exc:
        return build_setup_error_result(_MODE, dry_run, f"memo.send: {exc}")

    # ── Own-inbox refusal (invariant c) ───────────────────────────────────────
    # A repo must not write into its own inbox. memo.send is common_dir-scoped
    # (C3 committed), so repo_root — and therefore _sender_worktree — is
    # populated on every dispatched path; this guard is live in production.
    # _sender_worktree is None only via the direct-in-process/test call path
    # (repo_root not supplied), where this guard is a no-op (nothing to compare
    # against). Placed before target-path derivation and the write, alongside
    # the containment check.
    # Review: patrik — stale comment reworded; guard fires on every dispatched
    # path, the None-branch is test-only, not a pending-C3 gap.
    if _sender_worktree is not None:
        if _sender_worktree.resolve() == receiver_repo_path.resolve():
            return build_setup_error_result(
                _MODE, dry_run,
                f"memo.send: own-inbox refusal: sender repo {_sender_worktree} == "
                f"receiver repo {receiver_repo_path} — a repo must not write into "
                f"its own inbox.",
            )

    # ── scoped_to.sha resolvability gate (F14 fix — BEFORE any receiver-tree
    # write) ───────────────────────────────────────────────────────────────
    # See `_verify_scoped_to_sha_resolvable`'s own docstring/module-comment
    # block for the unreachable-vs-unresolvable split and the standing
    # 2026-08-03 ruling this does NOT reintroduce (an absent scoped_to.sha
    # still never blocks). Placed here — after receiver_repo_path is resolved,
    # before target_path/_write_memo_file/_commit_delivered_memo — so a
    # definitively-false pin is refused before anything is written into the
    # receiver's tree, not merely before the success banner.
    sha_resolve_error = await _verify_scoped_to_sha_resolvable(
        dry_run, receiver_repo_path, scoped_to,
    )
    if sha_resolve_error is not None:
        return sha_resolve_error

    # ── Derive target path ────────────────────────────────────────────────────
    # Review: code-reviewer F3 — single today() call; filename date and created: frontmatter
    # field are derived from the same value, preventing midnight-boundary divergence.
    # Review: code-reviewer (Finding 3, cross-call) — `today` may arrive as a
    # caller-supplied override (see the `today` kwarg docstring above); a
    # single-receiver caller (today=None) still gets exactly one today() call.
    if today is None:
        today = datetime.date.today().isoformat()
    # DR-026: sender-namespaced filename — <date>-<sender-slug>-<topic>.md.
    try:
        filename = _memo_filename(today, from_id, topic)
    except ValueError as exc:
        return build_setup_error_result(_MODE, dry_run, f"memo.send: {exc}")
    target_path = inbox_dir / filename
    # receiver_repo_path is registry-derived from _resolve_receiver_inbox (F4 — no .parent.parent).

    # ── Sanctioned supersedes: re-delivery disambiguation (C6, footgun #5, A6) ─
    # If the natural DR-026 filename already collides AND the caller declared
    # `supersedes:`, this is the sanctioned re-delivery path — write a FRESH
    # dated file (PM decision 2026-07-21) rather than refuse or hand-edit the
    # receiver's already-delivered file. Disambiguated by the supersedes
    # reference itself, not a nonce (see _redelivery_filename docstring).
    # Without `supersedes:`, an identical collision is completely unchanged —
    # still refused below (C1 D2 criterion 4, O_EXCL fail-loud).
    # Review: code-reviewer Finding 2 (2026-07-21 codereview slicememo-send-
    # deferred-review-findings) — always retarget filename/target_path to the
    # redelivery-disambiguated path once this branch is entered, not only
    # when it's collision-free. Previously the reassignment was gated behind
    # `if not redelivery_path.exists()`, so a RESIDUAL collision (two
    # redeliveries superseding the same prior memo on the same day) left
    # filename/target_path pointing at the original base DR-026 file — the
    # generic collision check below then reported the base filename as the
    # contested path, not the redelivery file that actually blocked the
    # second attempt. Retargeting unconditionally lets the collision check
    # further down report the TRUE contested path either way (fresh
    # redelivery: no collision, proceeds; residual collision: refuses with
    # the correct redelivery filename/id).
    if supersedes and target_path.exists():
        redelivery_filename = _redelivery_filename(today, from_id, topic, supersedes)
        redelivery_path = inbox_dir / redelivery_filename
        filename = redelivery_filename
        target_path = redelivery_path

    # ── Registry-enumerated containment check (MUST run before any filesystem op) ──
    # Lesson: 2026-07-05-externally-triggered-ops-must-contain-wi.yaml
    # Precedent: pcore-11 traversal-rejection 5296973
    # Catches absolute-override and ../ traversal via wire-supplied 'to'.
    containment_err = _containment_check(inbox_dir, target_path, all_repos)
    if containment_err:
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.send: containment-rejected: {containment_err}",
        )

    # ── Read-before-write: check collision state (AC7) ───────────────────────
    # Read the collision state BEFORE any write — both dry_run and act paths read this.
    collision_exists = target_path.exists()

    # ── Compose schema-valid memo (D2 criterion 6; invariant b self-check) ───
    # Composed BEFORE the dry_run branch, deliberately: --dry-run must run the SAME
    # frontmatter self-validation the act path enforces. A dry-run that greens a payload
    # the real send fail-loud-rejects is a false green, and catching exactly that before
    # the write is the whole contract of --dry-run. Composition is pure (no I/O, no
    # mutation), so running it on the preview path costs nothing and mutates nothing.
    try:
        content = _compose_memo(
            from_id=from_id,
            to=canonical_to,
            topic=topic,
            title=title,
            body=body,
            kind=kind,
            summary=summary,
            supersedes=supersedes,
            today=today,   # Review: code-reviewer F3 — single capture from above, no second today()
            scoped_to=scoped_to,
            campaign_id=campaign_id,   # DEC-3/C7 — persisted to disk when supplied
            in_reply_to=in_reply_to,
            space=space,
            sent_by=sent_by,   # C7 — resolved once above, same value on every write site
        )
    # Review: code-reviewer — broadened from ValueError-only: _compose_memo's
    # _render_extra_field → _render_yaml_block → _yaml_scalar call chain raises
    # TypeError (not ValueError) for an unsupported scalar type. Unreachable
    # today (_validate_scoped_to already constrains every scoped_to sub-value
    # to str/None), but the hoist above now exposes this except to BOTH
    # dry_run and act, so a future extra field without equally strict
    # pre-validation must fail loud here rather than crash the preview path.
    except (ValueError, TypeError) as exc:
        return build_setup_error_result(_MODE, dry_run, str(exc))

    # ── dry_run preview (mutates nothing) ─────────────────────────────────────
    if dry_run:
        return build_dry_run_result(_MODE, [{
            "id": str(target_path),
            "topic": topic,
            "receiver": canonical_to,
            "target_path": str(target_path),
            "collision": collision_exists,
            "note": (
                "collision: would refuse on act (C1 D2 criterion 4 fail-loud, no clobber)"
                if collision_exists else None
            ),
            # Additive, non-fatal notice (2026-08-07 warn-and-substitute
            # PM ruling, AC9) — present iff the explicit `summary` param was
            # over cap; None on a clean/absent/placeholder summary. Never
            # blocks the send — see `summary_over_cap_original` for the
            # author's original text, echoed back verbatim.
            "summary_cap_advisory": summary_cap_advisory,
            "summary_over_cap_original": summary_over_cap_original,
        }])

    # ── act path ──────────────────────────────────────────────────────────────

    # Fail-loud on collision (C1 D2 criterion 4; ratified fail-loud semantics;
    # DoE-normative 2026-07-05; see DR-214). Pre-check gives a clean error envelope;
    # _write_memo_file's O_EXCL is the atomic guard against a race.
    if collision_exists:
        return build_act_result(_MODE, [], [], [{
            "id": str(target_path),
            "reason": (
                f"collision: {filename!r} already exists in receiver inbox "
                f"{inbox_dir} — refuse (C1 D2 criterion 4 fail-loud, no clobber). "
                f"Choose a distinct topic or remove the existing file."
            ),
        }])

    # ── B3 gitignore delivery guard (D2 criterion 7; runs before write) ───────
    # Port from cross-repo-memo CLI (bin/cross-repo-memo:1244-1258).
    rel_inbox_path = os.path.join("cross-repo", "inbox", filename)
    is_ignored = await _git_check_ignore(receiver_repo_path, rel_inbox_path)
    if is_ignored:
        return build_act_result(_MODE, [], [], [{
            "id": str(target_path),
            "reason": (
                f"gitignore-delivery-guard: {filename!r} is gitignored in receiver repo "
                f"{receiver_repo_path} — fix the receiver .gitignore before delivering. "
                f"(B3 guard, D2 criterion 7, DoE Ask-1 concurrence condition 3)"
            ),
        }])

    # `content` was composed and self-validated above the dry_run branch — both paths
    # share one composition, so the preview cannot green a payload the act path rejects.

    # ── O_EXCL atomic write (this step alone is non-committing) ──────────────
    # The commit is a distinct step performed AFTER this write succeeds —
    # see _commit_delivered_memo below (retires D2 criterion 3 for the op as
    # a whole, PM directive 2026-07-21).
    try:
        _write_memo_file(target_path, content)
    except FileExistsError:
        # Race: file appeared between pre-check and write — still fail-loud (C1 D2 criterion 4).
        return build_act_result(_MODE, [], [], [{
            "id": str(target_path),
            "reason": (
                f"collision (race): {filename!r} appeared between collision-check and "
                f"O_EXCL write — refuse (C1 D2 criterion 4, no clobber)."
            ),
        }])
    except OSError as exc:
        return build_act_result(_MODE, [], [], [{
            "id": str(target_path),
            "reason": f"write-failed: {exc}",
        }])

    _LOG.info(
        "memo_send: delivered memo topic=%r → %s (from=%r receiver=%r)",
        topic, target_path, from_id, to,
    )

    # ── Delivered-memo commit (retires D2 criterion 3, PM directive 2026-07-21) ─
    # The file is already durably written above — a commit failure here must
    # NOT turn this successful delivery into a failed send (never-raise
    # contract; see _commit_delivered_memo docstring). rel_inbox_path was
    # computed above for the B3 gitignore guard and is reused here as the
    # commit's scoped pathspec.
    commit_outcome, commit_retried = await _commit_delivered_memo_with_retry(
        receiver_repo_path, rel_inbox_path, from_id, title, sent_by=sent_by,
    )
    if not commit_outcome.committed:
        _LOG.warning(
            "memo_send: delivered memo topic=%r → %s was written but could NOT be "
            "committed into receiver repo %s — see prior WARNING for the git failure.",
            topic, target_path, receiver_repo_path,
        )
    else:
        _LOG.info(
            "memo_send: delivered memo topic=%r committed into receiver repo %s "
            "on branch %r (all-hooks-off via core.hooksPath, committed-but-unpushed)%s",
            topic, receiver_repo_path, commit_outcome.branch,
            " [after one index.lock retry]" if commit_retried else "",
        )

    # delivery_commit (AC1, AC2 — pinned contract, plan body above): carries
    # the commit outcome onto the acted entry so a caller can see/branch on
    # WHY a delivery landed uncommitted, rather than the reason dying in
    # _LOG.warning. `retried` (C2) is True iff the index.lock retry actually
    # fired, regardless of whether that retry itself succeeded.
    delivery_commit = {
        "committed": commit_outcome.committed,
        "branch": commit_outcome.branch,
        "reason": commit_outcome.reason,
        "retried": commit_retried,
        "sha": commit_outcome.committed_sha,
    }

    # ── Sender-outbox sent-stamp (write-back onto the SENDER's own draft) ────
    # Runs AFTER the write above has already succeeded (never before — see
    # _stamp_sender_outbox_sent's docstring for why this ordering is the
    # whole point) and regardless of the receiver-repo commit's own
    # best-effort outcome, matching this op's own "delivered" boundary
    # (build_act_result below reports success on a successful write
    # independent of commit_outcome). _sender_worktree is None only on the
    # direct-in-process/test call path (repo_root not supplied) — same
    # None-tolerant posture as the own-inbox guard above.
    #
    # Scope-touch declaration (2026-08-05 engine-ops-declare-what-they-write
    # plan, C1): only the SENDER-side state/memo-outbox/ paths this op
    # actually wrote on THIS invocation are ever appended — never the
    # receiver-side target_path, which lands in a SIBLING repo's tree and is
    # outside this caller's own _origin_worktree (ipc.py's 2026-08-04 F1
    # cross-repo containment fix would drop it anyway; it is not attempted
    # here). The stamp and the ledger append are each conditional/best-effort,
    # so each is declared only when its own helper reports it actually wrote.
    touched_outbox_paths: list[str] = []
    sender_ledger_commit: Optional[str] = None
    ledger_appended = False
    if _sender_worktree is not None:
        stamped = _stamp_sender_outbox_sent(
            _sender_worktree, topic, target_path, receiver_repo_path,
        )
        if stamped:
            touched_outbox_paths.append(str(_sender_outbox_path(_sender_worktree, topic)))
        # Review: code-reviewer (P2-1) — `_append_sent_ledger`'s `locked_rmw`
        # call acquires a cross-process flock with up to LOCK_TIMEOUT_SECS of
        # wait (unlike the plain unlocked `open(mode="a")` it replaced), so it
        # is thread-wrapped here on the same footing as every other blocking
        # git_native call in this handler — running it inline would block the
        # event loop under this repo's stated lock-contention load.
        ledger_text = await asyncio.to_thread(
            _append_sent_ledger,
            _sender_worktree,
            topic=topic,
            to=canonical_to,
            kind=kind,
            delivered_path=target_path,
            receiver_repo_path=receiver_repo_path,
            in_reply_to=in_reply_to,
            delivery_commit_reason=delivery_commit.get("reason"),
            delivery_commit_retried=delivery_commit.get("retried"),
            delivery_commit_sha=delivery_commit.get("sha"),
            sent_by=sent_by,
        )
        ledger_appended = ledger_text is not None
        if ledger_appended:
            touched_outbox_paths.append(str(_sender_sent_ledger_path(_sender_worktree)))
            # C2 — commit the ledger append into the SENDER's own tree via
            # commit_authored_content (never a mirror of
            # _commit_delivered_memo's hand-rolled add/commit, see the
            # module-level "C2" section above for the full rationale).
            # `ledger_text` is the EXACT bytes `_append_sent_ledger` just
            # returned from its own single `locked_rmw` critical section —
            # no separate, unlocked read-back. A concurrently-appended
            # peer's row can legitimately ride along here: that peer's
            # append landed and released the lock before this critical
            # section started, so it is already an immutable, durable-on-
            # disk fact (a completed delivery, not someone else's
            # uncommitted work-in-progress) — `locked_rmw` closes the
            # torn-read case, not this one, and this is expected/tolerable
            # per the plan's C1 DR-272 amendment.
            #
            # `_defer_ledger_commit` (fan-out hoist, C2 plan body): a
            # per-receiver call from `_memo_send_fan_out` still appends its
            # own row above, but the actual commit is skipped here and
            # fired exactly once by the fan-out caller after its whole loop
            # completes — otherwise N receivers would fire N sequential
            # commit_authored_content CAS round-trips (and N auto-push
            # replays) for one operator gesture.
            if not _defer_ledger_commit:
                sender_ledger_commit = await _commit_sender_ledger(
                    _sender_worktree, ledger_text,
                )

    result = build_act_result(
        _MODE,
        [{
            "id": str(target_path),
            "written": True,
            "receiver": canonical_to,
            "topic": topic,
            "delivery_commit": delivery_commit,
            # C2 (EM ruling, C2 follow-up) — a sibling of delivery_commit on
            # this SAME acted entry, deliberately never a new top-level
            # envelope key: test_act_success_top_level_envelope_keys_unchanged
            # pins the top-level key set to exactly
            # exit_code/mode/dry_run/candidates/acted/failed, the same
            # boundary delivery_commit itself already respects by living
            # here rather than above. Own key, never overloading
            # delivery_commit (which names the RECEIVER-side commit outcome
            # above) — None when the leg was skipped (_sender_worktree is
            # None, the ledger append itself failed, or
            # _defer_ledger_commit deferred it to the fan-out caller, which
            # stamps this same key onto every acted entry in its own
            # campaign after its loop completes — see _memo_send_fan_out).
            "sender_ledger_commit": sender_ledger_commit,
            # Additive, non-fatal notice (2026-08-07 warn-and-substitute
            # PM ruling, AC9) — mirrors the dry_run candidate's own pair of
            # fields above; present iff the explicit `summary` param was
            # over cap on this delivered memo.
            "summary_cap_advisory": summary_cap_advisory,
            "summary_over_cap_original": summary_over_cap_original,
        }],
        [],
        [],
    )
    if touched_outbox_paths:
        result["_scope_touch_paths"] = touched_outbox_paths
    if _defer_ledger_commit:
        # Review: code-reviewer (P2-2) — non-wire, internal-only signal
        # consumed ONLY by `_memo_send_fan_out` (which always sets
        # `_defer_ledger_commit=True`) to know whether THIS receiver's call
        # actually appended a ledger row, so the fan-out's post-loop commit
        # can be skipped when no receiver in the campaign appended anything
        # (an unconditional post-loop commit would otherwise land a genuine
        # no-op commit). Never set on the direct, non-fan-out call path —
        # test_act_success_top_level_envelope_keys_unchanged pins the
        # top-level key set for that path.
        result["_ledger_appended"] = ledger_appended
    return result
