"""
coordinator_core.session.producer_resolve — resolve the ``producer`` record
for a handoff/baton record being CREATED (the claude-klabauter engine half of the
producer axis).

Placement rationale: this sits under ``coordinator_core/session/`` rather
than inside ``ops/handoff_normalize.py`` (the module that threads the
resolved record into frontmatter) because that module is under concurrent
peer edit for the sibling ``minted_by`` axis on 2026-08-12 — a new resolver
function landing there would widen the collision surface for no reason; the
resolved *value* only needs to reach ``_normalize_one_text`` as an ordinary
caller-supplied parameter, exactly like ``minted_by``/``carried_deliverable_id``
already do. This module composes two existing session primitives
(``session.shape.producer_read``, ``session.core.resolve_session_id``) and
has no reason to live beside either of them specifically, so it gets its own
narrow module rather than growing either.

Contract (settled cross-repo, see backlinks below):
  - ``typed_command``: the normalized command name captured this session, OR
    the reserved sentinel ``"other-command"`` / ``"unresolved"``, OR ``None``
    ("nothing typed this session" — a machine-minted producer, or a session
    whose id could not be resolved at all).
  - ``op_identity``: resolved AT THE CREATION SEAM (the calling op knows,
    with certainty, which door is minting the record) — never read back out
    of session state, and validated here against the closed
    ``ProducerOpIdentity`` vocabulary (``"machine-minted"`` /
    ``"hand-authored"``); an out-of-enum literal raises rather than being
    written. ``queue_scaffold_baton`` (op-minted by construction) and
    ``handoff_author_fork`` (EM-initiated) are different call sites but BOTH
    resolve to ``"machine-minted"`` — the axis carries only machine-vs-human,
    not the finer distinction between the two machine doors; inventing a
    third enum member to carry that finer split is the mistake this
    validation guards against.

Spec backlink: state/sizings/2026-08-12-producer-axis-claude-klabauter-engine-half.yaml
Spec backlink (cross-repo contract): coordinator-claude
    docs/plans/2026-08-12-producer-axis-on-the-baton-contract.md
Spec backlink (op_identity is ours to resolve, not read back): cross-repo/inbox/
    2026-08-12-coordinator-claude-em-producer-axis-batch-normalize-back-stamp.md
    ("On the machine-vs-human bit: ... It does not belong in session state at
    all — it is a property of *which door minted the record*, known with
    certainty at the creation seam and only inferable from session state.")

Negative-spec:
  - Does NOT resolve ``op_identity`` from session state — the caller (a
    creation door) supplies its own literal, resolved at its own call site.
    This module DOES validate that literal against the closed
    ``ProducerOpIdentity`` vocabulary (see ``_VALID_OP_IDENTITIES``, derived
    from ``coordinator_core.producer_vocab.ProducerOpIdentity`` — the same
    leaf-module Literal that ``summaries.py``'s pydantic field imports) and
    raises on anything outside it —
    "the caller resolves its own literal" is not license to invent a new
    token.
  - Does NOT call ``session.shape.producer_set`` — this module only READS the
    in-flight session's captured typed command to build a NEW record for a
    record being created; it never writes ``session-shape.json`` itself.
  - Does NOT ever raise into its caller. A malformed ``session-shape.json``
    (``session.shape.producer_read`` raising ``ValueError``) degrades to the
    ``"unresolved"`` typed_command member; an unresolvable session id (no env
    var set) degrades to ``None`` ("nothing captured" — distinct from
    ``"unresolved"``, which is reserved for an actual capture failure on a
    session that DOES resolve). One malformed file must never be able to
    take down a whole-tree creation/normalize sweep.
"""

from __future__ import annotations

from typing import Optional, get_args

from coordinator_core.producer_vocab import ProducerOpIdentity
from coordinator_core.session import core
from coordinator_core.session.shape import producer_read

_VALID_OP_IDENTITIES = frozenset(get_args(ProducerOpIdentity))
"""Single source of truth: derived from the shared `ProducerOpIdentity`
Literal (`coordinator_core.producer_vocab`, a leaf module with no
third-party imports — also the definition `summaries.py`'s pydantic
`ProducerOpIdentity` field imports) rather than a hand-duplicated set, so
this validation guard cannot drift out of sync with the contract it
enforces, and so reading it here never pulls pydantic onto this path."""


def resolve_producer_for_creation(
    *, op_identity: str, cwd: Optional[str] = None
) -> dict:
    """Build the ``producer`` record for a record being CREATED right now.

    ``op_identity`` is resolved by the CALLER at its own creation seam (see
    module docstring) and never read back out of session state — this
    function does not infer it or fall back if it is empty; the two creation
    doors each supply their own non-empty literal. This function DOES
    validate that literal against the closed ``_VALID_OP_IDENTITIES`` set
    (derived from the shared ``ProducerOpIdentity`` Literal in
    ``coordinator_core.producer_vocab``), raising on anything outside it —
    the drift guard just below this docstring. Separately, ``typed_command``
    is deliberately NOT validated against coordinator-claude's 46-member command vocabulary:
    that vocabulary is declared single-point-of-truth in coordinator-claude's own repo with
    its own parity test, and re-enumerating it here would create a second
    source of truth.

    Session id resolution and ``typed_command`` derivation:
      - Resolves the current session id via
        ``coordinator_core.session.core.resolve_session_id`` (never raises;
        ``""`` means unresolvable).
      - Empty session id -> ``typed_command=None`` ("nothing captured" — not
        the same fact as a capture failure, so NOT ``"unresolved"``).
      - Non-empty session id -> reads the session's captured producer record
        via ``session.shape.producer_read``:
          - ``producer_read`` raises ``ValueError`` (malformed
            ``session-shape.json``, not a JSON object, or a non-object
            ``producer`` value) -> degrades to ``typed_command="unresolved"``.
            Contained here; never propagated to this function's caller.
          - Returns ``None`` (no ``producer`` key captured yet this session)
            -> ``typed_command=None``.
          - Returns a record whose ``typed_command`` is a ``str`` or ``None``
            -> carried through verbatim.
          - Returns a record whose ``typed_command`` is present but neither a
            ``str`` nor ``None`` (an out-of-contract shape reaching disk some
            other way) -> degrades to ``"unresolved"``, same containment
            posture as the raise case above.

    Returns ``{"typed_command": ..., "op_identity": op_identity}`` — a plain
    dict, never ``None``; this is a record being created, so there is always
    something to write (at minimum ``typed_command=None`` alongside the
    caller's own ``op_identity``).
    """
    if not op_identity or op_identity not in _VALID_OP_IDENTITIES:
        raise ValueError(
            "resolve_producer_for_creation: op_identity must be one of "
            f"{sorted(_VALID_OP_IDENTITIES)!r} (got {op_identity!r}) — the "
            "caller (a creation door) resolves its own literal from the "
            "closed, cross-repo-shared ProducerOpIdentity enum, never a "
            "new/local token"
        )

    sid = core.resolve_session_id(cwd)
    typed_command: Optional[str]

    if not sid:
        typed_command = None
    else:
        try:
            record = producer_read(sid, cwd=cwd)
        except ValueError:
            typed_command = "unresolved"
        else:
            if record is None:
                typed_command = None
            else:
                candidate = record.get("typed_command")
                if candidate is None or isinstance(candidate, str):
                    typed_command = candidate
                else:
                    typed_command = "unresolved"

    return {"typed_command": typed_command, "op_identity": op_identity}
