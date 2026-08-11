"""
coordinator_core.session_ledger

Chain-walk aggregators over handoff Session Ledger blocks (T3a-g3d).

Port of: aggregate-chain-loe.sh (example-doctrine-repo b644d5a9, 2026-07-22).

``SESSION_LEDGER_BLOCK_LINES`` is the canonical Session Ledger block emitted
by every write-time author of a ledger-owing handoff — the emitter side of
the contract this package's own parser (``aggregate_chain_loe.parse_session_ledgers``,
``_ONELINE_RE``) reads. One definition, shared by ``coordinator/bin/coordinator-doc-new``
and the op-side authors (``ops/handoff_author_fork.py``, ``ops/queue_scaffold_baton.py``)
that bypass it — do not fork this literal per caller.

Spec backlink: docs/plans/2026-08-11-ledger-owing-handoff-kinds-emit-the-sess.md § C1/C2
"""

# Canonical Session Ledger block, shared verbatim by every ledger-owing handoff author.
# The comment's one-line grammar MUST stay the format ``parse_session_ledgers`` reads
# (``_ONELINE_RE``) — do not fork this literal per-kind or per-caller.
SESSION_LEDGER_BLOCK_LINES: list[str] = [
    "## Session Ledger",
    "",
    "<!-- Phase 2 LoE accumulator. Each session appends one line. -->",
    "<!-- Format: YYYY-MM-DD | <sid6> | <tshirt> | <Nd / No> | <one-line summary> -->",
    "",
]
