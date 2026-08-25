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
  3. Sender commit: one `commit_scoped` call over the three sender-side
     paths (new `sent/` file, deleted outbox original, appended ledger row)
     — one `git add` pathspec covers all three (Anti-scope § 6: a bare
     `git commit -- <path>` cannot stage the new `sent/` file alone).

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
  - Does NOT resolve `sent_by` fresh at send time when the draft already
    carries one (2026-08-13 session-identity contract) — the draft's own
    `sent_by:` value is threaded straight through. `_SENT_BY_UNRESOLVED` is
    the explicit sentinel for the absent case, never silent omission.
  - Does NOT overwrite an existing receiver-inbox file — refused twice,
    independently: an existence pre-check AND the `O_EXCL` open flag (AC6).
  - Does NOT trust a wire-supplied inbox path — `to` is resolved solely via
    `_memo_resolver.resolve_receiver_inbox` (registry-enumerated); the
    receiver-side write target is never wire-derived.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

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
from coordinator_core.ops.fleet._memo_summary import is_placeholder_summary

_LOG = logging.getLogger(__name__)

# Mode constant for the envelope mode field (memo.send is a single-mode op).
_MODE = "send"

# sent_by (2026-08-13 session-identity-earns-its-keep) — explicit sentinel for
# "the draft this send reads never resolved its own session id" — a memo that
# cannot name its sender must SAY SO, never omit the field silently. This op
# does NOT re-resolve session identity at send time (the plan's own
# instruction): whatever the draft's `sent_by:` carries — a resolved UUID, or
# this sentinel already stamped by memo.draft/memo.compose — is threaded
# straight through unchanged.
_SENT_BY_UNRESOLVED = "unresolved"

# Param keys this handler declares; anything else fails loud rather than
# being silently dropped (mirrors the pre-kill C9/A11 fix's discipline).
_KNOWN_PARAM_KEYS = frozenset({"dry_run", "topic"})

_OUTBOX_DIRNAME = ("state", "memo-outbox")
_SENT_SUBDIRNAME = ("state", "memo-outbox", "sent")
_SENT_LEDGER_FILENAME = "sent-ledger.jsonl"
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
    *, fm: dict, body: str, today: str,
) -> tuple[Optional[str], Optional[str]]:
    """Compose the delivered (status: open) memo content from a draft's
    parsed frontmatter + body. Returns (content, error) — exactly one non-None.

    Required fields on the draft: title, from, to, kind, summary (or a
    derivable prose body when summary is the memo.draft placeholder ruler).
    """
    title = fm.get("title")
    from_id = fm.get("from")
    to = fm.get("to")
    kind = fm.get("kind")
    summary = fm.get("summary")
    if is_placeholder_summary(summary):
        summary = None  # let _compose_memo derive from body

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
            sent_by=fm.get("sent_by") or _SENT_BY_UNRESOLVED,
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

    today = datetime.date.today().isoformat()
    content, compose_error = _compose_delivered_content(fm=fm, body=body, today=today)
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
        f"cross-repo memo delivery: {topic}\n\n"
        f"Delivered by memo.send from {from_id}.\n"
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
    try:
        draft_path.unlink()
    except OSError as exc:
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
        sent_by=fm.get("sent_by") or _SENT_BY_UNRESOLVED,
    )
    appended_line = json.dumps(row, ensure_ascii=False) + "\n"

    def _mutate(old_text: str) -> str:
        return old_text + appended_line

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
    sender_msg_file = _write_msg_file(
        f"memo.send: {topic} delivered to {to}\n\n"
        f"Moves the outbox draft to sent/ and appends the sent-ledger row.\n"
    )
    try:
        # commit_scoped chooses its own safe mechanism and computes trailers
        # itself (interpret-trailers) — unlike commit_authored_new_file,
        # this is a same-tree commit, so the normal trailer machinery
        # applies. One `add` pathspec covers all three paths (Anti-scope §6:
        # `git commit -- <path>` alone cannot stage a brand-new file).
        sender_commit = git_native.commit_scoped(
            [sent_relpath, outbox_relpath, _SENT_LEDGER_RELPATH],
            sender_msg_file, sender_worktree,
        )
    finally:
        try:
            sender_msg_file.unlink()
        except OSError:
            pass

    acted_item = {
        "id": str(target_file),
        "written": True,
        "committed": True,
        "delivery_commit_sha": delivery_commit_sha,
        "sender_committed": bool(sender_commit.ok),
    }
    if not sender_commit.ok:
        _LOG.warning(
            "memo_send: delivery to %s landed and committed (%s), but the "
            "sender-side receipt commit failed: %s — sent/ copy and ledger "
            "row are staged on disk, uncommitted.",
            to, delivery_commit_sha, sender_commit.stderr,
        )

    result = build_act_result(_MODE, [acted_item], [], [])
    result["_scope_touch_paths"] = [str(sent_path), str(ledger_path)]
    return result
