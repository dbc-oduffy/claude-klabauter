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

``SESSION_LEDGER_HEADING_RE`` is the canonical detector for that same heading —
shared by the parser (``aggregate_chain_loe``) and every write-time/detection
site (``ops/handoff_author_fork.py``, ``ops/queue_scaffold_baton.py``,
``coordinator/bin/coordinator-doc-new``'s C3 refusal) so emitter, parser, and
gate agree on one grammar by construction rather than three independently
hand-typed regexes drifting apart.
Review: code-reviewer 49e8b242 P2 — unifies a near-miss between
``frontmatter.body_blocks._compile_heading_re`` (accepted `##\\s+Session\\ Ledger\\s*$`)
and the parser's own `^## Session Ledger` (single space, no trailing anchor).
The parser's grammar wins: it defines what actually gets summed.
"""

import re

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

# Canonical Session Ledger heading detector. Matches the parser's actual
# accept-set (a literal single space after "##", no trailing anchor) so a
# detection site cannot consider a heading "present" that the parser would
# never recognize as ledger-summable, or vice versa. Usable both as
# ``.search(text)`` (MULTILINE, any line in a larger document) and as
# ``.match(line)`` (per-line, anchors to position 0 of the given string —
# unaffected by the MULTILINE flag either way).
SESSION_LEDGER_HEADING_RE = re.compile(r"^## Session Ledger", re.MULTILINE)
