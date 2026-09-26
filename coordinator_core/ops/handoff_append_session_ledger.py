"""
coordinator_core.ops.handoff_append_session_ledger — JSON-RPC
"handoff.append_session_ledger" operation.

Purpose: a BOUNDED WRAPPER over `handoff.correct_body` (never re-implements
its write path — see that module for the ownership arms, archive-follow
resolution, terminal-state refusal, and stamped paper trail this op
inherits identically), same shape as `handoff.discharge_criteria`. This op
is the APPEND half of AC-5,
`state/handoffs/2026-08-21-handoffs-and-spinoffs-minimal-for-hand-rolling.md`
("The Session Ledger row is machine-appended wherever the appending
ceremony knows the values") — the CATCH half already shipped as
`handoff.author_lint`'s `LEDGER_ROW_UNPARSEABLE` finding.

Of the row's five fields (`YYYY-MM-DD | <sid6> | <tshirt> | <Nd / No> |
<summary>`), FOUR are machine-resolvable at append time and this op
resolves them itself rather than accepting them as params (a caller
supplying a wrong date/sid6/tshirt/count would be exactly the "chore in
disguise" the parent baton exists to remove):

    date              `datetime.now(timezone.utc)`, unless `created` is
                       supplied (recovery/backfill use only — see param doc).
    sid6              the calling session's own id, resolved via
                       `ops.session_context.resolve_current_session_id` —
                       the SAME resolver `handoff.correct_body` uses for its
                       own ownership gate, so "whose row is this" and "who is
                       authorized to write it" agree by construction.
    Nd / No            counts from that session's own
                       `dispatched-agents.txt`, via
                       `session_ledger.aggregate_chain_loe.
                       _count_dispatches_from_agents_file` — the SAME reader
                       the chain aggregator's dispatch-fallback path already
                       uses (see that module for the file's shape). Absence
                       of the file defaults to `(0, 0)`: unlike the
                       chain-aggregator's fallback (reconstructing a PAST,
                       possibly-crashed session's history, where absence is
                       genuinely ambiguous — see `_dispatch_fallback_record`'s
                       own negative-spec), this op is invoked BY the live
                       session it appends a row FOR, at close, and
                       `dispatched-agents.txt` is created lazily on a
                       session's first dispatch (`hooks.track_dispatched_
                       agents`) — its absence here means "this session
                       dispatched nothing," a real, meaningful zero, not an
                       unmeasured unknown. THAT PREMISE HOLDS ONLY WHILE THE
                       APPENDING SESSION IS THE SESSION THE ROW IS FOR: supply
                       the `session_id` param and it inverts, absence becomes
                       unmeasured, and the op REFUSES rather than writing
                       `0d / 0o` — see the handler's own comment for the
                       measured case. On that path the counts come from the
                       `agent_dispatches` / `opus_dispatches` params.
    tshirt             `loe_thresholds.compute_tshirt(agent_dispatches,
                       opus_dispatches, em_tokens=None, thresholds)` — the
                       same any-criterion table `aggregate_chain_loe` uses.
                       `em_tokens` stays `None` (unknown): no on-disk,
                       per-session EM-token counter exists to read at append
                       time, matching every other write-time site's silence
                       on that metric (the oneline grammar itself does not
                       carry `em_tokens` at all — see `aggregate_chain_loe.
                       _parse_oneline_row`).

The FIFTH field, `summary`, is EM judgment (R1/R6, the baton's governing
split) and stays a REQUIRED param — this op does not derive, template, or
guess it from the title, the diff, or anything else. Doing so would be body
content generation, which the parent baton's "What must NOT be automated"
section forbids outright.

Idempotence (AC-5's freeze half): `/pickup`'s negative-spec freezes a
claimed body as narrative, with `## Session Ledger` the ONE carve-out
allowing an appended row per closing ceremony, never edited after. This op
refuses outright (before any write) only if a row IDENTICAL to the one
about to be written — same `session_ledger.row_identity(session_id,
summary)`, the one shared identity `session_ledger.aggregate_chain_loe`'s
own dedup also uses — already exists anywhere in the target's `##
Session Ledger` block(s): a stray double-dispatch of this op with the same
params, or a re-invocation of `/handoff`/`/workstream-complete` with
nothing new to say, must not duplicate the row. A session closing via a
SECOND, distinct ceremony (different `closing_ceremony`, hence a different
rendered summary) is a genuinely different row and is NOT refused — the
freeze is on repeating the identical row, not on a session ever writing a
second one.

Mechanism: reads the target's CURRENT body (read-only, pre-lock — mirrors
`handoff_discharge_criteria._resolve_read_path`, duplicated here deliberately
for the same "resolve where to insert" reason, never for the write itself)
to locate the `## Session Ledger` block's insertion point and the
already-appended session ids, builds a body-unique `old_string` ->
`new_string` INSERTION (append, not replace — `new_string` is `old_string`
plus the new row line), then delegates the entire write — ownership gate,
archive-follow, terminal-state refusal, all D2 bounds, the stamped
correction note — to `handoff_correct_body._handler`, imported and called
directly (the same reach-into-a-sibling-op convention `handoff_correct_body`
itself and `handoff_discharge_criteria` both use).

Authority: DR-247 sanctions `handoff.correct_body`; DR-274 § D3 extends that
sanction to `handoff.discharge_criteria` as a second body-mutating verb
built the same way. This op is a third, same shape, same sanction chain.

Self-registration: importing this module fires
``@register_op("handoff.append_session_ledger")`` as a side-effect. Added to
`coordinator_core/ops/__init__.py`'s eager import list to trigger
registration at `start_server()` time.

Registration completeness is computed, not remembered — see
`coordinator_core/authz/registration_quad.check_registration_quad()` for the
authoritative five-surface set and the guards that enforce it.

Exit-code contract: identical shape to `handoff_correct_body`'s:
    exit_code 0, applied True  — row appended, correction note stamped.
    exit_code 1, applied False — refused; a DISTINCT `error` string per
                                  precondition (this module's own resolution
                                  preconditions, or any of
                                  `handoff_correct_body`'s, forwarded as-is).

Negative-spec:
    - Does NOT re-implement `handoff_correct_body`'s write path, ownership
      gate, archive-follow resolution, terminal-state refusal, or stamped
      paper trail — all inherited by delegation.
    - Does NOT generate, template, or derive `summary` — required, EM-supplied,
      verbatim.
    - Does NOT edit or replace an already-appended row IDENTICAL to the one
      about to be written — refuses outright rather than risk a silent
      duplicate. Does NOT refuse a second, genuinely distinct row for the
      SAME session (a different `closing_ceremony` or summary) — one row
      per closing ceremony, not one row per session.
    - Does NOT derive, template, or guess `closing_ceremony` — free-form,
      optional, EM/caller-supplied verbatim; no enumerated vocabulary.
    - Does NOT touch any file outside `state/handoffs/`/`archive/handoffs/` —
      inherited from `handoff_correct_body`'s own containment.
    - Does NOT write a zero it did not measure: on the `session_id`-override
      path with no `dispatched-agents.txt` and no supplied counts, it refuses.
      A `0d / 0o` row parses perfectly and sums to nothing in
      `session_ledger.aggregate_chain_loe`, so a wrong row here is silent.
    - Does NOT retrofit an existing chain — this is an append-only door onto
      the target's OWN `## Session Ledger` block, one call, one row.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.ipc import register_op
from coordinator_core.loe_thresholds import compute_tshirt
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import handoff_archive_dest, main_worktree_root
from coordinator_core.ops.handoff_correct_body import _MAX_OLD_STRING_LEN
from coordinator_core.ops.handoff_correct_body import _handler as _correct_body_handler
from coordinator_core.ops.session_context import resolve_current_session_id
from coordinator_core.session import core as _session_core
from coordinator_core.session_ledger import SESSION_LEDGER_HEADING_RE, row_identity
from coordinator_core.session_ledger.aggregate_chain_loe import (
    _ONELINE_RE,
    _count_dispatches_from_agents_file,
    format_oneline_row,
    unparseable_ledger_rows,
)

# _ANY_HEADING_RE`, not imported since that name is that module's own
_ANY_HEADING_RE = re.compile(r"^## ")

_MAX_CONTEXT_EXPANSION_LINES = 30

# Closed param set for this op — same fail-loud shape as
# `records_query._KNOWN_PARAM_KEYS` (2026-07-22 claude-central-em
# silent-param-drop memo): an unknown key must refuse loudly, never be
# silently ignored. `closing_ceremony` is listed here ahead of its own
# handling (B15a) — see that row's `depends_on` note — so a caller adopting
# it early is refused for the RIGHT reason (not yet wired) rather than the
# wrong one (typo). Envelope keys starting with `_` are tolerated
# (transport/metadata keys this handler never reads).
_KNOWN_PARAM_KEYS = frozenset(
    {
        "handoff_path", "summary", "created", "session_id",
        "agent_dispatches", "opus_dispatches", "override_reason",
        "closing_ceremony",
    }
)


def _err(msg: str) -> dict:
    return {"exit_code": 1, "applied": False, "error": msg}


def _explicit_counts(params: dict) -> "tuple[Optional[tuple[int, int]], Optional[str]]":
    supplied = {k: params[k] for k in ("agent_dispatches", "opus_dispatches") if k in params
                and params[k] is not None}
    if not supplied:
        return None, None
    if len(supplied) == 1:
        missing = ({"agent_dispatches", "opus_dispatches"} - set(supplied)).pop()
        return None, (
            f"{next(iter(supplied))} was supplied without {missing} — the two are "
            f"one measurement and are supplied together"
        )
    ad, od = supplied["agent_dispatches"], supplied["opus_dispatches"]
    for name, val in (("agent_dispatches", ad), ("opus_dispatches", od)):
        if isinstance(val, bool) or not isinstance(val, int):
            return None, f"{name} must be an integer when supplied"
        if val < 0:
            return None, f"{name} must be >= 0 when supplied"
    if od > ad:
        return None, (
            f"opus_dispatches ({od}) exceeds agent_dispatches ({ad}) — the opus "
            f"count is a subset of the dispatch count"
        )
    return (ad, od), None


def _resolve_read_path(
    handoff_path_raw: str, repo_root: Path
) -> "tuple[Optional[Path], Optional[str]]":
    worktree = main_worktree_root(repo_root)
    p = Path(handoff_path_raw)
    if not p.is_absolute():
        p = worktree / p
    allowed_roots = [worktree / "state" / "handoffs", worktree / "archive" / "handoffs"]
    try:
        p = contained_path(p, allowed_roots)
    except ValueError as exc:
        return None, f"handoff_path is malformed (cannot be resolved as a filesystem path): {exc}"
    if p is None:
        return None, (
            "handoff_path escapes state/handoffs/ and archive/handoffs/ — the "
            f"only two roots this op ever touches: {handoff_path_raw!r}"
        )
    if not p.is_file():
        archived_candidate = handoff_archive_dest(worktree, p)
        try:
            archived_candidate = contained_path(archived_candidate, allowed_roots)
        except ValueError:
            archived_candidate = None
        if archived_candidate is not None and archived_candidate.is_file():
            p = archived_candidate
        else:
            return None, (
                "handoff not found on disk (checked state/handoffs/ and "
                f"archive/handoffs/): {handoff_path_raw}"
            )
    return p, None


def _existing_row_identities(body: str) -> "list[tuple[str, str]]":
    """One ``row_identity()`` tuple per existing one-line-append row across
    every ``## Session Ledger`` block in *body*. Mirrors
    ``aggregate_chain_loe.parse_session_ledgers``'s own block-boundary scan
    (heading in, next-``##``-heading or EOF out) but captures ``summary``
    too — that module's parsed record deliberately drops it (tshirt/summary
    are display-only there), so this op re-scans with ``_ONELINE_RE``
    directly rather than widening a sibling module's return shape for one
    caller. Legacy Field/Value rows carry no single-line summary and are
    silently skipped: this op only ever WRITES the one-line-append grammar,
    so a legacy row can never collide with a row this op is about to write.
    """
    identities: "list[tuple[str, str]]" = []
    in_ledger = False
    for line in body.splitlines():
        if SESSION_LEDGER_HEADING_RE.match(line):
            in_ledger = True
            continue
        if in_ledger and _ANY_HEADING_RE.match(line) and not SESSION_LEDGER_HEADING_RE.match(line):
            in_ledger = False
            continue
        if not in_ledger:
            continue
        m = _ONELINE_RE.match(line.strip())
        if m:
            identities.append(row_identity(m.group("session_id"), m.group("summary")))
    return identities


def _find_ledger_block(lines: "list[str]") -> "tuple[Optional[int], Optional[int], Optional[str]]":
    heading_idxs = [i for i, ln in enumerate(lines) if SESSION_LEDGER_HEADING_RE.match(ln)]
    if not heading_idxs:
        return None, None, "no '## Session Ledger' heading found in the body"
    if len(heading_idxs) > 1:
        return None, None, (
            f"{len(heading_idxs)} '## Session Ledger' headings found in the body — "
            "refusing an ambiguous insertion point"
        )
    heading_idx = heading_idxs[0]
    section_end = len(lines)
    for i in range(heading_idx + 1, len(lines)):
        if _ANY_HEADING_RE.match(lines[i]) and not SESSION_LEDGER_HEADING_RE.match(lines[i]):
            section_end = i
            break
    return heading_idx, section_end, None


def _build_append_replacement(
    body: str, lines: "list[str]", heading_idx: int, section_end: int, row_line: str
) -> "tuple[Optional[str], Optional[str]]":
    """Build a body-unique `(old_string, new_string)` pair that INSERTS
    `row_line` immediately after the block's current last content line,
    expanding backward from that line for uniqueness exactly as
    `handoff_discharge_criteria._build_unique_replacement` does for its own
    (replace, not insert) case. Returns `(None, error_message)` if no
    context window within `_MAX_CONTEXT_EXPANSION_LINES` disambiguates it.
    """
    non_blank = [i for i in range(heading_idx, section_end) if lines[i].strip()]
    anchor_idx = non_blank[-1] if non_blank else heading_idx

    for back in range(0, _MAX_CONTEXT_EXPANSION_LINES + 1):
        start = anchor_idx - back
        if start < 0:
            break
        candidate = "".join(lines[start:anchor_idx + 1])
        if len(candidate) > _MAX_OLD_STRING_LEN:
            return None, (
                "cannot construct a body-unique insertion point for the "
                "Session Ledger row: the smallest disambiguating context "
                f"exceeds the {_MAX_OLD_STRING_LEN}-character cap on "
                "replacement-target size — body structure is too repetitive "
                "for this op's disambiguation bound"
            )
        if body.count(candidate) == 1:
            insertion = row_line + "\n" if candidate.endswith("\n") else "\n" + row_line + "\n"
            return candidate, candidate + insertion
    return None, (
        "cannot construct a body-unique insertion point for the Session "
        f"Ledger row within {_MAX_CONTEXT_EXPANSION_LINES} lines of context — "
        "body structure is too repetitive for this op's disambiguation bound"
    )


@register_op("handoff.append_session_ledger")
async def _handler(
    params: dict,
    repo_root: Optional[Path] = None,
) -> dict:
    """JSON-RPC "handoff.append_session_ledger" handler.

    Params:
        handoff_path (str) — same as `handoff.correct_body`'s param. Required.
        summary      (str) — the row's one-line summary. Required, non-empty.
                              EM judgment — never derived by this op.
        created      (str) — OPTIONAL `YYYY-MM-DD` (or full ISO timestamp)
                              override for the row's date. Recovery/backfill
                              use only (e.g. appending a row for a session
                              that closed on a different UTC date than the
                              caller's now) — omit it for the normal case,
                              which stamps today's UTC date.
        session_id   (str) — OPTIONAL override of the session this row is
                              FOR. Omit it for the normal case, which resolves
                              the CALLING session via the same resolver
                              `handoff.correct_body` uses for its own
                              ownership gate.
        agent_dispatches (int) — OPTIONAL, supplied WITH `opus_dispatches`
                              (the two are one measurement). Recovery/backfill
                              use only, and REQUIRED on that path when the
                              named session has no `dispatched-agents.txt` on
                              this host — see the refusal in the handler for
                              why absence there is unmeasured rather than
                              zero. Wins over the on-disk read when both
                              exist: the caller measured a session this host
                              did not run.
        opus_dispatches (int) — OPTIONAL, the opus subset of the above.
        override_reason (str) — OPTIONAL. Forwarded verbatim to
                              `handoff.correct_body` (see that op's own param
                              doc) — consulted only when the calling session
                              is neither the claim holder nor the authoring
                              session of the target.
        closing_ceremony (str) — OPTIONAL, free-form non-empty text (no
                              enumerated vocabulary — a new ceremony name
                              needs no code change here). Rendered as the
                              row's summary with a trailing
                              `(<closing_ceremony> close)`. Omitting it
                              leaves the summary unchanged; there is no
                              separate no-param branch — the SAME identity
                              rule (`session_ledger.row_identity`) applies
                              either way, so a second call for the same
                              session with a DIFFERENT `closing_ceremony`
                              (or no ceremony at all, if the first call
                              supplied one) is a distinct row and appends,
                              while an identical repeat is refused.

    Returns: `handoff_correct_body._handler`'s own result dict, verbatim
    (its own `session_id`/`session_source` name the AUTHORIZING session, per
    that op's contract — unchanged here), plus (on success) `row` (the
    appended line, without its trailing newline), `ledger_session_id`
    (whose row this is — may differ from the authorizing `session_id` only
    when a caller supplies `session_id` as a param override),
    `agent_dispatches`, `opus_dispatches`, `dispatch_source` (`params` /
    `agents_file` / `absent_means_zero` — which of the three resolutions the
    counts came from, so a later reader of a surprising row can tell a
    measured zero from a read one), and `tshirt` naming the resolved values
    actually written.
    """
    unknown_keys = sorted(
        k for k in params if k not in _KNOWN_PARAM_KEYS and not k.startswith("_")
    )
    if unknown_keys:
        valid = ", ".join(sorted(_KNOWN_PARAM_KEYS))
        return _err(
            f"unknown param(s): {', '.join(repr(k) for k in unknown_keys)}. "
            f"Valid: {valid}."
        )

    handoff_path_raw: str = params.get("handoff_path") or ""
    if not handoff_path_raw:
        return _err("missing required param: handoff_path")

    summary_raw = params.get("summary")
    if not isinstance(summary_raw, str) or not summary_raw.strip():
        return _err("missing required param: summary (must be a non-empty string)")
    summary = summary_raw.strip()

    closing_ceremony_raw = params.get("closing_ceremony")
    if closing_ceremony_raw is not None:
        if not isinstance(closing_ceremony_raw, str) or not closing_ceremony_raw.strip():
            return _err("closing_ceremony must be a non-empty string when supplied")
        closing_ceremony = closing_ceremony_raw.strip()
        row_summary = f"{summary} ({closing_ceremony} close)"
    else:
        closing_ceremony = None
        row_summary = summary

    if repo_root is None:
        return _err(
            "handoff.append_session_ledger: repo_root is required "
            "(no founding root available — handler called without socket-authoritative common_dir)"
        )

    session_id_override = params.get("session_id")
    if session_id_override is not None and not isinstance(session_id_override, str):
        return _err("session_id must be a string when supplied")

    worktree = main_worktree_root(repo_root)
    session_id = (session_id_override or "").strip() or resolve_current_session_id(worktree)
    if not session_id:
        return _err(
            "no calling session id resolvable from COORDINATOR_SESSION_ID / "
            "CLAUDE_SESSION_ID / CLAUDE_CODE_SESSION_ID, and no session_id param "
            "supplied — cannot resolve whose row this is"
        )
    # LEADING 6, matching `format_oneline_row`'s own truncation (that
    # function documents this explicitly — a 2026-08-14 corpus measurement
    # found every matchable row abbreviates the leading 6). A refusal check
    # using a DIFFERENT slice than the one the row is actually written with
    # would never match its own row.
    sid6 = session_id[:6]

    created_raw = params.get("created")
    if created_raw is not None and not isinstance(created_raw, str):
        return _err("created must be a string when supplied")
    created = (created_raw or "").strip() or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    counts_supplied, counts_err = _explicit_counts(params)
    if counts_err:
        return _err(counts_err)

    base = _session_core.sessions_dir(cwd=str(worktree))
    if base:
        agents_file = Path(base) / session_id / "dispatched-agents.txt"
        ad, od = _count_dispatches_from_agents_file(agents_file)
    else:
        ad, od = None, None

    if counts_supplied is not None:
        agent_dispatches, opus_dispatches = counts_supplied
        dispatch_source = "params"
    elif ad is not None:
        agent_dispatches, opus_dispatches = ad, (od or 0)
        dispatch_source = "agents_file"
    elif session_id_override:
        # THE ABSENCE-IS-A-REAL-ZERO RULE DOES NOT REACH THE OVERRIDE BRANCH.
        # `dispatched-agents.txt` is absent BY CONSTRUCTION and the recovery
        return _err(
            f"session_id override {session_id!r} names a session with no "
            f"dispatched-agents.txt on this host, so its dispatch counts are "
            f"unmeasured, not zero — writing zero here would render the "
            f"session's effort as nothing in session_ledger.aggregate_chain_loe, "
            f"and the one-row-per-session freeze leaves no second append to "
            f"correct it. Count that session's agent sidecars "
            f"(.coordinator-local/subagent-share/{session_id}/) and supply "
            f"agent_dispatches / opus_dispatches"
        )
    else:
        agent_dispatches, opus_dispatches = 0, 0
        dispatch_source = "absent_means_zero"
    tshirt = compute_tshirt(agent_dispatches, opus_dispatches, None)

    row_line = format_oneline_row(created, session_id, tshirt, agent_dispatches, opus_dispatches, row_summary)

    p, resolve_err = _resolve_read_path(handoff_path_raw, repo_root)
    if p is None:
        return _err(resolve_err)

    try:
        text = p.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _err(f"cannot read handoff file: {exc}")

    split = split_frontmatter(text)
    if split is None:
        return _err(f"no valid YAML frontmatter block in: {handoff_path_raw}")
    body = split.body_with_leading_newline

    new_identity = row_identity(sid6, row_summary)
    if new_identity in _existing_row_identities(body):
        return _err(
            f"a Session Ledger row identical to this one (session {sid6!r}, "
            f"summary {row_summary!r}) already exists in {handoff_path_raw} — "
            "append_session_ledger refuses only an identical row, and this one "
            "is a byte-for-byte repeat"
        )

    lines = body.splitlines(keepends=True)
    heading_idx, section_end, block_err = _find_ledger_block(lines)
    if heading_idx is None:
        return _err(block_err)

    old_string, new_string_or_err = _build_append_replacement(
        body, lines, heading_idx, section_end, row_line
    )
    if old_string is None:
        return _err(new_string_or_err)
    new_string = new_string_or_err

    check_findings = unparseable_ledger_rows("## Session Ledger\n\n" + row_line + "\n")
    if check_findings:
        return _err(
            f"constructed row failed its own round-trip check: {row_line!r} — "
            "refusing to write an unparseable row (this indicates session_id "
            f"{session_id!r} is not hex-tailed; supply a session_id override)"
        )

    correct_body_params = {
        "handoff_path": handoff_path_raw,
        "old_string": old_string,
        "new_string": new_string,
    }
    if "override_reason" in params:
        correct_body_params["override_reason"] = params.get("override_reason")

    result = await _correct_body_handler(correct_body_params, repo_root=repo_root)
    if result.get("exit_code") == 0:
        result = dict(result)
        result["row"] = row_line
        result["ledger_session_id"] = session_id
        result["agent_dispatches"] = agent_dispatches
        result["opus_dispatches"] = opus_dispatches
        result["dispatch_source"] = dispatch_source
        result["tshirt"] = tshirt
    return result
