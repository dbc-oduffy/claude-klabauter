"""
coordinator_core.session_ledger

Chain-walk aggregators over handoff Session Ledger blocks (T3a-g3d).

Port of: aggregate-chain-loe.sh (DoE b644d5a9, 2026-07-22).

``SESSION_LEDGER_BLOCK_LINES`` is the canonical Session Ledger block emitted
by every write-time author of a ledger-owing handoff — the emitter side of
the contract this package's own parser (``aggregate_chain_loe.parse_session_ledgers``,
``_ONELINE_RE``) reads. One definition, shared by ``coordinator/bin/coordinator-doc-new.py``
and the op-side authors (``ops/handoff_author_fork.py``, ``ops/queue_scaffold_baton.py``)
that bypass it — do not fork this literal per caller.

Spec backlink: pln-ledger-owing-handoff-kinds-emi-648818 § C1/C2

``SESSION_LEDGER_HEADING_RE`` is the canonical detector for that same heading —
shared by the parser (``aggregate_chain_loe``) and every write-time/detection
site (``ops/handoff_author_fork.py``, ``ops/queue_scaffold_baton.py``,
``coordinator/bin/coordinator-doc-new.py``'s C3 refusal) so emitter, parser, and
gate agree on one grammar by construction rather than three independently
hand-typed regexes drifting apart.
Unifies a near-miss between
``frontmatter.body_blocks._compile_heading_re`` (accepted `##\\s+Session\\ Ledger\\s*$`)
and the parser's own `^## Session Ledger` (single space, no trailing anchor).
The parser's grammar wins: it defines what actually gets summed.
"""

import re

# (``_ONELINE_RE``) — do not fork this literal per-kind or per-caller.
# The `Nd / No` legend lines are load-bearing, not decoration. `_ONELINE_RE` binds those
SESSION_LEDGER_BLOCK_LINES: list[str] = [
    "## Session Ledger",
    "",
    "<!-- Phase 2 LoE accumulator. Each session appends one line per closing ceremony. -->",
    "<!-- Format: YYYY-MM-DD | <sid6> | <tshirt> | <Nd / No> | <one-line summary> -->",
    "<!-- N is an integer COUNT of dispatches, NOT a duration: Nd = agent dispatches, -->",
    "<!-- No = opus dispatches. e.g. `3d / 1o`. A row written as days (`0.3d`) does not -->",
    "<!-- parse, and a chain whose rows do not parse silently renders as ZERO effort. -->",
    "",
]

# ``.search(text)`` (MULTILINE, any line in a larger document) and as
# unaffected by the MULTILINE flag either way).
SESSION_LEDGER_HEADING_RE = re.compile(r"^## Session Ledger", re.MULTILINE)


def row_identity(session_id: str, summary: str) -> "tuple[str, str]":
    """The one shared definition of what makes two Session Ledger rows the
    SAME row: ``(session_id lower-cased, summary stripped)``. Two rows for
    one session with a DIFFERENT summary (e.g. a distinct
    ``closing_ceremony`` suffix — see
    ``ops.handoff_append_session_ledger``) are NOT the same row and both
    count; only a byte-identical (case-insensitive session id) pair
    collapses to one. Shared by the append op's refusal gate
    (``ops.handoff_append_session_ledger``) and the chain aggregator's
    dedup (``session_ledger.aggregate_chain_loe``) so "does this row already
    exist" and "has this row already been counted" agree by construction —
    do not fork this tuple shape per caller.
    """
    return (session_id.lower(), summary.strip())


def body_has_session_ledger_heading(body: str) -> bool:
    """Whether ``body`` (a handoff's body text, frontmatter already
    stripped) carries the canonical ``## Session Ledger`` heading.

    Thin wrapper over ``SESSION_LEDGER_HEADING_RE`` so callers checking for
    the block's presence -- `coordinator-doc-new.py`'s C3 scaffold-time
    refusal and `baton_assemble/apply.py`'s d2 body check (C1, pln-the-
    ledger-check-follows-the-body-not-ju-e2da19) -- share one predicate
    rather than each re-deriving `bool(SESSION_LEDGER_HEADING_RE.search(...))`
    inline. Do not replace the regex search with a substring test (`"##
    Session Ledger" in body`): that was already corrected once, in review
    49e8b242, because a substring test also matches the heading appearing
    inside a code fence or quoted example rather than as an actual heading
    line.
    """
    return bool(SESSION_LEDGER_HEADING_RE.search(body))
