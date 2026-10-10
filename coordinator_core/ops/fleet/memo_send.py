"""
coordinator_core.ops.fleet.memo_send — memo.send MUTATING op handler.

Purpose: deliver an already-staged `state/memo-outbox/<topic>.md` draft
(`memo.draft` / `memo.compose`) into a registry-enumerated receiver's
`cross-repo/inbox/` tree, then land the sender-side receipt — the rebuild
following the 2026-08-23 kill (kill-ledger K-050, 30,015.7ms max / 7,133.5ms
p50, n=20, 94% breach). **This is not the killed implementation restored** —
per CLAUDE.md § brightline ("kill means kill forever"), the fan-out/campaign
machinery, the self-receipt arm and the HTTP/UDS transport gating the old
3,623-line module carried do NOT come back. What comes back is the PM's own
three-write requirement:

  1. Receiver: `_receiver_landing.resolve_receiver_landing` picks the ref the
     delivery lands on (DR-214 A3): HEAD's work branch, or the receiver's day
     branch (minted if absent; HEAD moved onto it only off a clean `main`).
     Then write `cross-repo/inbox/<name>.md` (O_EXCL) in the receiver's own
     repo, commit it via `git_native.commit_authored_new_file` and record it
     in the receiver's index in process (`_record_delivery_in_receiver_index`)
     — ZERO git spawns on the green path, no hook from the receiver's tree
     ever fires (AC3). In the `ref-direct` arm (HEAD stays on another branch)
     there is no worktree write or index record: the commit goes onto the
     day-branch ref and the anchor carries the bytes. The verified delivery
     registers its branch with `push_cadence.note_foreign_delivery`; the push
     is the warm engine's, never inline.
  2. Sender: move `state/memo-outbox/<topic>.md` -> `sent/`, deriving the
     sent-copy's `status: sent` / `sent_at:` / `delivered_to:` stamp from
     the draft's OWN frontmatter (never re-authored).
  3. Sender commit: one `git.commit.commit_paths` call over the three
     sender-side paths (new `sent/` file, deleted outbox original, appended
     ledger row) — zero git spawns, and it commits WORKTREE bytes. That
     second property is load-bearing, not incidental: the sent-ledger is a
     fleet-shared bounded ring (`_SENT_LEDGER_MAX_ROWS`) whose worktree copy
     `locked_rmw` keeps as the union of every session's appends, while a
     staged blob for it goes
     stale the moment a peer appends. `commit_scoped`, which this replaced,
     committed the STAGED blob and so replayed stale ledger snapshots — see
     the call site's own note for the 2026-08-30 measurement.

Ordering is load-bearing: the receiver-side commit lands BEFORE the anchor
write (C4), which lands BEFORE the sender's receipt is written. A receipt
for an undelivered memo is a lie; an uncredited delivery is merely untidy —
the durable record is the `refs/coordinator/inbox/*` anchor `write_anchor`
lands in the receiver's own object store (`_memo_anchor.py`, C3), which
survives the receiver's own branch gestures and a `git gc`, not a re-read of
the receiver's own inbox (a branch delete/reset/recreate can take that inbox
file with it) — see `_memo_send`'s call order.

Spec backlink:
    docs/plans/2026-08-25-memo-send-three-writes-and-one-commit-th.md § C2
    Deleted original's own contract (frontmatter shape, MUTATES declaration):
        `git show 677d433eb -- coordinator_core/ops/fleet/memo_send.py`
    DR-214: docs/decisions/DR-214-send-class-cross-tree-write-boundary.md

Negative-spec:
  - Does NOT accept `title`/`body`/`kind`/`summary` as wire params — every
    field comes from the CALLER's own already-staged
    `state/memo-outbox/<topic>.md` draft (`memo.draft`/`memo.compose`).
    Params are `dry_run` + `topic` only; nothing else is declared or read.
  - Does NOT fan out to multiple receivers, generate a `campaign_id`, write
    a self-receipt, or accept any transport-gating param — all retired with
    the kill, not ported (Out of scope in the governing plan).
  - Does NOT commit to the receiver's `main` or push inline (DR-214 A3): a
    delivery lands on a `work/*` ref only, and the push is registered, not run.
  - Does NOT fall back to a spawning, hook-running commit in the receiver's
    tree when `commit_authored_new_file` declines (AC4) — a decline fails
    the receiver item loud; the sender-side receipt is never written for
    that item (see the ordering note above). A decline also leaves NO file
    behind: the O_EXCL write this op made is rolled back, so the receiver's
    tree is exactly as it was found and a re-run is not blocked by AC6's
    no-clobber guard tripping over this op's own orphan.
  - Does NOT deliver an unattributed memo SILENTLY — when `sent_by` lands as
    the sentinel, `acted[0].sender_unattributed` says so on the result
    envelope and `cross-repo-memo send` prints it. The delivery still
    succeeds (an un-nameable sender is a degraded memo, not a failed one),
    but it is never invisible.
  - Does NOT overwrite a `sent_by` the draft already carries — that value is
    threaded straight through. Send time is where session identity is
    RESOLVED (2026-08-13 session-identity contract C7): the draft/compose
    pair deliberately never writes the field, so on the ordinary path this op
    resolves it itself. `_SENT_BY_UNRESOLVED` is the explicit sentinel for a
    resolution FAILURE, never silent omission — and never the ordinary case.
  - Does NOT silently drop a draft's `cc:` field (klabauter#46) — a present
    `cc:` (string or list of strings) is stamped onto the delivered content
    after `to:` (see `_stamp_cc`) so it survives the send; a malformed one
    (wrong type, empty string/list, non-string list entry) refuses the send
    loud (see `_validate_cc`) rather than composing without it.
  - Does NOT stop at stamping `cc:` into the copy already going to `to:`
    (klabauter#46, second half) — each `cc:` name is resolved via the SAME
    `memo_wire.resolve_receiver` `to:` uses (see `_resolve_cc_targets`) and gets
    its OWN receiver-side write + commit + anchor (see `_deliver_cc_copy`),
    in the SAME repo, with the SAME cc-stamped content `to:` receives. An
    unresolvable cc name refuses the WHOLE send loud, before any write —
    mirrors `to:`'s own UNKNOWN RECEIVER refusal exactly, so a cc leg is
    held to the same "resolvable before any byte moves" bar `to:` already
    was. A cc naming a publish mirror or a redirect alias is delivered to
    its owner, exactly as `to:` is: `memo_wire.resolve_receiver` does the
    rerouting for both. Once every cc name resolves, a PER-RECEIVER write
    failure (collision, declined/unverified commit) after `to:` has already
    landed does NOT undo `to:`'s delivery or fail the whole send — it is
    reported in the envelope's `failed[]` (DETERMINATE-PARTIAL, exit_code 2)
    exactly like a downstream sender-receipt failure already is, never
    dropped silently.
  - Does NOT silently drop a draft's `distill_fate`/`in_repo_capture` fields
    (bug-backlog 297a3a6012a8) — present, they are stamped onto the composed
    content (`_stamp_distill_fate`) before `validate_memo_cross_fields` runs,
    so `schema_validate.py::_memo_cf_distill_fate` sees what the draft
    actually declared instead of judging a field composition already threw
    away. `_compose_memo` itself still does not know these fields (same
    footprint reasoning as `cc`, klabauter#46) — this stamps the
    already-composed content, then lets the existing validator refuse a
    malformed value loud.
  - Does NOT overwrite an existing receiver-inbox file — refused twice,
    independently: an existence pre-check AND the `O_EXCL` open flag (AC6).
  - Does NOT trust a wire-supplied inbox path — `to` is resolved solely via
    `memo_wire.resolve_delivery_target` (registry-enumerated); the
    receiver-side write target is never wire-derived.
  - Does NOT allow a send whose staged body is byte-identical (frontmatter
    stripped, trailing whitespace normalised) to another `*.md` draft
    already sitting in the same sender's `state/memo-outbox/` under a
    DIFFERENT topic and the SAME receiver (`to:`) — refused via `build_setup_error_result` before any
    write, naming the colliding topic and the outbox path. Skipped only
    when the body is empty (the `--empty-body` opt-in path: two
    deliberately body-less memos are not a collision). No override flag —
    an override turns a guard into a warning, and the 2026-08-21 incident
    passed every warning it had.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import tempfile
import time
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple, Optional

from coordinator_core.session.machinery_paths import (
    LEGACY_MEMO_OUTBOX_RELDIR,
    MEMO_OUTBOX_RELDIR,
)
from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    insert_fm_field_raw,
    rebuild as _rebuild_frontmatter,
    replace_fm_field,
    serialize_yaml_scalar,
    split_frontmatter,
)
from coordinator_core.frontmatter.schema_validate import (
    fold_flat_scoped_to,
    format_validation_errors,
    parse_frontmatter,
    validate_memo_cross_fields,
)
from coordinator_core.git.commit import (
    CommitRefused,
    FilterUnsupported,
    commit_paths,
    hash_worktree_blobs_via_spawn,
)
from coordinator_core.git.commit_trailers import apply_missing_trailers
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git import git_state
from coordinator_core.git.git_objects import _read_object, read_ref_loose_or_packed, write_object
from coordinator_core.git.index_write import (
    IndexStaleAfterCommit,
    IndexWriteError,
    IndexWriteLockBusy,
    splice_index,
)
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, locked_rmw
from coordinator_core.machine_profile import feature_refusal
from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.fleet.archive_actioned_memos import memo_archive_dest
from coordinator_core.ops.fleet import _receiver_landing
from coordinator_core.warm import push_cadence
from coordinator_core.ops.fleet._common import (
    build_act_result,
    build_dry_run_result,
    build_setup_error_result,
    main_worktree_root,
)
from coordinator_core.ops.fleet._memo_anchor import (
    ANCHOR_REF_PREFIX,
    anchor_names,
    present_filenames,
    write_anchor,
)
from coordinator_core.ops.fleet._memo_compose import (
    _TOPIC_SLUG_RE,
    _compose_memo,
    _render_extra_field,
    _normalize_in_reply_to,
    body_opens_frontmatter,
)
from coordinator_core.ops.fleet.memo_wire import (
    WireRefusal,
    resolve_delivery_target,
    resolve_receiver,
)
from coordinator_core.ops.fleet._memo_summary import has_prose_body, is_placeholder_summary
from coordinator_core.ops.fleet.memo_draft import legacy_outbox_dir, outbox_dir, resolve_outbox_draft_path
from coordinator_core.ops.session_context import resolve_current_session_id
from coordinator_core.session import machinery_paths as _machinery_paths

_LOG = logging.getLogger(__name__)

# Mode constant for the envelope mode field (memo.send is a single-mode op).
_MODE = "send"

_HEADS_PREFIX = "refs/heads/"

# sent_by (2026-08-13 session-identity-earns-its-keep, C7) — explicit sentinel
# for "this send could not resolve its own session id" — a memo that cannot
# name its sender must SAY SO, never omit the field silently.
_SENT_BY_UNRESOLVED = "unresolved"


def _resolve_sent_by(fm: dict) -> str:
    """The sender session id for this send: the draft's own value if it has
    one, otherwise resolved fresh from session identity, otherwise the
    sentinel.

    Send time is the ONLY place this field is resolved. `memo.draft` and
    `memo.compose` never author it and `memo.compose` actively strips one
    (`_memo_compose` has no path that acquires it; sent_by never joins
    `_CARRIED_DRAFT_FIELDS`), so a field-authored memo reaches here with the
    field absent every time — leaving it to the caller makes the sentinel the
    only reachable outcome, which is what silently made ~49 delivered memos
    unrepliable between 2026-08-25 and 2026-08-27.

    Negative-spec: never RAISES on an unresolvable session — an un-nameable
    sender degrades the memo's repliability, it does not fail the delivery.

    VERDICT (P088-C2, verify-and-pin, no production branch change): this
    site is already warm-scoped via ``ops.session_context.resolve_current_session_id``,
    which delegates to ``session.core.attributable_session_id`` — do not
    re-resolve here. That accessor resolves tier-0 (``carried_session_id()``)
    only under a warm dispatch and the full env-tier chain
    (``resolve_session_id(cwd)``) cold, so this call already implements the
    anti-forgery, warm-scoped policy without any further swap.
    """
    carried = fm.get("sent_by")
    if isinstance(carried, str) and carried.strip():
        return carried
    return resolve_current_session_id() or _SENT_BY_UNRESOLVED


def _delivery_commit_message(topic: str, from_id: str, sent_by: str) -> str:
    """The receiver-side delivery commit's message, carrying a `Session-Id:`
    trailer when the sender is nameable.

    The trailer is the SECOND carrier of sender identity, independent of the
    memo's own `sent_by:` frontmatter, and it is the one DoE's
    `resolve-peer-address.py` declares as an input (alongside `claimed_by` on
    a handoff and `created_by_session` on a queue entry) — an inbound memo has
    no claim decision to consult, so without this the only sanctioned
    session-id -> peer-name join has nothing to read. On 2026-08-25, three
    memos whose frontmatter carrier had already failed still named their
    sender through this trailer alone; that is the case for carrying both.

    Stamped programmatically here because the receiver-side commit takes
    `git_native.commit_authored_new_file`'s zero-spawn arm, which runs no
    hooks and no `interpret-trailers` — so this does NOT ride the
    prepare-commit-msg shim that fails open when its path goes stale.

    Negative-spec: an unresolved sender means NO trailer, never a
    `Session-Id: unresolved` line — a trailer exists to be joined to an
    address, and a sentinel one would be a value the resolver must learn to
    reject. The frontmatter field is where the absence is recorded. The
    trailer is its own final paragraph, blank-line separated, or git's
    trailer parser does not see it.

    Negative-spec: a session id is durable ATTRIBUTION, not a stamped address
    — a resume or `/clear` mints a new id while the peer name and pid persist,
    so a reader must resolve this to an address at point of use and expect a
    miss. Never treat it as a promise the sender is still reachable.
    """
    message = (
        f"cross-repo memo delivery: {topic}\n\n"
        f"Delivered by memo.send from {from_id}.\n"
    )
    if sent_by != _SENT_BY_UNRESOLVED:
        message += f"\nSession-Id: {sent_by}\n"
    return message

# Param keys this handler declares; anything else fails loud rather than
# being silently dropped (mirrors the pre-kill C9/A11 fix's discipline).
_KNOWN_PARAM_KEYS = frozenset({"dry_run", "topic"})

_SENT_LEDGER_FILENAME = "sent-ledger.jsonl"

#: Rows the sent-ledger retains. It is a BOUNDED RING, not a record of
#: truth: the newest row evicts the oldest, so the file's size and the cost
#: of appending to it are constant instead of tracking every memo this fleet
#: has ever sent. At 2,405 rows / 725KB (2026-08-30) each send read the file
#: whole, rewrote it whole, then hashed and zlib-compressed it whole to add
#: one line -- ~100ms of memo.send's ~125ms, growing without limit.
#:
#: WHAT SURVIVES EVICTION, so nothing here is load-bearing for a record.
#: The durable record of a send is threefold and none of it lives in this
#: file: the delivered memo in the receiver's own tree, the delivery commit
#: in the receiver's history, and this repo's own never-evicted
#: `.coordinator-local/memo-outbox/sent/<topic>.md` copy. The one production reader that
#: treated a row as a permanent registration --
#: `fact_contract_gate.engine_gap_marker.memo_exists` -- now falls through
#: to that sent copy (`_sent_copy_has`), so an evicted row cannot turn a
#: registered engine-gap ask into rot. A new reader that needs history
#: older than this window must read one of those three, never widen this.
#:
#: 250 rows is ~2.5 days at this fleet's 2026-08 rate (~92 sends/day) and
#: ~120KB at the current ~470B/row -- sized for the question this file is
#: actually asked ("did my send land"), which is same-day. Raising it costs
#: linearly on EVERY send, in a file read, rewritten, hashed and compressed
#: whole each time; a reader needing a longer window should take one of the
#: three durable records above instead of paying for it here.
_SENT_LEDGER_MAX_ROWS = 250

#: The OTHER half of the bound, and the half that bites in most repos.
#: claude-klabauter and coordinator-content-repo are two halves of one delivery system and send
#: constantly, so the row cap above evicts for them every few days. Almost
#: every other repo sends a handful of memos a month: 250 rows there is not
#: 2.5 days, it is a year or more, and a row cap alone would leave those
#: ledgers unbounded in TIME while looking bounded. A rare sender's file
#: stays small either way -- what an age bound buys is that nothing left in
#: it is old enough to be mistaken for current.
#:
#: Applied only to rows carrying a parseable `sent_at`; a row without one is
#: left to the row cap rather than dropped on a field it never had (rows
#: predating the field, and any hand-written line). Both bounds run on every
#: append, so neither can be the one that quietly stopped applying.
_SENT_LEDGER_MAX_AGE_DAYS = 30

# Generator-provenance: writes+commits into a registry-enumerated RECEIVER
# repo's cross-repo/inbox/ tree (a different repo, not fixed), and moves
# +ledgers into the CALLING repo's own outbox tree (new
# `.coordinator-local/memo-outbox/` root; an already-staged draft found at
# the retired `state/memo-outbox/` root is deleted from there, never
# rewritten there) — a data-dependent set of tracked paths across two
# repos, never one fixed target. Recovered from the deleted original's own
# declaration (git show 677d433eb) — the plan names this as the contract to
# preserve verbatim, extended for the 2026-09-03 outbox relocation. The
# receiver-inbox leg is itself data-dependent per `memo_corpus.receiver_inbox_root`
# (C10a migration window): an unmigrated receiver still resolves to the legacy
# `cross-repo/inbox/`, a migrated one to `state/cross-repo/inbox/` — both
# patterns are named here rather than the stale single legacy literal, since
# either can be the actual write target depending on the receiver probed.
MUTATES_APPEND = [f"{MEMO_OUTBOX_RELDIR}/sent-ledger.jsonl"]
MUTATES = [
    f"{LEGACY_MEMO_OUTBOX_RELDIR}/*.md",
    "cross-repo/inbox/*.md",
    "state/cross-repo/inbox/*.md",
]


# ---------------------------------------------------------------------------
# Param validation
# ---------------------------------------------------------------------------

def _validate_send_params(params: dict):
    """Validate memo.send params; return (dry_run, topic) or a setup-error dict.

    Only `dry_run` (bool, required) and `topic` (slug, required) are
    declared — every other field this send needs comes off the caller's own
    already-staged `state/memo-outbox/<topic>.md` draft, never off the wire.
    """
    dry_run = params.get("dry_run", False)
    if not isinstance(dry_run, bool):
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.send: dry_run must be bool, got " + repr(type(dry_run).__name__),
        )

    unknown_keys = set(params.keys()) - _KNOWN_PARAM_KEYS
    if unknown_keys:
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.send: unrecognized param(s) {sorted(unknown_keys)} — known "
            f"params: {sorted(_KNOWN_PARAM_KEYS)}. memo.send reads every other "
            f"field off the staged outbox draft, never off the wire.",
        )

    topic = params.get("topic")
    if not topic or not isinstance(topic, str):
        return build_setup_error_result(
            _MODE, dry_run, "memo.send: topic is required (non-empty string)",
        )
    if not _TOPIC_SLUG_RE.fullmatch(topic):
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.send: topic {topic!r} is invalid — must match [a-z0-9][a-z0-9-]* "
            f"(lowercase alphanum and hyphens only, starting with alphanum). "
            f"Path chars (/, .., absolute paths) are not permitted.",
        )

    return dry_run, topic


# ---------------------------------------------------------------------------
# Sender-side paths
# ---------------------------------------------------------------------------

def _draft_path(sender_worktree: Path, topic: str) -> Path:
    """Resolve the staged draft: new `.coordinator-local/memo-outbox/` root
    first, retired `state/memo-outbox/` root second (2026-09-03
    relocation) — a draft staged before the repoint is still sendable."""
    return resolve_outbox_draft_path(sender_worktree, topic)


def _sent_path(sender_worktree: Path, topic: str) -> Path:
    """The sender-side sent-copy WRITE target — always the new root."""
    return Path(_machinery_paths.memo_outbox_sent_dir(str(sender_worktree))) / f"{topic}.md"


def _sent_ledger_path(sender_worktree: Path) -> Path:
    """The sent-ledger WRITE target — always the new root."""
    return Path(_machinery_paths.memo_outbox_sent_ledger_path(str(sender_worktree)))


def _portable_delivered_to_form(
    receiver_repo_path: Path,
    delivered_path: Path,
    all_repos: Optional[dict] = None,
) -> str:
    """Render `delivered_path` as the `delivered_to` receipt: `<repo-key>:<path>`.

    `<repo-key>` is the canonical `repos.*` registry key that owns
    `receiver_repo_path` and `<path>` is repo-relative with `/` separators, so
    the receipt re-resolves after a root moves. Readers that take the basename
    of the field are unaffected. A receiver with no registry key, or a
    delivered path outside it, falls back to the repo-relative path, then a
    `~/` form, then the absolute string: never a key-less guess.
    """
    try:
        rel = delivered_path.resolve().relative_to(receiver_repo_path.resolve()).as_posix()
    except (ValueError, OSError):
        rel = None
    if rel is not None:
        from coordinator_core.machine_resolver import canonical_repo_key_for_root

        key = canonical_repo_key_for_root(receiver_repo_path, all_repos or {})
        return f"{key}:{rel}" if key else rel
    try:
        home_rel = delivered_path.resolve().relative_to(Path.home().resolve())
        return "~/" + home_rel.as_posix()
    except (ValueError, OSError):
        pass
    return str(delivered_path).replace("\\", "/")


def _normalize_body(text: str) -> str:
    """Body text normalised for byte-identical duplicate-draft comparison.

    Trailing whitespace is stripped per line, then trailing blank lines are
    collapsed. The caller must already have stripped frontmatter — this
    function normalises BODY text only.
    """
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _find_duplicate_draft_topic(
    outbox_dir: Path, topic: str, normalized_body: str, receiver: object = None,
) -> Optional[str]:
    """Scan sibling `*.md` drafts directly in `outbox_dir` (non-recursive —
    `sent/` is a subdirectory and is never visited) for one whose body
    normalises byte-identical to `normalized_body` under a DIFFERENT topic
    AND the same receiver (`to:`) -- one body to two receivers is not a
    duplicate.

    Returns the colliding topic, or None. A candidate this cannot read or
    parse is skipped rather than treated as a match or a failure — a
    stray/corrupt sibling draft must not block an unrelated send.
    """
    try:
        candidates = sorted(outbox_dir.glob("*.md"))
    except OSError:
        return None
    for candidate in candidates:
        other_topic = candidate.stem
        if other_topic == topic:
            continue
        try:
            # Widened from `except OSError:`.
            # UnicodeDecodeError (raised by read_text on non-UTF-8 bytes) is a
            # ValueError subclass, not an OSError, so a single non-UTF-8
            # sibling draft used to raise straight out of this scan and block
            # an unrelated send — contradicting this function's own
            # "must not block an unrelated send" contract. parse_frontmatter
            # itself never raises (catches internally, falls back to
            # {"frontmatter": None, "body": content}), so ValueError here only
            # ever catches the decode failure — kept broad rather than
            # UnicodeDecodeError-only in case that changes.
            other_text = candidate.read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        # Dropped the dead
        # `other_body is None: continue` guard. parse_frontmatter's `body` is
        # always a str (falls back to the whole file text when frontmatter is
        # absent/unparseable), so that branch never fired; the real
        # unreadable-sibling skip is the except clause above.
        other_parsed = parse_frontmatter(other_text)
        other_fm = other_parsed.get("frontmatter") or {}
        if str(other_fm.get("to")) != str(receiver):
            continue
        other_body = other_parsed.get("body")
        if _normalize_body(other_body) == normalized_body:
            return other_topic
    return None


# ---------------------------------------------------------------------------
# Draft read + delivered-memo composition
# ---------------------------------------------------------------------------

def _read_draft(draft_path: Path) -> tuple[Optional[dict], Optional[str], Optional[str]]:
    """Read+parse the staged outbox draft. Returns (frontmatter, body, error).

    Exactly one of (frontmatter, error) is non-None on return; `body` is
    non-None iff frontmatter is.
    """
    try:
        content = draft_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None, (
            f"memo.send: no staged draft at {draft_path} — draft it first via "
            f"memo.draft, then memo.compose a body, before calling memo.send."
        )
    except OSError as exc:
        return None, None, f"memo.send: could not read draft {draft_path}: {exc}"

    parsed = parse_frontmatter(content)
    fm = parsed.get("frontmatter")
    if fm is None:
        return None, None, (
            f"memo.send: draft {draft_path} has no parseable YAML frontmatter"
        )
    return fm, parsed.get("body", ""), None


#: klabauter#46 — `cc:` used to reach `_compose_memo` nowhere (it declares no
#: `cc` parameter) and so vanished from the delivered memo with no trace: a
#: draft hand-authored with `cc: example-cockpit-repo-em` was silently delivered
#: to `to:` alone, cc-less. `_compose_memo` itself is out of this fix's
#: footprint (klabauter#46/#40 scope: memo_send.py, memo_kinds.py,
#: cross-repo-memo.py only) — instead of composing cc INTO `_compose_memo`,
#: this op stamps it onto the ALREADY-COMPOSED content as a post-processing
#: frontmatter insert, using the same `insert_fm_field`/`insert_fm_field_raw`
#: primitives `_stamp_sent_copy` already uses for `sent_at:`/`delivered_to:`.
def _validate_cc(cc: object) -> tuple[Optional[list], Optional[str]]:
    """Validate a draft's `cc:` field. Returns (normalized_list, error) —
    exactly one non-None. `None, None` when `cc` is absent (the ordinary,
    cc-less case — never an error).

    Negative-spec: NEVER silently drops a present-but-malformed `cc:` —
    an empty string, an empty list, a non-string list entry, or any type
    other than str/list refuses loud rather than composing without it
    (klabauter#46's "prefer a loud refusal over a silently dropped field").
    """
    if cc is None:
        return None, None
    if isinstance(cc, str):
        if not cc.strip():
            return None, "memo.send: draft's 'cc' field is an empty string — remove it or name a receiver"
        return [cc.strip()], None
    if isinstance(cc, list):
        if not cc:
            return None, "memo.send: draft's 'cc' field is an empty list — remove it or name a receiver"
        normalized = []
        for entry in cc:
            if not isinstance(entry, str) or not entry.strip():
                return None, (
                    f"memo.send: draft's 'cc' field carries a non-string or "
                    f"empty entry ({entry!r}) — every cc: recipient must be a "
                    f"non-empty string"
                )
            normalized.append(entry.strip())
        return normalized, None
    return None, (
        f"memo.send: draft's 'cc' field must be a string or a list of "
        f"strings, got {type(cc).__name__}"
    )


def _stamp_cc(content: str, cc: list) -> str:
    """Insert the validated `cc:` list into already-composed delivered
    content, immediately after `to:` — single-entry as a plain scalar line
    (matches every other scalar field this composer emits), multi-entry as
    an inline YAML sequence (mirrors `supersedes:`'s list form in
    `_memo_compose._render_extra_field`, which this module cannot import
    without pulling `_compose_memo`'s cc-blind composer back into the seam
    this fix threads around).
    """
    split = split_frontmatter(content)
    if split is None:
        # Unreachable in practice (content was just composed by _compose_memo,
        # which always emits parseable frontmatter) — but never corrupt a
        # memo silently: return it cc-less rather than raise past the
        # caller's own validation step.
        return content
    fm_text = split.fm_text
    if len(cc) == 1:
        fm_text = insert_fm_field(fm_text, "cc", cc[0], after_key="to")
    else:
        raw = "[" + ", ".join(serialize_yaml_scalar(entry) for entry in cc) + "]"
        fm_text = insert_fm_field_raw(fm_text, "cc", raw, after_key="to")
    return _rebuild_frontmatter(split, fm_text)


#: 2026-09-01 backlog item 297a3a6012a8 — `distill_fate`/`in_repo_capture`
#: reached `_compose_memo` nowhere (neither is a declared kwarg), so both
#: were dropped from the delivered memo with no trace: a draft carrying
#: `distill_fate: ratification` + `in_repo_capture: "~/.claude/..."` sent
#: with rc 0 and delivered WITHOUT either field, which also disarmed
#: `schema_validate.py::_memo_cf_distill_fate` — a cross-field guard cannot
#: refuse a field composition already discarded. Same shape and same fix
#: pattern as `cc` (klabauter#46, `_stamp_cc` above): stamp the already-
#: composed content post-hoc rather than widening `_compose_memo`'s kwargs
#: (out of this op's footprint). Carries the fields through UNVALIDATED —
#: `_memo_send`'s own `validate_memo_cross_fields(delivered_fm)` call,
#: immediately after `_compose_delivered_content` runs, is what re-arms the
#: guard; this function's only job is to stop discarding its input.
def _stamp_distill_fate(content: str, fm: dict) -> str:
    """Carry a draft's `distill_fate`/`in_repo_capture` fields into the
    already-composed delivered content, when present, so the cross-field
    validator the caller runs next actually sees them instead of validating
    against fields composition already dropped.

    Negative-spec: does NOT validate either value — a malformed
    `distill_fate` or a `~/.claude`-rooted `in_repo_capture` is refused by
    `validate_memo_cross_fields` downstream, not here (mirrors the ordering
    `cc` already uses: stamp first, let the existing validator judge it).
    """
    distill_fate = fm.get("distill_fate")
    in_repo_capture = fm.get("in_repo_capture")
    if distill_fate is None and in_repo_capture is None:
        return content
    split = split_frontmatter(content)
    if split is None:
        # Unreachable in practice (content was just composed by
        # _compose_memo, which always emits parseable frontmatter) — but
        # never corrupt a memo silently: return it as composed rather than
        # raise past the caller's own validation step.
        return content
    fm_text = split.fm_text
    if distill_fate is not None:
        fm_text = insert_fm_field(fm_text, "distill_fate", distill_fate, after_key="kind")
    if in_repo_capture is not None:
        after = "distill_fate" if distill_fate is not None else "kind"
        fm_text = insert_fm_field(fm_text, "in_repo_capture", in_repo_capture, after_key=after)
    return _rebuild_frontmatter(split, fm_text)


def _stamp_passthrough_fields(content: str, fm: dict) -> str:
    """Carry every draft frontmatter key `_compose_memo` did not emit
    (`discharges:` first among them) into the composed content, rendered with
    the composer's own `_render_extra_field`. Keys the composer already wrote
    keep the composed value."""
    split = split_frontmatter(content)
    if split is None:
        return content
    composed = parse_frontmatter(content).get("frontmatter") or {}
    blocks = [
        _render_extra_field(key, value)
        for key, value in fm.items()
        if key not in composed and value is not None
    ]
    if not blocks:
        return content
    return _rebuild_frontmatter(split, split.fm_text.rstrip("\n") + "\n" + "\n".join(blocks))


def _compose_delivered_content(
    *, fm: dict, body: str, today: str, sent_by: str,
) -> tuple[Optional[str], Optional[str]]:
    """Compose the delivered (status: open) memo content from a draft's
    parsed frontmatter + body. Returns (content, error) — exactly one non-None.

    Required fields on the draft: title, from, to, kind, a body with prose in
    it, and summary (or a derivable prose body when summary is the memo.draft
    placeholder ruler).

    `cc:` (klabauter#46, optional): validated via `_validate_cc` and, when
    present and valid, stamped onto the composed content via `_stamp_cc` —
    it survives delivery instead of being silently dropped. A malformed
    `cc:` refuses loud (see `_validate_cc`) rather than composing without it.
    """
    title = fm.get("title")
    from_id = fm.get("from")
    to = fm.get("to")
    kind = fm.get("kind")
    summary = fm.get("summary")
    if is_placeholder_summary(summary):
        summary = None  # let _compose_memo derive from body

    if not has_prose_body(body):
        # A scaffold composed and never written back. On 2026-08-19 one
        # reached coordinator-content-repo as frontmatter plus four empty comment blocks,
        # `summary:` holding a fragment of the draft warning itself; the four
        # items its title advertised existed nowhere and had to be re-sent.
        # Every OTHER required-field check here is a shape check the sender
        # cannot have meant to fail — this one is the memo's entire content,
        # and it was the only one not being made. Refuse at the last step
        # before an O_EXCL write into someone else's repo, which is the last
        # point at which refusing is still cheap: past it the receiver holds
        # a memo whose own title promises content it does not carry.
        return None, (
            "memo.send: draft body carries no prose — it is empty, or still "
            "memo.draft's placeholder comments. Write the body via "
            "memo.compose, then send."
        )

    for field_name, value in (("title", title), ("from", from_id), ("to", to), ("kind", kind)):
        if not value or not isinstance(value, str):
            return None, (
                f"memo.send: draft is missing required field {field_name!r} — "
                f"compose it via memo.compose before sending."
            )

    cc, cc_error = _validate_cc(fm.get("cc"))
    if cc_error is not None:
        return None, cc_error

    try:
        content = _compose_memo(
            from_id=from_id,
            to=to,
            topic="",  # topic lives in the filename, not frontmatter — unused here
            title=title,
            body=body,
            kind=kind,
            summary=summary,
            supersedes=fm.get("supersedes"),
            today=today,
            scoped_to=fold_flat_scoped_to(fm).get("scoped_to"),
            in_reply_to=fm.get("in_reply_to"),
            space=fm.get("space"),
            sent_by=sent_by,
        )
    except ValueError as exc:
        return None, f"memo.send: {exc}"

    if cc is not None:
        content = _stamp_cc(content, cc)

    content = _stamp_distill_fate(content, fm)
    content = _stamp_passthrough_fields(content, fm)

    return content, None


# ---------------------------------------------------------------------------
# Sender-side sent-copy stamp (derived from the draft's own frontmatter)
# ---------------------------------------------------------------------------

def _stamp_sent_copy(draft_text: str, *, sent_at: str, delivered_to: str) -> str:
    """Derive the `sent/<topic>.md` content from the draft's OWN frontmatter
    text — `status: draft` -> `status: sent`, plus `sent_at:`/`delivered_to:`
    inserted after `status:`. Every other field (title/to/summary/kind/
    scoped_to/...) survives byte-identical. Never re-authored (the plan's own
    words: "a programmatic derivation of it").
    """
    split = split_frontmatter(draft_text)
    if split is None:
        raise ValueError("memo.send: draft has no parseable frontmatter to stamp")
    fm = split.fm_text
    fm = replace_fm_field(fm, "status", "sent")
    fm = insert_fm_field(fm, "sent_at", sent_at, after_key="status")
    fm = insert_fm_field(fm, "delivered_to", delivered_to, after_key="sent_at")
    return _rebuild_frontmatter(split, fm)


# ---------------------------------------------------------------------------
# Sender-side sent-ledger row
# ---------------------------------------------------------------------------

def _ledger_row(
    *, topic: str, to: str, kind: str, summary: Optional[str],
    delivered_to: str, in_reply_to: Optional[str],
    delivery_commit_sha: Optional[str], sent_by: str,
    delivery_branch: Optional[str] = None,
    anchor_ref: Optional[str] = None,
) -> dict:
    """`delivery_branch` — the repo-relative ref (`git_native.GitResult.
    cas_ref_relpath`, e.g. `refs/heads/main`) the delivery commit was CAS'd
    onto in the receiver, taken verbatim from the commit call's own result —
    never re-resolved, since a later read of the receiver's HEAD would name
    wherever the receiver has since moved, not where this delivery landed.
    `None` on rows written before this field existed (and on any row whose
    commit result did not carry one) — C5 must read that as UNKNOWN, never
    as a mismatch against a receiver's current ref.

    `anchor_ref` — the `refs/coordinator/inbox/*` ref name (C4) the delivery
    was anchored under, or `None` when the anchor write lost its CAS race
    (`write_anchor` returned `None`) and on every row written before this
    field existed. `None` here is UNKNOWN/absent, never a mismatch against
    a live anchor — the same discipline `delivery_branch` already carries.
    """
    return {
        "sent_at": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "to": to,
        "topic": topic,
        "kind": kind,
        "summary": summary,
        "delivered_to": delivered_to,
        "in_reply_to": in_reply_to,
        "delivery_commit_sha": delivery_commit_sha,
        "delivery_branch": delivery_branch,
        "sent_by": sent_by,
        "anchor_ref": anchor_ref,
    }


def _delivery_commit_is_object(receiver_repo_path: Path, sha: str) -> bool:
    """True iff `sha` names a commit object readable in the receiver's own
    object store — object EXISTENCE, not reachability from a ref.

    Ships the cheaper of the two checks the plan named (design rationale:
    docs/plans/2026-09-11-memo-send-returns-ok-with-a-commit-sha-t.md).

    Uses the in-process pack/loose object reader (`git_objects._read_object`)
    against the receiver's git COMMON dir — zero process spawns, and no hook
    runs in the receiver's tree (`commit_authored_new_file`'s own no-hooks
    contract, which this read never touches).
    """
    common_dir = resolve_git_common_dir(receiver_repo_path)
    found = _read_object(common_dir, sha)
    return found is not None and found[0] == "commit"


def _landing_tree_has(
    receiver_repo_path: Path, landing: _receiver_landing.ReceiverLanding, rels: list,
) -> bool:
    """True iff any of `rels` is already in the tree of the commit the delivery
    would parent onto: the landing ref's tip, or its `mint_sha` while the ref is
    still unminted. An unreadable tree reads False; the commit then declines loud."""
    common_dir = resolve_git_common_dir(receiver_repo_path)
    tip = read_ref_loose_or_packed(common_dir, landing.ref_relpath) or landing.mint_sha
    info = git_state.read_commit(receiver_repo_path, tip) if tip else None
    spine = (
        git_state.read_tree_spine(receiver_repo_path, rels, root_tree_sha=info.tree)
        if info is not None else None
    )
    if spine is None:
        return False
    for rel in rels:
        parent_dir, _, leaf = rel.rpartition("/")
        if leaf in spine.get(parent_dir, {}):
            return True
    return False


def _rollback_unwritten_file(target_file: Path) -> str:
    """Undo this call's own O_EXCL write when the commit that was supposed
    to follow it did not durably land — shared by both refusal arms in
    `_memo_send` (the declined-commit C1 arm and the unverified-delivery C2
    arm): same unlink, same rollback-detail suffix text, different reason
    prefix per arm (kept distinct so a reader can still tell which fired).

    Unlinking is safe precisely here and nowhere else: the O_EXCL open that
    created `target_file` proves it did not exist before this call, and
    either failure mode proves nothing references it since. Returns the
    trailing clause each arm appends to its own `reason` text.
    """
    try:
        target_file.unlink()
    except OSError as unlink_exc:
        return (
            f" WARNING: the file this call wrote could NOT be removed"
            f" ({unlink_exc}); it is left uncommitted in the receiver's"
            f" tree and a retry will refuse on the no-clobber guard until"
            f" it is cleared."
        )
    return " the file this call wrote was removed (tree restored)."


#: Bounded wait for a peer's `.git/index.lock` in the receiver -- wall clock
#: spent sleeping, not process time; past it the delivery reports a stale
#: index rather than stalling the send.
_RECEIVER_INDEX_LOCK_ATTEMPTS = 5
_RECEIVER_INDEX_LOCK_SLEEP_SECS = 0.02

#: Inherited variables that point git at a repository, index or object store
#: other than the one `cwd` names -- set inside any git hook. The fallback
#: spawn below strips them so it writes the RECEIVER's own index.
_REPO_LOCATING_GIT_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX",
)


def _record_delivery_in_receiver_index(
    receiver_repo_path: Path, rel_path: str, content: str,
) -> Optional[str]:
    """Add the just-committed delivery to the receiver's own `.git/index`, so
    `git status` there is clean. Returns `None` on success, else a one-line
    warning for the envelope.

    Without this entry the path is in HEAD and the worktree but not the index
    -- `D  path` plus `?? path` -- and the receiver's next `git add -A` or
    `commit -a` deletes the delivered memo in its own history.

    In-process `splice_index`: zero spawns, and the index is located from
    `receiver_repo_path` itself, never from an inherited `GIT_INDEX_FILE` /
    `GIT_DIR` (set under any git hook, naming the CALLER's index). A peer's `index.lock` is
    waited on briefly, never stolen. An index `splice_index` will not write
    (v3/v4, unmerged) falls back to one hookless `update-index` spawn with
    the repo-locating variables stripped. Never raises: the commit already
    landed, and a failed index write must not read as a failed send."""
    remedy = f"`git -C {receiver_repo_path} reset -q -- {rel_path}` restores it"
    try:
        blob_sha = write_object(
            resolve_git_common_dir(receiver_repo_path), b"blob", content.encode("utf-8")
        )
    except OSError as exc:
        return f"memo.send: receiver index not updated for {rel_path} ({exc}); {remedy}."
    updates = {rel_path: (0o100644, blob_sha)}
    for attempt in range(_RECEIVER_INDEX_LOCK_ATTEMPTS):
        try:
            splice_index(receiver_repo_path, updates)
            return None
        except IndexWriteLockBusy as exc:
            detail = str(exc)
            if attempt + 1 < _RECEIVER_INDEX_LOCK_ATTEMPTS:
                time.sleep(_RECEIVER_INDEX_LOCK_SLEEP_SECS)
        except IndexStaleAfterCommit as exc:
            detail = str(exc)
            break
        except IndexWriteError:
            env = {k: v for k, v in os.environ.items() if k not in _REPO_LOCATING_GIT_ENV}
            refreshed = git_native._git(
                ["update-index", "--add", "--cacheinfo", f"100644,{blob_sha},{rel_path}"],
                cwd=receiver_repo_path, env=env,
            )
            if refreshed.ok:
                return None
            detail = (refreshed.stderr or "").strip()
            break
    return f"memo.send: receiver index not updated for {rel_path} ({detail}); {remedy}."


def _resolve_cc_targets(cc_list: list) -> tuple[Optional[list], Optional[str]]:
    """Resolve every `cc:` name to (name, inbox_dir, receiver_repo_path) the
    same way `to:` is resolved. Returns (targets, error) — exactly one
    non-None. `targets` is `[]` (never `None`) when `cc_list` is empty/None.

    Runs BEFORE any write (klabauter#46, second half): an unresolvable cc
    name refuses the WHOLE send loud here, mirroring `to:`'s own UNKNOWN
    RECEIVER refusal — never a partial send that silently drops one cc leg
    because it happened not to resolve. A publish-mirror or redirect-alias
    name resolves to its owner's inbox here exactly as it does for `to:`.
    """
    if not cc_list:
        return [], None
    targets = []
    for name in cc_list:
        receiver = resolve_receiver(name)
        if isinstance(receiver, WireRefusal):
            return None, _cc_refusal_text(name, receiver.message)
        targets.append((name, receiver.inbox_dir, receiver.receiver_repo_path))
    return targets, None


def _cc_refusal_text(name: str, message: str) -> str:
    """Re-voice a `memo_wire` receiver refusal for a `cc:` name."""
    prefix = "memo.send: "
    rest = message[len(prefix):] if message.startswith(prefix) else message
    if rest.startswith("machine-local registry could not be read"):
        return f"{prefix}cc target {name!r} could not be resolved — {rest}"
    if rest.startswith("UNKNOWN RECEIVER"):
        return (
            prefix
            + rest.replace("UNKNOWN RECEIVER", "UNKNOWN CC RECEIVER", 1).replace(
                "or check for a typo in the draft's `to:`.",
                "fix a typo in the draft's `cc:`, or remove that name from "
                "`cc:` before sending.",
            )
        )
    return f"{prefix}cc target {name!r}: {rest}"


def _deliver_cc_copy(
    *, name: str, inbox_dir: Path, receiver_repo_path: Path, filename: str,
    content: str, topic: str, from_id: str, sent_by: str,
) -> dict:
    """Write+commit+anchor one `cc:` receiver's OWN copy of the already-
    composed (cc-stamped) delivered content — the second half of
    klabauter#46: `to:` and every `cc:` name each get a real receiver-side
    write, not just a `cc:` line inside the one file `to:` receives.

    Same write discipline `_memo_send` uses for `to:` (O_EXCL create, zero-
    spawn `commit_authored_new_file`, verify the commit object, best-effort
    anchor) — duplicated rather than shared with the `to:` arm because the
    `to:` arm is entangled with the dry-run preview / no-clobber / warn-once
    gates that only ever apply to `to:`; a cc leg is resolved-and-committed
    only, never previewed or gated on reader-liveness of its own.

    Returns one dict, always carrying `ok: bool`. On failure the O_EXCL
    write (if it happened) is rolled back via `_rollback_unwritten_file`,
    same as `to:`'s own AC4/verify-failure arms — a failed cc leg never
    leaves an uncommitted orphan file behind in that receiver's tree either.
    """
    target_file = inbox_dir / filename
    rel_path = os.path.relpath(target_file, receiver_repo_path).replace(os.sep, "/")
    landing = _receiver_landing.resolve_receiver_landing(receiver_repo_path)
    ref_direct = landing.mode == _receiver_landing.MODE_REF_DIRECT
    collides = (
        _landing_tree_has(receiver_repo_path, landing, [rel_path])
        if ref_direct else target_file.exists()
    )
    if collides:
        return {
            "ok": False, "id": str(target_file), "to": name,
            "reason": (
                f"collision: {target_file} already exists in cc receiver "
                f"{name!r}'s inbox — refuse (no clobber)."
            ),
        }

    landing = _receiver_landing.apply_receiver_landing(landing)
    ref_direct = landing.mode == _receiver_landing.MODE_REF_DIRECT
    if not ref_direct:
        inbox_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(target_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
        except FileExistsError:
            return {
                "ok": False, "id": str(target_file), "to": name,
                "reason": (
                    f"collision (race): {target_file} appeared between the "
                    f"collision-check and the O_EXCL write in cc receiver "
                    f"{name!r} — refuse (no clobber)."
                ),
            }
        except OSError as exc:
            return {
                "ok": False, "id": str(target_file), "to": name,
                "reason": f"write-failed for cc receiver {name!r}: {exc}",
            }

    msg_file = _write_msg_file(
        _delivery_commit_message(topic, from_id, sent_by)
    )
    try:
        commit_result = git_native.commit_authored_new_file(
            rel_path, content, msg_file, receiver_repo_path,
            refresh_shared_index=False,
            onto_ref=landing.ref_relpath if ref_direct else None,
        )
    finally:
        try:
            msg_file.unlink()
        except OSError:
            pass

    if not commit_result.ok:
        rollback_detail = "" if ref_direct else _rollback_unwritten_file(target_file)
        return {
            "ok": False, "id": str(target_file), "to": name,
            "reason": (
                f"cc receiver {name!r} commit declined: "
                f"{commit_result.stderr} — not retried, per AC4;"
                f"{rollback_detail}"
            ),
        }

    delivery_commit_sha = commit_result.stdout.strip() or None
    if delivery_commit_sha is None or not _delivery_commit_is_object(
        receiver_repo_path, delivery_commit_sha
    ):
        rollback_detail = "" if ref_direct else _rollback_unwritten_file(target_file)
        return {
            "ok": False, "id": str(target_file), "to": name,
            "reason": (
                f"cc receiver {name!r} commit could not be verified: "
                f"{delivery_commit_sha or ''!r} is not a commit object in "
                f"the receiver's repository — not sent." + rollback_detail
            ),
        }

    index_warning = (
        None if ref_direct
        else _record_delivery_in_receiver_index(receiver_repo_path, rel_path, content)
    )
    common_dir = resolve_git_common_dir(receiver_repo_path)
    anchored_bytes = (
        content.encode("utf-8") if ref_direct else target_file.read_bytes()
    )
    anchor_blob_sha = write_anchor(
        common_dir, filename, delivery_commit_sha, anchored_bytes
    )
    push_cadence.note_foreign_delivery(
        receiver_repo_path, landing.ref_relpath[len(_HEADS_PREFIX):]
    )
    delivered = {
        "ok": True, "id": str(target_file), "to": name,
        "written": True, "committed": True,
        "delivery_commit_sha": delivery_commit_sha,
        "anchored": anchor_blob_sha is not None,
    }
    if index_warning is not None:
        _LOG.warning("%s", index_warning)
        delivered["receiver_index_warning"] = index_warning
    return delivered


class _SenderCommit(NamedTuple):
    """The sender-side receipt commit's outcome, in the two fields the
    envelope below reads. `commit_paths` signals failure by raising and
    success by returning a `CommitOutcome`, so the two arms are normalised
    here rather than at each of the four read sites."""

    ok: bool
    sha: Optional[str]
    stderr: str


def _row_is_older_than_cutoff_dict(row: dict, cutoff: datetime.datetime) -> bool:
    """True iff this ALREADY-PARSED ledger row carries a `sent_at` older than
    `cutoff`. Shared by both this module's own line-based wrapper below (the
    row cap's read-modify-write only has raw lines in hand) and
    `workday_start_cross_repo_memo_outbox_surface._gone_delivery_lines` (which
    already holds parsed dicts via `_iter_ledger_rows`, so parsing here
    again would be pure waste) — one comparator, not two (Review:
    overengineering-reviewer).

    UNDATABLE ROWS ARE NEVER EVICTED BY AGE -- a row with no `sent_at`, or
    one this cannot parse, returns False and lives until the row cap
    reaches it (or, for the surfacer, is never surfaced on a guess).
    Dropping a row for failing to prove its own age would delete the oldest
    rows in the file (the ones predating the field) on the first append
    after this shipped, which is the opposite of what an age bound is for.

    `sent_at` is written by `_ledger_row` as `%Y-%m-%dT%H:%M:%SZ`. Parsed
    with `fromisoformat` after swapping the `Z`, which Python's parser did
    not accept before 3.11 and this repo's floor is 3.11; a value in any
    other shape is undatable by the rule above, not an error.
    """
    sent_at = row.get("sent_at")
    if not isinstance(sent_at, str) or not sent_at:
        return False
    try:
        stamp = datetime.datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp < cutoff


def _row_is_older_than_cutoff(line: str, cutoff: datetime.datetime) -> bool:
    """True iff this RAW ledger line carries a `sent_at` older than
    `cutoff` -- parses, then delegates to `_row_is_older_than_cutoff_dict`.
    See that function for the undatable-rows discipline this shares."""
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        return False
    if not isinstance(row, dict):
        return False
    return _row_is_older_than_cutoff_dict(row, cutoff)


def _write_msg_file(text: str) -> Path:
    fd, name = tempfile.mkstemp(prefix="memo-send-msg-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    except BaseException:
        os.unlink(name)
        raise
    return Path(name)


# ---------------------------------------------------------------------------
# C3 — citation lint: warn once where a body cites a docs/state/coordinator/
# archive/cross-repo path with no repo qualifier — the receiver would
# resolve it against its own tree, not this sender's.
# ---------------------------------------------------------------------------

#: A candidate path root, not preceded by a path or URL character — so a
#: mid-path segment (`blob/main/docs/x.md`) or a URL segment
#: (`github.com/o/r/docs/x.md`) is never itself a candidate; only a path
#: that starts a token is. Matches forward-slash paths only (D4: memo bodies
#: cite paths in that form on every host).
_CITATION_ROOT_RE = re.compile(
    r"(?<![\w./:-])(?:docs|state|coordinator|archive|cross-repo)/[\w\-./]*[\w\-]"
)

#: The qualifier immediately before a candidate — a repo name, allowing one
#: backtick and whitespace, followed by `:` or a space (D4's `<repo>:<path>`
#: and `<repo> <path>` forms).
_CITATION_QUALIFIER_PRE_RE = re.compile(r"([A-Za-z][A-Za-z0-9_-]*)[:\s]\s*`?\Z")


_FENCE_LINE_RE = re.compile(r"[ \t]*(`{3,}|~{3,})")


def _fenced_spans(body: str) -> list:
    """`(start, end)` offsets of each fenced code block (``` or ~~~, any
    indentation), fence lines included. An unclosed fence runs to the end of
    `body`. Contract: text inside a span is verbatim and never rewritten.
    """
    spans: list = []
    open_start = None
    marker = ""
    pos = 0
    for line in body.splitlines(keepends=True):
        end = pos + len(line)
        m = _FENCE_LINE_RE.match(line)
        if open_start is None:
            if m:
                open_start, marker = pos, m.group(1)
        elif m and m.group(1)[0] == marker[0] and len(m.group(1)) >= len(marker) \
                and not line[m.end():].strip():
            spans.append((open_start, end))
            open_start = None
        pos = end
    if open_start is not None:
        spans.append((open_start, len(body)))
    return spans


def _citation_matches(body: str):
    """`_CITATION_ROOT_RE` matches outside fenced code blocks."""
    spans = _fenced_spans(body)
    for match in _CITATION_ROOT_RE.finditer(body):
        if any(s <= match.start() < e for s, e in spans):
            continue
        yield match


def _repo_qualifier_names(all_repos: dict) -> frozenset:
    """The set of names that qualify a body path citation as repo-scoped:
    each registry key (lowercased, `_` -> `-`) plus the lowercased basename
    of each registry path — no new registry read, `all_repos` is the same
    dict `resolve_receiver_inbox` already returned to the caller.
    """
    names = set()
    for key, path in (all_repos or {}).items():
        if isinstance(key, str) and key:
            names.add(key.lower().replace("_", "-"))
        if isinstance(path, str) and path:
            names.add(Path(path).name.lower())
    return frozenset(names)


def _unqualified_path_citations(body: str, qualifiers: frozenset) -> list:
    """The distinct `docs/`, `state/`, `coordinator/`, `archive/` or
    `cross-repo/`-rooted body paths not immediately preceded (allowing one
    backtick and whitespace) by a name in `qualifiers` — in first-seen
    order.

    Negative-spec: never refuses, never reads a file off disk, and never
    treats a URL segment or a mid-path segment as a candidate (see
    `_CITATION_ROOT_RE`'s lookbehind).
    """
    seen: list = []
    seen_set: set = set()
    for match in _citation_matches(body):
        candidate = match.group(0)
        prefix = body[: match.start()]
        qualifier_match = _CITATION_QUALIFIER_PRE_RE.search(prefix)
        if qualifier_match and qualifier_match.group(1).lower() in qualifiers:
            continue
        if candidate not in seen_set:
            seen_set.add(candidate)
            seen.append(candidate)
    return seen


def _citation_owners(
    paths: list, sender_root: Path, receiver_root: Path,
    sender_name: str,
) -> tuple:
    """Classify each bare path: `({path: qualifier}, [unresolved])`. A path
    absent from the sender's tree still belongs to the sender when a
    non-top-level ancestor directory exists there, or when the receiver
    holds it (an absence report is the common cause); the receiver is never
    credited by existence alone. Held by both trees, or by neither with no
    sender ancestor -> unresolved (left bare). In-process stats only.
    """
    owners: dict = {}
    unresolved: list = []
    for p in paths:
        in_sender = (sender_root / p).exists()
        in_receiver = (receiver_root / p).exists()
        if in_sender and in_receiver:
            unresolved.append(p)
        elif in_sender or in_receiver or _sender_has_ancestor(sender_root, p):
            owners[p] = sender_name
        else:
            unresolved.append(p)
    return owners, unresolved


def _sender_has_ancestor(sender_root: Path, rel: str) -> bool:
    """True when a directory strictly between the repo root's top-level
    segment (`state/`, `docs/` exist in every fleet repo) and `rel` exists."""
    parts = rel.split("/")
    return any(
        (sender_root.joinpath(*parts[:i])).is_dir() for i in range(len(parts) - 1, 1, -1)
    )


def _citation_lint_notice(paths: list, sender_name: str, owners: Optional[dict] = None) -> str:
    """The informational (never held/refused) citation-qualify notice: lists
    each bare path with the repo it was qualified as (`owners`, defaulting to
    `sender_name`). Lists at most five paths, because the author needs
    examples, not an inventory."""
    owners = owners or {}
    shown = paths[:5]
    remainder = len(paths) - len(shown)
    listed = "\n".join(f"    {p} -> {owners.get(p, sender_name)}:{p}" for p in shown)
    more_clause = f"\n    ...and {remainder} more" if remainder > 0 else ""
    return (
        "memo.send: %d body path(s) were not repo-qualified; qualified as "
        "the sender's. Write `repo:path` to cite a peer.\n"
        "%s%s"
        % (len(paths), listed, more_clause)
    )


def _qualify_unqualified_citations(
    body: str, qualifiers: frozenset, sender_name: str,
    owners: Optional[dict] = None,
) -> str:
    """Rewrite each unqualified `docs/`/`state/`/`coordinator/`/`archive/`/
    `cross-repo/`-rooted citation in `body` to `<repo>:<path>`. With `owners`
    (path -> qualifier), a path absent from it is left bare; without it every
    path is qualified as `sender_name`. Leaves already-qualified citations
    untouched. Operates left-to-right over one pass so an inserted qualifier
    is never itself re-matched.
    """
    out: list = []
    cursor = 0
    for match in _citation_matches(body):
        candidate = match.group(0)
        prefix = body[cursor: match.start()]
        qualifier_match = _CITATION_QUALIFIER_PRE_RE.search(body[: match.start()])
        already_qualified = bool(
            qualifier_match and qualifier_match.group(1).lower() in qualifiers
        )
        out.append(prefix)
        if already_qualified:
            out.append(candidate)
        elif owners is None:
            out.append(f"{sender_name}:{candidate}")
        elif candidate in owners:
            out.append(f"{owners[candidate]}:{candidate}")
        else:
            out.append(candidate)
        cursor = match.end()
    out.append(body[cursor:])
    return "".join(out)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Warn-once acknowledgement bookkeeping, shared by every one-shot gate below.
# ---------------------------------------------------------------------------

#: Where the once-per-topic acknowledgement lands. Under the sender's own
#: `.coordinator-local/` (already the outbox's home and already untracked),
#: never in a receiver's tree and never in git history — this is session-local
#: bookkeeping about a warning, not a durable fleet artifact.
_SEND_ACK_RELDIR = (".coordinator-local", "memo-send-ack")


def _send_ack_path(sender_worktree: Path, topic: str) -> Path:
    """One marker per topic, keyed by hash rather than transliteration.

    The first version filtered non-alphanumerics to `-`, which collapsed `a/b`,
    `a-b` and `a b` onto one marker — so one topic could consume another's
    single warning — and kept `.`, so a topic of `.` or `..` addressed a
    directory. A digest has neither problem and needs no length cap.
    """
    import hashlib

    digest = hashlib.sha256(topic.encode("utf-8")).hexdigest()[:32]
    return sender_worktree.joinpath(*_SEND_ACK_RELDIR) / digest


def _warn_once(sender_worktree: Path, ack_key: str, warning: str) -> Optional[str]:
    """The one shared ack protocol every one-shot `memo.send` warning uses.

    `None` when `ack_key` was already warned about, or when the marker
    cannot be recorded; otherwise writes the marker and returns `warning`
    verbatim. Callers take no `dry_run` argument here by design — a
    preview writes nothing, so every caller skips this entirely under
    `dry_run` rather than pass it through.

    Negative-spec: decides nothing about WHETHER to warn (that judgment is
    entirely the caller's, made before this is called), and never batches
    or coalesces multiple warnings — one call is one key.

    Fails OPEN: an `OSError` writing the marker returns `None` (send)
    rather than stranding the memo on an unwritable ack directory — the
    retry can't be promised to behave differently, so refusing the first
    attempt would be permanent.
    """
    ack = _send_ack_path(sender_worktree, ack_key)
    try:
        if ack.is_file():
            return None
        ack.parent.mkdir(parents=True, exist_ok=True)
        ack.write_text("warned\n", encoding="utf-8", newline="\n")
    except OSError:
        return None
    return warning


def _held_once_refusal(warnings: list) -> str:
    """Every warn-once gate that fired on this attempt, as ONE refusal.

    The gates are evaluated together and each records its marker in the
    same pass, so "the next attempt sends" is true when it is said. Gated
    one at a time, a memo tripping two gates was refused twice, each
    refusal promising the retry would send.
    """
    noun = "This warning fires" if len(warnings) == 1 else "These warnings fire"
    return (
        "\n\n".join(warnings)
        + "\n  Nothing was written. %s once per topic; the next attempt sends."
        % noun
    )


# ---------------------------------------------------------------------------
# C5/C7 — sender-side sweep: is a ledgered delivery present, anchored, or
# gone in the receiver? Read-only, both repos; C7 widens the verdict from
# object-existence alone to tree-presence-or-anchor, reusing
# `_delivery_commit_is_object` only as the pre-anchor fallback. See module
# docstring's C5/C7 spec backlinks.
# ---------------------------------------------------------------------------

_CHECK_DELIVERIES_KNOWN_PARAM_KEYS = frozenset({"dry_run"})


def _validate_check_deliveries_params(params: dict):
    """Validate memo.check_deliveries params; return dry_run or a setup-error dict.

    `dry_run` must be `True` — this op has no act mode, it only ever reads.
    """
    dry_run = params.get("dry_run")
    if dry_run is not True:
        return build_setup_error_result(
            "check_deliveries", dry_run,
            "memo.check_deliveries: dry_run must be true — this op has no "
            "act mode, it only reads the sent-ledger and each receiver's "
            "own object store.",
        )
    unknown_keys = set(params.keys()) - _CHECK_DELIVERIES_KNOWN_PARAM_KEYS
    if unknown_keys:
        return build_setup_error_result(
            "check_deliveries", dry_run,
            f"memo.check_deliveries: unrecognized param(s) {sorted(unknown_keys)} "
            f"— known params: {sorted(_CHECK_DELIVERIES_KNOWN_PARAM_KEYS)}.",
        )
    return dry_run


def _iter_ledger_rows(ledger_path: Path):
    """Yield each parseable dict row from the sent-ledger JSONL, in file order.

    A line that is not JSON, or not a JSON object, is skipped rather than
    raising — same discipline as `_row_is_older_than_cutoff`: a malformed or
    hand-edited line must not sink the whole sweep.
    """
    try:
        text = ledger_path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            yield row


def _prior_reply_from_another_session(
    ledger_path: Path, in_reply_to: str, sent_by: str,
) -> Optional[dict]:
    """The newest sent-ledger row that already answered `in_reply_to` from
    a DIFFERENT session than `sent_by` — evidence this send would be a
    second reply to a memo this repo already answered once.

    Walks `_iter_ledger_rows` (outbox drafts and `sent/` copies are never
    read — the ledger is the one place a completed send is durably
    recorded across sessions, and the only one this reads). A row counts
    as a match when its own `_normalize_in_reply_to` equals this send's,
    and as a DIFFERENT session whenever either `sent_by` is missing or the
    `_SENT_BY_UNRESOLVED` sentinel — an unresolved sender cannot be
    assumed to BE this session, so it must never read as a same-session
    repeat.

    Negative-spec: never raises. A `ledger_path` that cannot be read
    yields no rows via `_iter_ledger_rows`, so this returns `None`.
    """
    target = _normalize_in_reply_to(in_reply_to)
    newest = None
    for row in _iter_ledger_rows(ledger_path):
        row_in_reply_to = row.get("in_reply_to")
        if not isinstance(row_in_reply_to, str) or not row_in_reply_to:
            continue
        if _normalize_in_reply_to(row_in_reply_to) != target:
            continue
        row_sent_by = row.get("sent_by")
        same_session = (
            isinstance(row_sent_by, str)
            and row_sent_by
            and row_sent_by != _SENT_BY_UNRESOLVED
            and sent_by != _SENT_BY_UNRESOLVED
            and row_sent_by == sent_by
        )
        if same_session:
            continue
        newest = row
    return newest


def _duplicate_reply_warning(prior: dict) -> str:
    """The one-shot duplicate-reply warning — register, not an essay: one
    fact (another session of this repo already answered this memo), the
    check the EM applies, and the way through."""
    return (
        "memo.send: another session of this repo already answered that "
        "memo — topic %r, sent to %r at %s.\n"
        "  Read that reply before sending a second one."
        % (prior.get("topic"), prior.get("to"), prior.get("sent_at"))
    )


#: Sweep verdicts — four outcomes, never three. A receiver absent from this
#: machine is UNCHECKABLE, not GONE: it says nothing about whether the
#: delivery arrived (plan's "The delivery-durability decision", C5).
#: `restorable` is its own verdict (Review: eng-director F8), not folded
#: into `verified` — a receiver that never runs a local workday-start
#: (e.g. an all-cloud posture) must not read as verified while its memo
#: sits unread in anchor form only; it reads as restorable until the
#: receiver's own heal adopts it back into its tree.
_VERDICT_VERIFIED = "verified"
_VERDICT_RESTORABLE = "restorable"
_VERDICT_GONE = "gone"
_VERDICT_NOT_CHECKABLE = "not_checkable"

#: Delivered means present, at any depth, under either corpus root's
#: `inbox/` or `archive/` in the receiver's OWN working tree -- "delivered
#: means present or anchored, not the object exists" (C7). The traversal
#: is shared with `memo_heal._present_map` via
#: `_memo_anchor.present_filenames` (Review: overengineering-reviewer F1:
#: this was the OTHER join key of the feature, alongside the anchor-ref
#: shape `_memo_anchor.py` already protects); only the reported TEXT
#: differs between memo_send (sender-side, reads a registry-resolved
#: PEER's tree) and memo_heal (receiver-side, acts on `repo_root` itself).


def _delivery_present_in_tree(receiver_repo_path: Path, filename: str) -> bool:
    """True iff `filename` exists, at any depth, under either corpus root's
    `inbox/` or `archive/` in the receiver's OWN working tree -- the tree is
    what a reader of that repo actually sees; a branch delete/reset/
    recreate changes what this returns, which is exactly why the anchor
    check below exists for when it says no."""
    if not filename:
        return False
    return filename in present_filenames(receiver_repo_path)


def _anchor_is_live(receiver_repo_path: Path, filename: str, sha: str) -> bool:
    """True iff `refs/coordinator/inbox/<filename>/<sha>` currently resolves
    to a blob in the receiver's own object store — i.e. the anchor `memo.
    send` wrote for this exact delivery has not been retired by the
    receiver's own `memo.heal_inbox`. Zero spawns."""
    if not filename:
        return False
    common_dir = resolve_git_common_dir(receiver_repo_path)
    return any(
        fname == filename and csha == sha
        for fname, csha, _bsha in anchor_names(common_dir)
    )


def _check_ledger_row(row: dict) -> dict:
    """Classify one sent-ledger row: verified / restorable / gone /
    not_checkable — "delivered" means present in the receiver's own tree OR
    anchored there, never merely "the delivery commit object still exists"
    (C7; that narrower rule is now only the pre-anchor fallback below).

    Order of the checks, each answering a strictly narrower question than
    the last:
      1. Is the delivered filename present (inbox or archive, either
         corpus root, any depth) in the receiver's OWN working tree?
         -> verified.
      2. Absent from the tree — is this exact (filename, delivery commit)
         still anchored under `refs/coordinator/inbox/`? -> restorable
         (the receiver's next workday-start restores it from the anchor).
      3. Absent from the tree, anchor also gone — did the row EVER record
         an `anchor_ref`? A missing anchor on a row that HAD one means the
         receiver's own heal retired it deliberately (heal only retires
         after observing an archive or removal) -> verified, with a note
         distinguishing "disposed of by the receiver" from "lost".
      4. No `anchor_ref` on the row at all (pre-A2, or the anchor write
         itself lost its CAS race at send time) — fall back to the
         pre-C7 rule this replaces: is `delivery_commit_sha` still a
         commit object in the receiver's store? -> verified if so, else
         gone.

    Never writes, never re-sends.
    """
    topic = row.get("topic")
    to = row.get("to")
    sha = row.get("delivery_commit_sha")
    branch = row.get("delivery_branch")  # absent -> unknown, never a mismatch
    delivered_to = row.get("delivered_to")
    anchor_ref_on_row = row.get("anchor_ref")
    candidate = {
        "id": f"{to}:{topic}:{sha}",
        "topic": topic,
        "to": to,
        "delivery_commit_sha": sha,
        "delivery_branch": branch if isinstance(branch, str) else None,
    }

    if not sha or not isinstance(sha, str):
        candidate["status"] = _VERDICT_NOT_CHECKABLE
        candidate["note"] = "row carries no delivery_commit_sha — nothing to verify"
        return candidate

    if not to or not isinstance(to, str):
        candidate["status"] = _VERDICT_NOT_CHECKABLE
        candidate["note"] = "row carries no 'to' — cannot resolve a receiver to check"
        return candidate

    receiver = resolve_receiver(to)
    if isinstance(receiver, WireRefusal):
        candidate["status"] = _VERDICT_NOT_CHECKABLE
        candidate["note"] = (
            f"receiver {to!r} could not be resolved: {receiver.message} — a "
            f"peer not checked out here says nothing about delivery"
        )
        return candidate
    receiver_repo_path = receiver.receiver_repo_path

    filename = os.path.basename(delivered_to) if isinstance(delivered_to, str) else None

    if _delivery_present_in_tree(receiver_repo_path, filename):
        candidate["status"] = _VERDICT_VERIFIED
        candidate["note"] = None
        return candidate

    if _anchor_is_live(receiver_repo_path, filename, sha):
        candidate["status"] = _VERDICT_RESTORABLE
        candidate["note"] = (
            f"{filename!r} is absent from the receiver's tree but anchored "
            f"under refs/coordinator/inbox/ — anchored; the receiver's next "
            f"workday-start restores it"
        )
        return candidate

    if isinstance(anchor_ref_on_row, str) and anchor_ref_on_row:
        candidate["status"] = _VERDICT_VERIFIED
        candidate["note"] = (
            f"{filename!r} is absent and its anchor is gone — the "
            f"receiver's own heal only retires an anchor after observing an "
            f"archive or deliberate removal, so this reads as disposed of "
            f"by the receiver, not lost"
        )
        return candidate

    # Rows predating anchors (no anchor_ref ever recorded — pre-A2, or the
    # anchor write itself lost its CAS race at send time): fall back to the
    # pre-C7 object-existence rule this replaces.
    if _delivery_commit_is_object(receiver_repo_path, sha):
        candidate["status"] = _VERDICT_VERIFIED
        candidate["note"] = None
    else:
        candidate["status"] = _VERDICT_GONE
        candidate["note"] = (
            f"{sha!r} is no longer a commit object in {receiver_repo_path} — "
            f"delivered, not durable; re-delivery is an operator decision "
            f"(re-run memo.send)"
        )
    return candidate


@register_op("memo.check_deliveries")
def _memo_check_deliveries(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'memo.check_deliveries' COMPUTE_ONLY op handler.

    A sender-side, read-only sweep over this repo's own sent-ledger,
    classifying each row verified / restorable / gone / not_checkable
    (`_check_ledger_row`, C7) — "delivered" means the memo is present in
    the receiver's own inbox/archive tree OR anchored under
    `refs/coordinator/inbox/`, never merely "the delivery commit object
    still exists" (that narrower rule is now only the pre-anchor fallback
    for rows predating C4/A2). Converts a silent permanent loss (the
    incident this plan closes) into a visible one, and stops reading a
    receiver that never runs a local workday-start as `verified` while its
    memo sits unread (Review: eng-director F8).

    Params:
        dry_run (bool, required): must be True — no act mode.

    repo_root: git common dir (`_OP_KEY_SCOPE = "common_dir"`) — the
    SENDER's own worktree, same derivation memo.send uses.

    Reads only: the sender's own ledger file, and the working trees +
    object stores of registered receiver repos present on this machine.
    Never writes into a receiver, never re-sends — see module docstring's
    C5 negative-spec.
    """
    validated = _validate_check_deliveries_params(params)
    if isinstance(validated, dict):
        return validated
    dry_run = validated

    if repo_root is None:
        return build_setup_error_result(
            "check_deliveries", dry_run,
            "memo.check_deliveries: no repo_root supplied — reads the "
            "CALLING repo's own sent-ledger and requires a resolved "
            "worktree (common_dir-keyed op).",
        )
    sender_worktree = main_worktree_root(Path(repo_root))
    ledger_path = _sent_ledger_path(sender_worktree)

    candidates = [_check_ledger_row(row) for row in _iter_ledger_rows(ledger_path)]
    return build_dry_run_result("check_deliveries", candidates)


_BODY_DISCHARGES_LINE_RE = re.compile(r"^\s*discharges:", re.MULTILINE)


def _body_discharges_without_block(fm: dict, body: str) -> bool:
    """True when a body line starts `discharges:` and the frontmatter has no
    `discharges` key — `gate_liveness.resolve` reads only the frontmatter."""
    return "discharges" not in fm and bool(_BODY_DISCHARGES_LINE_RE.search(body or ""))


_PLAN_TASKS_BLOCK_RE = re.compile(r"```yaml plan-tasks\n(.*?)```", re.DOTALL)


def _parked_thread_gate(
    receiver_repo_path: Path, in_reply_to: str, owners: set,
) -> Optional[tuple]:
    """`(plan filename, row id)` of a receiver plan row holding an uncleared
    `external_gate` keyed `memo-thread` on `in_reply_to` and owned by one of
    `owners` (lowercase repo names); `None` when there is none.

    Read-only and in-process. A plan whose lowercased text lacks the thread
    id is skipped before any YAML parse, so only the few matching plans pay
    for one.
    """
    import yaml

    from coordinator_core.ops.gate_liveness.resolve import _memo_thread_ids_match

    needle = Path(in_reply_to.strip()).name.lower()
    needle = needle[:-3] if needle.endswith(".md") else needle
    if not needle:
        return None
    try:
        plans = sorted((receiver_repo_path / "docs" / "plans").glob("*.md"))
    except OSError:
        return None
    for plan in plans:
        try:
            text = plan.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if needle not in text.lower():
            continue
        for block in _PLAN_TASKS_BLOCK_RE.findall(text):
            try:
                rows = yaml.safe_load(block)
            except yaml.YAMLError:
                continue
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                gates = row.get("external_gate")
                for gate in gates if isinstance(gates, list) else []:
                    if not isinstance(gate, dict) or gate.get("cleared") is True:
                        continue
                    key = gate.get("closure_key")
                    if not isinstance(key, dict) or key.get("kind") != "memo-thread":
                        continue
                    key_id, owner = key.get("id"), gate.get("owner_repo")
                    if (
                        isinstance(key_id, str) and isinstance(owner, str)
                        and _memo_thread_ids_match(key_id, in_reply_to)
                        and owner.strip().lower() in owners
                    ):
                        return plan.name, str(row.get("id"))
    return None


def _discharge_prompt(plan_name: str, row_id: str, in_reply_to: str) -> str:
    return (
        f"memo.send: this reply answers {in_reply_to!r}, which gates row "
        f"{row_id} of the receiver's plan {plan_name}, and the draft has no "
        "`discharges` block, so that gate stays awaiting-discharge. Compose "
        "the block with `gate_liveness.emit_discharge`, or resend to send as is."
    )


@register_op("memo.send")
def _memo_send(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'memo.send' MUTATING op handler.

    Delivers the caller's already-staged `state/memo-outbox/<topic>.md`
    draft into a registry-enumerated receiver's `cross-repo/inbox/`, then
    lands the sender-side receipt. See module docstring for the three-write
    shape and its ordering guarantee.

    Params:
        dry_run (bool, required): preview (true) vs. act (false).
        topic   (str, required):  the staged draft's topic slug — identifies
                                   `state/memo-outbox/<topic>.md`.

    repo_root: git common dir (`_OP_KEY_SCOPE = "common_dir"`) — the SENDER's
    own worktree is derived via `main_worktree_root(repo_root)`. The
    RECEIVER's repo is always registry-derived via `to:`, never wire-derived.
    """
    validated = _validate_send_params(params)
    if isinstance(validated, dict):
        return validated  # exit_code:1 setup-error envelope
    dry_run, topic = validated

    refusal = feature_refusal("cross_repo_memos")
    if refusal:
        return build_setup_error_result(_MODE, dry_run, refusal)

    if repo_root is None:
        return build_setup_error_result(
            _MODE, dry_run,
            # Error named the retired root only; now names the canonical root, dual-root read noted.
            "memo.send: no repo_root supplied — memo.send reads the CALLING "
            "repo's own .coordinator-local/memo-outbox/ (falling back to the "
            "retired state/memo-outbox/ for pre-relocation drafts) and requires "
            "a resolved worktree (common_dir-keyed op).",
        )
    sender_worktree = main_worktree_root(Path(repo_root))

    draft_path = _draft_path(sender_worktree, topic)
    fm, body, read_error = _read_draft(draft_path)
    if read_error is not None:
        return build_setup_error_result(_MODE, dry_run, read_error)

    to = fm.get("to")
    if not to or not isinstance(to, str):
        return build_setup_error_result(
            _MODE, dry_run, "memo.send: draft is missing required field 'to'",
        )

    # klabauter#46 (second half): every cc: name must resolve BEFORE any
    # write happens, same bar to: is already held to — see
    # `_resolve_cc_targets`.
    cc_list, cc_error = _validate_cc(fm.get("cc"))
    if cc_error is not None:
        return build_setup_error_result(_MODE, dry_run, cc_error)
    cc_targets, cc_resolve_error = _resolve_cc_targets(cc_list or [])
    if cc_resolve_error is not None:
        return build_setup_error_result(_MODE, dry_run, cc_resolve_error)

    if body_opens_frontmatter(body):
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.send: the staged body opens a second frontmatter block — "
            "re-run memo.compose with only the body text.",
        )

    if _body_discharges_without_block(fm, body):
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.send: body has a `discharges:` line but the frontmatter has "
            "no `discharges` block, so the receiver's gate stays "
            "awaiting-discharge. Compose the block with "
            "`gate_liveness.emit_discharge`.",
        )

    # Duplicate-body detector — a byte-identical body under a DIFFERENT
    # topic in the same outbox is almost always a stale duplicate draft, not
    # two intentional sends. Skipped for an empty body: the `--empty-body`
    # opt-in path can legitimately stage two deliberately body-less memos,
    # which are not a collision. Runs at the OP layer (not the CLI), so a
    # direct `coordinator-invoke memo.send` is covered too.
    normalized_body = _normalize_body(body)
    if normalized_body:
        # Scanned across BOTH outbox roots (2026-09-03 relocation) — a
        # sibling staged before the repoint is still a real duplicate.
        for sibling_dir in (outbox_dir(sender_worktree), legacy_outbox_dir(sender_worktree)):
            colliding_topic = _find_duplicate_draft_topic(
                sibling_dir, topic, normalized_body, fm.get("to"),
            )
            if colliding_topic is not None:
                return build_setup_error_result(
                    _MODE, dry_run,
                    f"memo.send: staged body is byte-identical to draft "
                    f"{colliding_topic!r} (same receiver) in {sibling_dir} — rewrite this body, "
                    f"or discard the stale draft, before sending.",
                )

    today = datetime.date.today().isoformat()
    # Resolved ONCE and threaded to all three carriers: the delivered memo's
    # frontmatter, the delivery commit's Session-Id: trailer, and the
    # sent-ledger row. Three independent calls could disagree, leaving a
    # reader who joins them with three answers to one question -- and the
    # sender_unattributed flag below has to describe the value actually
    # stamped, not a fourth resolution of it.
    sent_by = _resolve_sent_by(fm)
    content, compose_error = _compose_delivered_content(
        fm=fm, body=body, today=today, sent_by=sent_by,
    )
    if compose_error is not None:
        return build_setup_error_result(_MODE, dry_run, compose_error)

    delivered_fm = parse_frontmatter(content).get("frontmatter") or {}
    cross_field_errors = validate_memo_cross_fields(delivered_fm)
    if cross_field_errors:
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.send: composed memo failed cross-field validation: "
            + format_validation_errors(cross_field_errors),
        )

    from_id = fm.get("from")
    target = resolve_delivery_target(
        to, sender_worktree=sender_worktree, from_id=from_id, topic=topic, today=today,
    )
    if isinstance(target, WireRefusal):
        return build_setup_error_result(_MODE, dry_run, target.message)
    filename = target.filename
    inbox_dir = target.inbox_dir
    receiver_repo_path = target.receiver_repo_path
    all_repos = target.all_repos
    self_send = target.self_send

    # The warn-once gates — all evaluated on the same attempt and refused as
    # one (`_held_once_refusal`). After the UNKNOWN RECEIVER refusal (an
    # unresolvable receiver fails first and never uses up an ack) and before
    # the dry_run preview return (a preview never gates). Nothing has been
    # written yet, so a refusal still costs nothing.
    held = []

    # Duplicate reply: THIS repo already answered `in_reply_to` from a
    # DIFFERENT session.
    reply_to = fm.get("in_reply_to")
    if not dry_run and isinstance(reply_to, str) and reply_to.strip():
        prior_reply = _prior_reply_from_another_session(
            _sent_ledger_path(sender_worktree), reply_to, sent_by,
        )
        if prior_reply is not None:
            duplicate_warning = _warn_once(
                sender_worktree, f"duplicate-reply:{topic}",
                _duplicate_reply_warning(prior_reply),
            )
            if duplicate_warning is not None:
                held.append(duplicate_warning)

    # Discharge prompt: the receiver holds a parked gate keyed on the thread
    # this reply answers, and the draft carries no `discharges` block.
    if (
        not dry_run and not self_send and "discharges" not in fm
        and isinstance(reply_to, str) and reply_to.strip()
    ):
        sender_owner = str(from_id or sender_worktree.name).strip().lower()
        if sender_owner.endswith("-em"):
            sender_owner = sender_owner[: -len("-em")]
        parked = _parked_thread_gate(
            receiver_repo_path, reply_to,
            {sender_owner, sender_worktree.name.lower()},
        )
        if parked is not None:
            discharge_prompt = _warn_once(
                sender_worktree, f"discharge-prompt:{topic}:{reply_to.strip()}",
                _discharge_prompt(parked[0], parked[1], reply_to.strip()),
            )
            if discharge_prompt is not None:
                held.append(discharge_prompt)

    # Citation lint: the body cites a docs/state/coordinator/archive/
    # cross-repo path with no repo qualifier — the receiver resolves it
    # against ITS OWN tree, not this sender's. Skipped entirely when the
    # receiver resolves to the sender's own worktree (D4) — a self-send
    # never crosses a tree boundary.
    citations_unresolved: list = []
    if not dry_run and not self_send:
        sender_name = sender_worktree.name.lower()
        qualifiers = _repo_qualifier_names(all_repos) | {sender_name}
        unqualified = _unqualified_path_citations(body, qualifiers)
        if unqualified:
            # Repo-qualify at send time rather than refuse-then-let-an-
            # unchanged-retry-through (memo friction item 5): an unqualified
            # path in a memo sent from this repo means THIS repo, so fix it
            # in the already-composed `content` (the citation-lint check runs
            # after compose, once `all_repos` is available) and deliver the
            # qualified body. Never held/refused.
            sender_name = str(from_id or sender_worktree.name).strip()
            if sender_name.endswith("-em"):
                sender_name = sender_name[: -len("-em")]
            owners, citations_unresolved = _citation_owners(
                unqualified, sender_worktree, receiver_repo_path,
                sender_name,
            )
            split = split_frontmatter(content)
            if split is not None and owners:
                content = (
                    (split.preamble or "")
                    + "---\n"
                    + (split.fm_text if split.fm_text.endswith("\n") else split.fm_text + "\n")
                    + "---"
                    + _qualify_unqualified_citations(
                        split.body_with_leading_newline, qualifiers, sender_name,
                        owners,
                    )
                )
            if owners:
                notice = _warn_once(
                    sender_worktree, f"citation-lint:{topic}",
                    _citation_lint_notice(list(owners), sender_name, owners),
                )
                if notice is not None:
                    _LOG.warning(notice)

    if held:
        return build_setup_error_result(_MODE, dry_run, _held_once_refusal(held))

    target_file = inbox_dir / filename
    # AC6 leg 1 — existence pre-check, independent of the O_EXCL leg below.
    # Extended beyond the inbox (item 52): a memo already ARCHIVED under this
    # exact filename is just as much a collision as one still sitting in the
    # inbox — checking the inbox alone let a resend of an already-actioned
    # topic slip past this pre-check the moment the original was swept into
    # cross-repo/archive/, silently duplicating a memo the receiver had
    # already dispositioned.
    archive_file = memo_archive_dest(receiver_repo_path, target_file)
    rel_path = os.path.relpath(target_file, receiver_repo_path).replace(os.sep, "/")
    landing = _receiver_landing.resolve_receiver_landing(receiver_repo_path)
    if landing.mode == _receiver_landing.MODE_REF_DIRECT:
        archive_rel = os.path.relpath(archive_file, receiver_repo_path).replace(os.sep, "/")
        collision_exists = _landing_tree_has(
            receiver_repo_path, landing, [rel_path, archive_rel]
        )
        collision_path = archive_file if collision_exists else target_file
    else:
        collision_exists = target_file.exists() or archive_file.exists()
        collision_path = target_file if target_file.exists() else archive_file

    if dry_run:
        return build_dry_run_result(_MODE, [{
            "id": str(target_file),
            "topic": topic,
            "to": to,
            "target_path": str(target_file),
            "landing_mode": landing.mode,
            "landing_ref": landing.ref_relpath,
            "collision": collision_exists,
            "note": (
                "collision: a memo already exists at this receiver-inbox path "
                "(or was already archived under this filename) — refuse (no "
                "clobber)."
                if collision_exists else None
            ),
        }])

    # ── act path ──────────────────────────────────────────────────────────
    if collision_exists:
        return build_act_result(_MODE, [], [], [{
            "id": str(target_file),
            "reason": (
                f"collision: {collision_path} already exists in the receiver's "
                f"inbox or archive — refuse (no clobber)."
            ),
        }])

    landing = _receiver_landing.apply_receiver_landing(landing)
    ref_direct = landing.mode == _receiver_landing.MODE_REF_DIRECT
    if not ref_direct:
        inbox_dir.mkdir(parents=True, exist_ok=True)
        try:
            # C1 (git_native.commit_authored_new_file) never writes the
            # worktree itself — it commits bytes the caller already holds. This
            # write IS that caller-side write. newline="\n" is pinned per the
            # plan's own instruction: CR content is one of the two cases the
            # zero-spawn commit arm refuses outright.
            fd = os.open(str(target_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
        except FileExistsError:
            # AC6 leg 2 — the race the pre-check above cannot close alone.
            return build_act_result(_MODE, [], [], [{
                "id": str(target_file),
                "reason": (
                    f"collision (race): {target_file} appeared between the "
                    f"collision-check and the O_EXCL write — refuse (no clobber)."
                ),
            }])
        except OSError as exc:
            return build_act_result(_MODE, [], [], [{
                "id": str(target_file), "reason": f"write-failed: {exc}",
            }])

    # target_file was resolved via `_resolve_receiver_inbox` ->
    # `memo_corpus.receiver_inbox_root` above, so it already reflects
    # whichever root (legacy `cross-repo/` or migrated `state/cross-repo/`)
    # THIS receiver actually resolves to — never a fixed `cross-repo/inbox/`
    # literal, which is wrong for any receiver that has migrated.
    msg_file = _write_msg_file(
        _delivery_commit_message(topic, from_id, sent_by)
    )
    try:
        commit_result = git_native.commit_authored_new_file(
            rel_path, content, msg_file, receiver_repo_path,
            refresh_shared_index=False,
            onto_ref=landing.ref_relpath if ref_direct else None,
        )
    finally:
        try:
            msg_file.unlink()
        except OSError:
            pass

    if not commit_result.ok:
        # AC4 — fail loud, never fall back to a spawning/hook-running commit
        # in the receiver's tree. Per the plan's ordering guarantee, the
        # sender-side receipt below is never written for a delivery that did
        # not durably commit.
        #
        # The file this call created is REMOVED before returning, so a declined
        # commit leaves the receiver's tree exactly as it was found. That is a
        # rollback of this op's own write, not the spawning-commit fallback AC4
        # forbids -- nothing is committed, and no hook in the receiver's tree
        # fires either way.
        #
        # Leaving it behind was worse than untidy, and "recoverable by the
        # receiver's next session-init sweep" overstated the recovery. The
        # orphan is materially delivered (a reader with that tree open sees the
        # memo) while every ledger says it was not: the sender's draft is still
        # `status: draft` and no receipt exists. Worse, it is unrecoverable
        # through this op -- the retry is refused by AC6's no-clobber guard
        # against the very file the failed attempt wrote, so the only ways
        # forward were hand-removing a file from a repo we do not own or
        # hand-committing into it, both of which the cross-repo memo channel
        # exists to prevent. Removing our own uncommitted write closes that
        # trap: the state is clean, and re-running `memo.send` just works.
        #
        # Unlinking is safe precisely here and nowhere else: the O_EXCL open
        # above proves the file did not exist before this call, and the failed
        # commit proves nothing references it.
        rollback_detail = "" if ref_direct else _rollback_unwritten_file(target_file)
        return build_act_result(_MODE, [], [], [{
            "id": str(target_file),
            "reason": (
                f"receiver-side commit declined: {commit_result.stderr} — "
                f"not retried, per AC4;{rollback_detail}"
            ),
        }])

    delivery_commit_sha = commit_result.stdout.strip() or None

    # `commit_result.ok` is a return value, not a verified fact.
    if delivery_commit_sha is None or not _delivery_commit_is_object(
        receiver_repo_path, delivery_commit_sha
    ):
        rollback_detail = "" if ref_direct else _rollback_unwritten_file(target_file)
        return build_act_result(_MODE, [], [], [{
            "id": str(target_file),
            "reason": (
                f"receiver-side commit could not be verified: "
                f"{delivery_commit_sha or ''!r} is not a commit object in "
                f"the receiver's repository — not sent."
                + rollback_detail
            ),
        }])

    receiver_index_warning = (
        None if ref_direct
        else _record_delivery_in_receiver_index(receiver_repo_path, rel_path, content)
    )
    if receiver_index_warning is not None:
        _LOG.warning("%s", receiver_index_warning)

    # ── anchor: the durable record (C4) ─────────────────────────────────
    # `target_file`'s bytes on disk are exactly the bytes the commit above
    # hashed -- read them back rather than reuse in-memory `content`, so the
    # anchored blob is provably identical to what the receiver's own history
    # committed. Common dir resolved the same way `_delivery_commit_is_object`
    # already does, in-process, zero spawns.
    common_dir = resolve_git_common_dir(receiver_repo_path)
    anchored_bytes = (
        content.encode("utf-8") if ref_direct else target_file.read_bytes()
    )
    anchor_blob_sha = write_anchor(
        common_dir, filename, delivery_commit_sha, anchored_bytes
    )
    push_cadence.note_foreign_delivery(
        receiver_repo_path, landing.ref_relpath[len(_HEADS_PREFIX):]
    )
    anchored = anchor_blob_sha is not None
    anchor_ref = (
        ANCHOR_REF_PREFIX + filename + "/" + delivery_commit_sha
        if anchored else None
    )
    anchor_warning = None
    if not anchored:
        # A failed anchor does NOT fail the send -- the delivery commit has
        # already landed, and refusing now would be a false negative. One
        # fact, one alternative: register style, no apology, no override key.
        anchor_warning = (
            f"memo.send: anchor write for {filename} lost its CAS race — "
            f"re-run memo.send, or the receiver's next workday-start "
            f"adopts it."
        )
        _LOG.warning(
            "memo_send: delivery to %s landed and committed (%s), but the "
            "anchor write lost its CAS race for %s — re-run memo.send, or "
            "the receiver's next workday-start adopts it.",
            to, delivery_commit_sha, filename,
        )

    delivered_to = _portable_delivered_to_form(receiver_repo_path, target_file, all_repos)

    # ── cc: each cc target gets its OWN write+commit+anchor (klabauter#46,
    # second half) ──────────────────────────────────────────────────────
    # Every name in cc_targets already resolved before to:'s own write
    # (see `_resolve_cc_targets`), so a per-receiver failure HERE is a
    # write/commit-time failure, not an unresolvable-name one — it is
    # reported below as a partial failure (DETERMINATE-PARTIAL, exit_code
    # 2), never silently dropped, and never undoes to:'s already-landed
    # delivery.
    cc_delivered = []
    cc_failed = []
    for cc_name, cc_inbox_dir, cc_receiver_repo_path in (cc_targets or []):
        cc_result = _deliver_cc_copy(
            name=cc_name, inbox_dir=cc_inbox_dir,
            receiver_repo_path=cc_receiver_repo_path, filename=filename,
            content=content, topic=topic, from_id=from_id, sent_by=sent_by,
        )
        if cc_result["ok"]:
            cc_delivered.append(cc_result)
        else:
            cc_failed.append(cc_result)
            _LOG.warning(
                "memo_send: cc delivery to %s failed for topic %s: %s",
                cc_name, topic, cc_result["reason"],
            )

    # ── sender-side receipt: sent/ copy + ledger row + one commit ──────────
    sent_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        draft_text = draft_path.read_text(encoding="utf-8")
        sent_content = _stamp_sent_copy(
            draft_text, sent_at=sent_at, delivered_to=delivered_to,
        )
    except (OSError, ValueError) as exc:
        return build_act_result(
            _MODE,
            [{"id": str(target_file), "written": True, "committed": True,
              "delivery_commit_sha": delivery_commit_sha}],
            [], [{
                "id": str(draft_path),
                "reason": (
                    f"delivery landed but the sender-side sent-copy stamp "
                    f"failed: {exc} — the receiver already has the memo; "
                    f"fix the draft's frontmatter and re-run the sender-side "
                    f"receipt manually."
                ),
            }],
        )

    sent_path = _sent_path(sender_worktree, topic)
    sent_path.parent.mkdir(parents=True, exist_ok=True)
    sent_path.write_text(sent_content, encoding="utf-8", newline="\n")
    draft_removed = True
    try:
        draft_path.unlink()
    except OSError as exc:
        draft_removed = False
        _LOG.warning(
            "memo_send: could not remove original outbox draft %s after "
            "moving it to sent/ (%s) — the sent/ copy is authoritative; a "
            "leftover draft copy is a stale duplicate, not a data-loss risk.",
            draft_path, exc,
        )

    ledger_path = _sent_ledger_path(sender_worktree)
    row = _ledger_row(
        topic=topic, to=to, kind=fm.get("kind"), summary=delivered_fm.get("summary"),
        delivered_to=delivered_to, in_reply_to=fm.get("in_reply_to"),
        delivery_commit_sha=delivery_commit_sha,
        delivery_branch=commit_result.cas_ref_relpath,
        sent_by=sent_by,
        anchor_ref=anchor_ref,
    )
    appended_line = json.dumps(row, ensure_ascii=False) + "\n"
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=_SENT_LEDGER_MAX_AGE_DAYS
    )

    def _mutate(old_text: str) -> str:
        """Append this send's row, evicting the oldest rows past
        `_SENT_LEDGER_MAX_ROWS`.

        Safe to trim HERE and only here: `locked_rmw` holds the
        cross-process exclusive lock across this whole read-modify-write, so
        the text being trimmed is the union of every completed append and no
        peer can be mid-append inside it (see the commit call's own
        WORKTREE-BYTES note below for why that union is what gets
        committed).

        Rows are re-emitted rather than sliced out of `old_text` so a file
        left without a trailing newline -- by a truncated write, or a hand
        edit -- cannot silently glue this row onto the last one.

        Both bounds apply, age first: the row cap governs a prolific sender
        (claude-klabauter/DoE), the age cap governs every other repo, and which one
        bites is a property of the repo, never of this code.
        """
        rows = [line for line in old_text.splitlines() if line.strip()]
        rows = [line for line in rows if not _row_is_older_than_cutoff(line, cutoff)]
        kept = rows[-(_SENT_LEDGER_MAX_ROWS - 1):] if _SENT_LEDGER_MAX_ROWS > 1 else []
        return "".join(line + "\n" for line in kept) + appended_line

    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        locked_rmw(ledger_path, _mutate, repo_root=sender_worktree, missing_ok=True)
    except (OSError, LockTimeout, RuntimeError) as exc:
        return build_act_result(
            _MODE,
            [{"id": str(target_file), "written": True, "committed": True,
              "delivery_commit_sha": delivery_commit_sha}],
            [], [{
                "id": str(ledger_path),
                "reason": (
                    f"delivery landed and the sent/ copy was staged, but the "
                    f"ledger append failed: {exc} — the receiver already has "
                    f"the memo; the sender-side commit did not run."
                ),
            }],
        )

    # Relative to `sender_worktree` from the ACTUAL resolved locations, not
    # a fixed root: `draft_path` may be under either outbox root
    # (`_draft_path`'s dual-root resolution), while `sent_path`/`ledger_path`
    # are always the new root (their own accessors never resolve legacy).
    def _relpath(p: Path) -> str:
        return os.path.relpath(str(p), str(sender_worktree)).replace(os.sep, "/")

    sent_relpath = _relpath(sent_path)
    outbox_relpath = _relpath(draft_path)
    ledger_relpath = _relpath(ledger_path)
    sender_message = apply_missing_trailers(
        f"memo.send: {topic} delivered to {to}\n\n"
        f"Moves the outbox draft to sent/ and appends the sent-ledger row.\n",
        sender_worktree,
        [sent_relpath, ledger_relpath, outbox_relpath],
    )
    # THE OUTBOX PATH IS NAMED ONLY IF HEAD KNOWS IT.
    # It is named so the MOVE's deletion leg lands -- but a draft staged by
    # `memo.draft` and sent straight away was never committed, so after the
    # move that path is both gone from disk and unknown to git, and naming it
    # fails the WHOLE commit: the receipt never lands and the sent/ copy plus
    # ledger row sit uncommitted. That is the documented canonical workflow
    # (draft -> send), so this fired on the first two real sends, 2026-08-25.
    #
    # `_head_entry_for` answers "is this in HEAD?" from the in-process tree
    # spine, so the check costs ZERO git spawns. An untracked draft has no
    # deletion to land, so dropping it loses nothing.
    # AND ONLY IF THE MOVE ACTUALLY REMOVED IT. The unlink above degrades to a
    # warning, so HEAD membership alone would declare a deletion for a file
    # still sitting in the outbox -- a phantom deletion, which
    # `git/commit.py :: commit_paths` refuses outright, turning a tolerated
    # stale duplicate into a failed send. The leftover draft stays tracked,
    # which is the same "stale duplicate, not data loss" the warning already
    # accepts.
    sender_deleted = (
        [outbox_relpath]
        if draft_removed
        and git_native._head_entry_for(sender_worktree, outbox_relpath) is not None
        else []
    )

    # WORKTREE BYTES, NOT THE STAGED BLOB, AND THAT IS THE WHOLE POINT HERE.
    # `ledger_relpath` names a fleet-shared file: every sending session appends
    # its row to the same file (a bounded ring -- see
    # `_SENT_LEDGER_MAX_ROWS`), and the `locked_rmw` above
    # holds a cross-process exclusive lock across the read-modify-write, so
    # the WORKTREE copy is always the union of every completed append. The
    # staged blob is not: an index entry for this path goes stale the moment
    # a peer appends, and `commit_scoped` -- which this call replaces --
    # commits the STAGED blob. Measured on this tree 2026-08-30: ten
    # consecutive memo.send commits landed the byte-identical stale blob
    # b2a5f7d9d (2376 rows) while every one of their parents held a richer
    # one, so each row appended in between vanished from HEAD, the sender's
    # own included. Committing the worktree commits the union, and this arm
    # then cannot drop a peer's row.
    #
    # `prefer_staged` is deliberately NOT passed: naming the ledger there
    # selects the losing arm. `prefer_deliberate_stage` stays False for the
    # neighbouring reason its own contract gives -- the other two paths are
    # authored by this pass, so there is no third party's partial stage to
    # preserve, only stale blobs a widened default could hide.
    # state/bug-backlog/2026-08-27-commit-v2-cutover-silently-flips-whose-c-
    # 09cf57f3b909.yaml
    try:
        outcome = commit_paths(
            sender_worktree,
            [sent_relpath, ledger_relpath],
            sender_message,
            deleted_paths=sender_deleted,
            blob_fallback=partial(
                hash_worktree_blobs_via_spawn, cwd=sender_worktree
            ),
        )
        sender_commit = _SenderCommit(True, outcome.sha, "")
    except IndexStaleAfterCommit as exc:
        # THE RECEIPT LANDED; only the index is stale. This clause is the one
        # the sibling below said to add "at the same time as the raiser" --
        # `commit.py` is now that raiser. It MUST precede the
        # `IndexWriteError` clause, which is its own base class: reaching that
        # clause instead would report a committed receipt as uncommitted and
        # invite an operator to redo work already in history.
        landed = getattr(exc, "outcome", None)
        sender_commit = _SenderCommit(
            True, getattr(landed, "sha", None), ""
        )
    except (CommitRefused, FilterUnsupported, IndexWriteError) as exc:
        # `IndexWriteError` (and its `IndexWriteLockBusy` subclass) belongs
        # here for the SAME reason the other two do: by this line the receiver
        # commit has already landed, so anything that only stops the SENDER's
        # receipt is the "uncredited delivery is merely untidy" case the module
        # docstring's ordering guarantee describes -- reported on the envelope
        # as `sender_committed: false`, never as a failed send.
        #
        # It was missing, and at the ~50-session load norm a peer holding
        # `.git/index.lock` is routine, not exotic. The escape turned a
        # fully-delivered memo into a bare `-32603 Internal error:
        # IndexWriteLockBusy` at the JSON-RPC boundary, which reads as total
        # failure -- so the honest caller response is a retry, and the retry
        # DOUBLE-DELIVERS. Example-retrieval-repo-em hit exactly this: receiver commit,
        # ledger row and delivery all landed, and the op reported an internal
        # error (memo `cross-repo/inbox/2026-09-01-example-retrieval-repo-em-ceremony-
        # engine-defects-second-repo-confirmation.md`).
        #
        # SUBCLASS HAZARD, for whoever adds the first raiser:
        # `IndexStaleAfterCommit` is also an `IndexWriteError`, but it means
        # the commit LANDED and only the index is stale -- routing it here
        # would report a landed receipt as uncommitted. Nothing raises it
        # today (checked: no raise site in the tree), so no branch guards it
        # yet; add one ahead of this clause at the same time as the raiser.
        sender_commit = _SenderCommit(False, None, str(exc))

    acted_item = {
        "id": str(target_file),
        "written": True,
        "committed": True,
        "delivery_commit_sha": delivery_commit_sha,
        "anchored": anchored,
        "sender_committed": bool(sender_commit.ok),
        # A DELIVERY THAT CANNOT NAME ITS SENDER SAYS SO, AT SEND TIME.
        # The sentinel is otherwise write-only: it lands in the delivered
        # memo's frontmatter and the ledger row, and nothing reads either
        # again, so the sender learns nothing and the receiver learns only
        # when it tries to reply. That is exactly how the 2026-08-25 rebuild
        # shipped 67 unattributed memos over three days before the RECEIVING
        # repo reported it back to us. Surfacing it on the envelope makes the
        # next occurrence cost one line at send time instead of a cross-repo
        # memo, a sizing, and two sessions' investigation.
        "sender_unattributed": sent_by == _SENT_BY_UNRESOLVED,
        "sent_receipt": sent_relpath,
    }
    if citations_unresolved:
        acted_item["citations_unresolved"] = citations_unresolved
    if cc_delivered:
        acted_item["cc_delivered"] = cc_delivered
    if anchor_warning is not None:
        acted_item["anchor_warning"] = anchor_warning
    if receiver_index_warning is not None:
        acted_item["receiver_index_warning"] = receiver_index_warning
    if not sender_commit.ok:
        # The stderr is the whole diagnosis, and a WARNING alone loses it: the
        # engine's log is not retained, so an operator sees only the CLI's
        # generic line and cannot tell WHICH failure they hit. That cost two
        # sends to diagnose the pathspec cause fixed on 2026-08-25 (569e39e1b),
        # and it recurred on a DIFFERENT cause immediately after, with the
        # reason again unavailable. Carry it on the result.
        acted_item["sender_commit_stderr"] = (sender_commit.stderr or "").strip()
        _LOG.warning(
            "memo_send: delivery to %s landed and committed (%s), but the "
            "sender-side receipt commit failed: %s — sent/ copy and ledger "
            "row are staged on disk, uncommitted.",
            to, delivery_commit_sha, sender_commit.stderr,
        )

    result = build_act_result(_MODE, [acted_item], [], list(cc_failed))
    result["_scope_touch_paths"] = [str(sent_path), str(ledger_path)]
    return result
