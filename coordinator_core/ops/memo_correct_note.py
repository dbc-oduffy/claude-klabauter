"""
coordinator_core.ops.memo_correct_note — JSON-RPC "memo.correct_note" operation.

Purpose: an authorship-gated correction door for the ``decision_note`` of an
already-actioned cross-repo memo. ``memo.transition`` stamps ``status: actioned``
plus ``decision`` / ``decision_note`` / ``realized_by`` in one shot and refuses to
re-action ("cannot re-action"); its ``correct_realization`` route re-opens the
evidence pair (``realized_by`` AND ``decision_note``) together. Neither is the door
for the common case: the note carries one clause that was true when written and
false by the time it landed, and the sender — who reads the note — needs it fixed
without anyone touching the verdict or the cited evidence. Handoffs have
``handoff.correct_body`` for exactly this; this is the memo counterpart.

What it changes: ``decision_note`` only, on a decision-shape memo (``decision:`` on
disk) whose status is ``actioned`` or ``closed``. The new text replaces the old
base text; every ``[correction ...]`` clause already on the note is carried forward
in order and a fresh clause is appended, so N corrections leave N visible markers.

What it refuses to touch (immutable by construction — this must not become a
back-door re-action path): ``decision`` and ``realized_by``. Supplying either is a
refusal, not an ignored param. Verdict changes stay ``memo.transition``'s
fail-loud; an ``actioned_note``-shape memo (no ``decision:``) is refused and pointed
at ``--correct-realization``.

THE AUTHORSHIP GATE IS ANTI-ACCIDENT, NOT ANTI-ADVERSARY. The resolved calling
session id must equal the memo's ``picked_up_by`` (the claim of record that
``action`` preserves). Session identity resolves through the canonical resolver
(``COORDINATOR_SESSION_ID`` > ``CLAUDE_SESSION_ID`` > ``CLAUDE_CODE_SESSION_ID``),
a caller-controlled environment lookup: a caller that sets one passes the gate. The
actual control is the stamped correction clause every applied correction writes —
it names the resolved session id and which variable resolved it, so a spoofed call
is made visible on disk, not prevented. Do not cite the gate as a security
boundary. ``override_reason`` (JSON-RPC surface only, deliberately not plumbed
through the CLI) lets a non-author correct a note whose author session is gone; the
reason is stamped verbatim into the clause.

Commit ownership: a real write is committed in one explicit-pathspec commit of the
memo path (``memo_transition._commit_terminal_write``). An identical-note request is
an idempotent no-op with no commit.

Self-registration: importing this module fires ``@register_op("memo.correct_note")``;
``coordinator_core/ops/__init__.py`` imports it eagerly, and the op carries an
``OP_CLASSIFICATION`` entry and an ``_OP_KEY_SCOPE`` entry (``show_top`` — the memo
location comes from ``params["memo"]``, ``repo_root`` is unused, as for
``memo.transition``).

Return contract (same envelope as ``memo.transition``):
    {"exit_code": 0, "applied": True,  "message": str, "commit_sha": str}
    {"exit_code": 0, "applied": False, "message": str}   idempotent no-op
    {"exit_code": 1, "applied": False, "error": str}      refused; nothing written
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from datetime import datetime, timezone
from typing import Any

from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    read_fm_field,
    read_fm_field_unquoted,
    rebuild,
    replace_fm_field,
    split_frontmatter,
    unquote_yaml_scalar,
)
from coordinator_core.frontmatter.schema_validate import format_validation_errors
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops.handoff_correct_body import (
    _SESSION_ID_SENTINELS,
    _contains_invisible_unicode,
    _resolve_session_id_with_source,
)
from coordinator_core.ops.memo_transition import (
    _commit_terminal_write,
    _containment_check,
    _count_status_keys,
    _err,
    _normalize_oversize_summary,
    _ok,
    _validate_memo_fm,
)

# Statuses whose decision_note is a settled record the sender reads. `superseded`
# is a different terminal shape (no decision_note) and is not correctable here.
_CORRECTABLE_STATUSES = ("actioned", "closed")

# Fields this op never moves. Supplying one is a refusal so a caller cannot believe
# a verdict or evidence pointer was changed when it was silently dropped.
_IMMUTABLE_PARAMS = ("decision", "realized_by")

_CORRECTION_CLAUSE_RE = re.compile(r"\[correction [^\]]*\]")
_CORRECTION_MARKER = "[correction "


def _build_clause(session_id: str, source: str, override_reason: str) -> str:
    ts = datetime.now(timezone.utc).isoformat()
    # The clause is one bracketed run that later corrections re-find by regex, so the
    # reason may carry neither a closing bracket nor a line break.
    reason = " ".join(override_reason.replace("]", ")").split())
    suffix = f"; override_reason={reason!r}" if reason else ""
    return f"[correction {ts} by session {session_id} (resolved via {source}): decision_note corrected{suffix}]"


def _validate_new_note(note: str) -> str | None:
    if not note.strip():
        return "memo.correct_note: 'decision_note' is required and must be non-empty"
    if "\n" in note or "\r" in note:
        return (
            "memo.correct_note: decision_note must be single-line (no embedded \\n or \\r) — "
            "serialize_yaml_scalar does not support multi-line scalar values"
        )
    if _CORRECTION_MARKER in note or _contains_invisible_unicode(note):
        return (
            "memo.correct_note: decision_note may not contain a '[correction ' clause or "
            "invisible/format Unicode characters — the correction clause is stamped by "
            "this op, never supplied"
        )
    return None


def _correct(memo: str, params: dict, cwd: str | None) -> dict:
    immutable = [p for p in _IMMUTABLE_PARAMS if params.get(p)]
    if immutable:
        return _err(
            f"memo.correct_note: {', '.join(immutable)} is immutable here — this op moves "
            "decision_note only. A verdict change is memo.transition's fail-loud; evidence "
            "correction is memo.transition action --correct-realization"
        )

    new_note = params.get("decision_note")
    if not isinstance(new_note, str):
        return _err("memo.correct_note: 'decision_note' is required")
    new_note = new_note.strip()
    note_error = _validate_new_note(new_note)
    if note_error is not None:
        return _err(note_error)

    override_reason = (params.get("override_reason") or "").strip()

    session_id, source = _resolve_session_id_with_source()
    if not session_id or not source:
        return _err(
            "memo.correct_note: could not resolve a session id — set COORDINATOR_SESSION_ID, "
            "CLAUDE_SESSION_ID, or CLAUDE_CODE_SESSION_ID (the correction clause names the caller)"
        )
    if session_id in _SESSION_ID_SENTINELS:
        return _err(f"memo.correct_note: resolved session id {session_id!r} is a placeholder, not a session")

    try:
        memo_path, git_root = _containment_check(memo, cwd)
    except ValueError as exc:
        return _err(str(exc))
    except subprocess.TimeoutExpired:
        return _err(f"memo.correct_note: containment check timed out for --memo {memo!r}")

    if not memo_path.is_file():
        return _err(f"memo not found: {memo_path}")

    noop: list[dict | None] = [None]

    def _mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"no parseable YAML frontmatter in {memo}")

        dup = _count_status_keys(split.fm_text)
        if dup >= 2:
            raise MutateAbort(
                f"memo has {dup} status: keys — hand-collapse the duplicate before retrying"
            )

        status = read_fm_field(split.fm_text, "status")
        if status not in _CORRECTABLE_STATUSES:
            raise MutateAbort(
                f'unexpected current status "{status or "(missing)"}" for correct_note — '
                "expected actioned or closed (an unactioned memo takes its note at action time)"
            )
        if read_fm_field(split.fm_text, "decision") is None:
            raise MutateAbort(
                "memo has no decision on record (actioned_note shape) — correct_note moves "
                "decision_note only; use action --correct-realization to amend an actioned_note"
            )

        picked_up_by = read_fm_field_unquoted(split.fm_text, "picked_up_by")
        if not override_reason:
            if not picked_up_by:
                raise MutateAbort(
                    "memo has no picked_up_by (no claim of record) — authorship cannot be "
                    "established; pass override_reason on the JSON-RPC surface to correct it"
                )
            if picked_up_by != session_id:
                raise MutateAbort(
                    f"memo was actioned by session {picked_up_by}; calling session is "
                    f"{session_id} — only the actioning session corrects its own note"
                )

        cur_raw = unquote_yaml_scalar(read_fm_field(split.fm_text, "decision_note")) or ""
        prior_clauses = _CORRECTION_CLAUSE_RE.findall(cur_raw)
        cur_base = _CORRECTION_CLAUSE_RE.sub("", cur_raw).strip()
        cur_base = re.sub(r"\s{2,}", " ", cur_base)
        if new_note == cur_base:
            noop[0] = _ok(False, f"{memo} decision_note already reads as requested — no-op")
            return old_text

        clause = _build_clause(session_id, source, override_reason)
        combined = " ".join([new_note, *prior_clauses, clause])

        fm_text = split.fm_text
        if read_fm_field(fm_text, "decision_note") is None:
            fm_text = insert_fm_field(fm_text, "decision_note", combined, "decision", numeric_quoting=True)
        else:
            fm_text = replace_fm_field(fm_text, "decision_note", combined, numeric_quoting=True)

        fm_text = _normalize_oversize_summary(fm_text, memo)
        errors = _validate_memo_fm(fm_text)
        if errors:
            raise MutateAbort(f"memo cross-field validation failed: {format_validation_errors(errors)}")

        return rebuild(split, fm_text)

    try:
        new_text = locked_rmw(memo_path, _mutate, repo_root=git_root)
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "correct_note: unknown mutation error")
    except LockTimeout as exc:
        return _err(str(exc))
    except FileNotFoundError:
        return _err(f"memo not found: {memo_path}")

    if noop[0] is not None:
        return noop[0]

    written = split_frontmatter(new_text)
    if written is None or _count_status_keys(written.fm_text) != 1:
        return _err(f"INTERNAL ERROR — post-write status key count ≠ 1. Inspect {memo} immediately.")

    commit_sha, commit_error = _commit_terminal_write(
        memo_path, git_root, "correct_note", new_text, attributed_session_id=session_id,
    )
    if commit_error is not None:
        return _err(commit_error)
    return _ok(True, f"corrected decision_note on {memo}", commit_sha=commit_sha)


@register_op("memo.correct_note")
async def _handler(params: dict, repo_root: Any = None) -> dict:
    """JSON-RPC 'memo.correct_note' handler.

    MUTATING: rewrites one memo's ``decision_note`` in place and commits it. See the
    module docstring for the gate, the immutables, and the return contract.

    Params: ``memo`` (str, required), ``decision_note`` (str, required, single-line),
    ``cwd`` (str, optional — anchors a relative ``memo``), ``override_reason``
    (str, optional — JSON-RPC surface only; skips the authorship equality gate and is
    stamped verbatim). ``decision`` / ``realized_by`` are refused if supplied.
    """
    memo = (params.get("memo") or "").strip()
    if not memo:
        return _err("memo.correct_note: 'memo' is required")
    cwd = (params.get("cwd") or "").strip() or None
    return await asyncio.to_thread(_correct, memo, params, cwd)
