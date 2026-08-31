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

  1. Receiver: write `cross-repo/inbox/<name>.md` (O_EXCL) in the receiver's
     own repo, then commit it via `git_native.commit_authored_new_file` —
     ZERO git spawns for the commit itself, one hookless `update-index`
     refresh, no hook from the receiver's tree ever fires (AC3).
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

Ordering is load-bearing: the receiver-side commit lands BEFORE the sender's
receipt is written. A receipt for an undelivered memo is a lie; an
uncredited delivery is merely untidy (recoverable by re-reading the
receiver's own inbox) — see `_memo_send`'s call order.

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
  - Does NOT fall back to a spawning, hook-running commit in the receiver's
    tree when `commit_authored_new_file` declines (AC4) — a decline fails
    the receiver item loud; the sender-side receipt is never written for
    that item (see the ordering note above).
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
  - Does NOT overwrite an existing receiver-inbox file — refused twice,
    independently: an existence pre-check AND the `O_EXCL` open flag (AC6).
  - Does NOT trust a wire-supplied inbox path — `to` is resolved solely via
    `_memo_resolver.resolve_receiver_inbox` (registry-enumerated); the
    receiver-side write target is never wire-derived.
  - Does NOT allow a send whose staged body is byte-identical (frontmatter
    stripped, trailing whitespace normalised) to another `*.md` draft
    already sitting in the same sender's `state/memo-outbox/` under a
    DIFFERENT topic — refused via `build_setup_error_result` before any
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
import tempfile
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple, Optional

from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    rebuild as _rebuild_frontmatter,
    replace_fm_field,
    split_frontmatter,
)
from coordinator_core.frontmatter.schema_validate import (
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
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, locked_rmw
from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.fleet._common import (
    build_act_result,
    build_dry_run_result,
    build_setup_error_result,
    main_worktree_root,
)
from coordinator_core.ops.fleet._memo_compose import (
    _TOPIC_SLUG_RE,
    _compose_memo,
    _memo_filename,
)
from coordinator_core.ops.fleet._memo_resolver import (
    AmbiguousReceiverError,
    RegistryReadError,
    resolve_receiver_inbox as _resolve_receiver_inbox,
    suggest_nearest_receiver as _suggest_nearest_receiver,
)
from coordinator_core.ops.fleet._memo_summary import has_prose_body, is_placeholder_summary
from coordinator_core.ops.session_context import resolve_current_session_id

_LOG = logging.getLogger(__name__)

# Mode constant for the envelope mode field (memo.send is a single-mode op).
_MODE = "send"

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

_OUTBOX_DIRNAME = ("state", "memo-outbox")
_SENT_SUBDIRNAME = ("state", "memo-outbox", "sent")
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
#: `state/memo-outbox/sent/<topic>.md` copy. The one production reader that
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
#: claude-klabauter and DoE-claude are two halves of one delivery system and send
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
_SENT_LEDGER_RELPATH = "/".join((*_OUTBOX_DIRNAME, _SENT_LEDGER_FILENAME))

# Generator-provenance: writes+commits into a registry-enumerated RECEIVER
# repo's cross-repo/inbox/ tree (a different repo, not fixed), and moves
# +ledgers into the CALLING repo's own state/memo-outbox/ tree — a
# data-dependent set of tracked paths across two repos, never one fixed
# target. Recovered from the deleted original's own declaration (git show
# 677d433eb) — the plan names this as the contract to preserve verbatim.
MUTATES = ["state/memo-outbox/sent-ledger.jsonl", "cross-repo/inbox/*.md"]


# ---------------------------------------------------------------------------
# Param validation
# ---------------------------------------------------------------------------

def _validate_send_params(params: dict):
    """Validate memo.send params; return (dry_run, topic) or a setup-error dict.

    Only `dry_run` (bool, required) and `topic` (slug, required) are
    declared — every other field this send needs comes off the caller's own
    already-staged `state/memo-outbox/<topic>.md` draft, never off the wire.
    """
    dry_run = params.get("dry_run")
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
    return sender_worktree.joinpath(*_OUTBOX_DIRNAME, f"{topic}.md")


def _sent_path(sender_worktree: Path, topic: str) -> Path:
    return sender_worktree.joinpath(*_SENT_SUBDIRNAME, f"{topic}.md")


def _sent_ledger_path(sender_worktree: Path) -> Path:
    return sender_worktree.joinpath(*_OUTBOX_DIRNAME, _SENT_LEDGER_FILENAME)


def _portable_delivered_to_form(receiver_repo_path: Path, delivered_path: Path) -> str:
    """Render `delivered_path` as the portable form stamped into
    `delivered_to` — receiver-repo-relative when possible, falling back to a
    `~/`-prefixed home-relative form, and only then to the absolute string.

    A machine-absolute path tracked into a sent memo reddens DoE's
    `test_no_posix_home_path_citations` portability gate; the receiver is
    already unambiguous from `to:` + `delivery_mode: receiver-repo`, so a
    receiver-repo-relative path loses no information. Ported verbatim from
    the deleted original (git show 677d433eb), which this same reasoning
    motivated. Separators are always normalized to `/`.
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
    outbox_dir: Path, topic: str, normalized_body: str,
) -> Optional[str]:
    """Scan sibling `*.md` drafts directly in `outbox_dir` (non-recursive —
    `sent/` is a subdirectory and is never visited) for one whose body
    normalises byte-identical to `normalized_body` under a DIFFERENT topic.

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
            other_text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        other_body = parse_frontmatter(other_text).get("body")
        if other_body is None:
            continue
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


def _compose_delivered_content(
    *, fm: dict, body: str, today: str, sent_by: str,
) -> tuple[Optional[str], Optional[str]]:
    """Compose the delivered (status: open) memo content from a draft's
    parsed frontmatter + body. Returns (content, error) — exactly one non-None.

    Required fields on the draft: title, from, to, kind, a body with prose in
    it, and summary (or a derivable prose body when summary is the memo.draft
    placeholder ruler).
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
        # reached DoE-claude as frontmatter plus four empty comment blocks,
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
            scoped_to=fm.get("scoped_to"),
            in_reply_to=fm.get("in_reply_to"),
            space=fm.get("space"),
            sent_by=sent_by,
        )
    except ValueError as exc:
        return None, f"memo.send: {exc}"

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
) -> dict:
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
        "sent_by": sent_by,
    }


class _SenderCommit(NamedTuple):
    """The sender-side receipt commit's outcome, in the two fields the
    envelope below reads. `commit_paths` signals failure by raising and
    success by returning a `CommitOutcome`, so the two arms are normalised
    here rather than at each of the four read sites."""

    ok: bool
    sha: Optional[str]
    stderr: str


def _row_is_older_than_cutoff(line: str, cutoff: datetime.datetime) -> bool:
    """True iff this ledger line carries a `sent_at` older than `cutoff`.

    UNDATABLE ROWS ARE NEVER EVICTED BY AGE -- a line that is not JSON, or
    carries no `sent_at`, or carries one this cannot parse, returns False
    and lives until the row cap reaches it. Dropping a row for failing to
    prove its own age would delete the oldest rows in the file (the ones
    predating the field) on the first append after this shipped, which is
    the opposite of what an age bound is for.

    `sent_at` is written by `_ledger_row` as `%Y-%m-%dT%H:%M:%SZ`. Parsed
    with `fromisoformat` after swapping the `Z`, which Python's parser did
    not accept before 3.11 and this repo's floor is 3.11; a value in any
    other shape is undatable by the rule above, not an error.
    """
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        return False
    if not isinstance(row, dict):
        return False
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
# Handler
# ---------------------------------------------------------------------------

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

    if repo_root is None:
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.send: no repo_root supplied — memo.send reads the CALLING "
            "repo's own state/memo-outbox/ and requires a resolved worktree "
            "(common_dir-keyed op).",
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

    # Duplicate-body detector — a byte-identical body under a DIFFERENT
    # topic in the same outbox is almost always a stale duplicate draft, not
    # two intentional sends. Skipped for an empty body: the `--empty-body`
    # opt-in path can legitimately stage two deliberately body-less memos,
    # which are not a collision. Runs at the OP layer (not the CLI), so a
    # direct `coordinator-invoke memo.send` is covered too.
    normalized_body = _normalize_body(body)
    if normalized_body:
        outbox_dir = sender_worktree.joinpath(*_OUTBOX_DIRNAME)
        colliding_topic = _find_duplicate_draft_topic(
            outbox_dir, topic, normalized_body,
        )
        if colliding_topic is not None:
            return build_setup_error_result(
                _MODE, dry_run,
                f"memo.send: staged body is byte-identical to draft "
                f"{colliding_topic!r} in {outbox_dir} — rewrite this body, "
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
    filename = _memo_filename(today, from_id, topic)

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
        suggestion = _suggest_nearest_receiver(to, all_repos)
        suggestion_clause = f" Did you mean {suggestion!r}?" if suggestion else ""
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.send: UNKNOWN RECEIVER — {to!r} does not resolve to any "
            f"registered receiver on this machine.{suggestion_clause} Register "
            f"the receiver repo first (machine-local set repos.<name> "
            f"<abs-path-to-repo>), or check for a typo in the draft's `to:`.",
        )

    target_file = inbox_dir / filename
    # AC6 leg 1 — existence pre-check, independent of the O_EXCL leg below.
    collision_exists = target_file.exists()

    if dry_run:
        return build_dry_run_result(_MODE, [{
            "id": str(target_file),
            "topic": topic,
            "to": to,
            "target_path": str(target_file),
            "collision": collision_exists,
            "note": (
                "collision: a memo already exists at this receiver-inbox path "
                "— refuse (no clobber)."
                if collision_exists else None
            ),
        }])

    # ── act path ──────────────────────────────────────────────────────────
    if collision_exists:
        return build_act_result(_MODE, [], [], [{
            "id": str(target_file),
            "reason": (
                f"collision: {target_file} already exists in the receiver's "
                f"inbox — refuse (no clobber)."
            ),
        }])

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

    rel_path = "cross-repo/inbox/" + filename
    msg_file = _write_msg_file(
        _delivery_commit_message(topic, from_id, sent_by)
    )
    try:
        commit_result = git_native.commit_authored_new_file(
            rel_path, content, msg_file, receiver_repo_path,
        )
    finally:
        try:
            msg_file.unlink()
        except OSError:
            pass

    if not commit_result.ok:
        # AC4 — fail loud, never fall back to a spawning/hook-running commit
        # in the receiver's tree. The file is left written+uncommitted in a
        # repo we do not own (recoverable by the receiver's own next
        # session-init sweep) — nothing here retries or escalates the write.
        # Per the plan's ordering guarantee, the sender-side receipt below
        # is never written for a delivery that did not durably commit.
        return build_act_result(_MODE, [], [], [{
            "id": str(target_file),
            "reason": (
                f"receiver-side commit declined: {commit_result.stderr} — "
                f"the memo file was written but NOT committed into the "
                f"receiver's tree; not retried, per AC4."
            ),
        }])

    delivery_commit_sha = commit_result.stdout.strip() or None
    delivered_to = _portable_delivered_to_form(receiver_repo_path, target_file)

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
        sent_by=sent_by,
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

    sent_relpath = "/".join((*_SENT_SUBDIRNAME, f"{topic}.md"))
    outbox_relpath = "/".join((*_OUTBOX_DIRNAME, f"{topic}.md"))
    sender_message = apply_missing_trailers(
        f"memo.send: {topic} delivered to {to}\n\n"
        f"Moves the outbox draft to sent/ and appends the sent-ledger row.\n",
        sender_worktree,
        [sent_relpath, _SENT_LEDGER_RELPATH, outbox_relpath],
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
    # `_SENT_LEDGER_RELPATH` is fleet-shared: every sending session appends
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
            [sent_relpath, _SENT_LEDGER_RELPATH],
            sender_message,
            deleted_paths=sender_deleted,
            blob_fallback=partial(
                hash_worktree_blobs_via_spawn, cwd=sender_worktree
            ),
        )
        sender_commit = _SenderCommit(True, outcome.sha, "")
    except (CommitRefused, FilterUnsupported) as exc:
        sender_commit = _SenderCommit(False, None, str(exc))

    acted_item = {
        "id": str(target_file),
        "written": True,
        "committed": True,
        "delivery_commit_sha": delivery_commit_sha,
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
    }
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

    result = build_act_result(_MODE, [acted_item], [], [])
    result["_scope_touch_paths"] = [str(sent_path), str(ledger_path)]
    return result
