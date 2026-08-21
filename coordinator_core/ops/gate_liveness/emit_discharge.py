"""
coordinator_core.ops.gate_liveness.emit_discharge — compose the `discharges:`
block a discharging repo stamps on an outbound cross-repo memo when work it
owns lands and a sibling declared it as a blocker.

Purpose: this is the PRODUCER half of the gate-closure-signal contract (the
READER half is `gate_liveness.resolve`'s `closure_key` join, C1). Per the
landed contract (`plan-tasks.schema.json` 1.10.0's `external_gate[].
closure_key` + `cross-repo-memo.schema.json` 1.7.0's optional `discharges`
object — SSOT `coordinator_core.contract.emit_memo_schema`), a discharge
travels as an ORDINARY cross-repo memo carrying `{closure_key, evidence,
landed_at}`. No new `kind: discharge` enum member — the ruling declined a
four-site cross-repo lockstep on the kind vocabulary; a waiting repo finds a
discharge by the block's presence, never by `kind`.

Spec backlink: docs/plans/2026-08-21-a-discharged-gate-tells-the-row-waiting.md § C4

Scope, deliberately narrow: this module VALIDATES and COMPOSES the memo text
carrying the block — it does not deliver it. `memo.send`'s underlying
delivery path commits the written memo via a scoped git subprocess
(`coordinator_core.ops.fleet.memo_send._commit_delivered_memo`), and this
plan's Executor hard constraints forbid `subprocess`/`git` in every chunk
("No subprocess, no git. If a chunk seems to need one, that is a BLOCKED
report, not a spawn."). `emit_discharge` therefore stops at composing a
schema-valid memo document string; an actual send goes through the existing
`memo.send` op exactly as any other memo does, with this module's output
supplying the frontmatter fragment / composed body.

`evidence` reuses `realized_by`'s validated shape, so the existing validator
(`coordinator_core.frontmatter.schema_validate.
_memo_cf_actioned_decision_requires_realized_by`) does the work: `evidence`
is well-formed exactly when it is the sentinel `"inline"`, a path containing
`/`, or a 7-64 char hex commit SHA. This module does not re-derive that
regex — it constructs a synthetic frontmatter dict shaped to trip the
existing rule and reads its verdict, so the two can never drift apart.

Negative-spec: does not write a file, does not compute a `closure_key`
identity on this module's own initiative (the caller — the code that knows
what landed and what a sibling declared as its blocker — supplies
`closure_key`/`evidence`/`landed_at` verbatim), and does not touch
`memo_send.py`, `ops/__init__.py`, or `authz/classification.py` — this
module is a plain composer/validator, not a registered op (C4's `writes:`
scope names only this file and its test).
"""
from __future__ import annotations

from typing import Any, Optional

from coordinator_core.frontmatter.schema_validate import (
    _memo_cf_actioned_decision_requires_realized_by,
)
from coordinator_core.ops.fleet.memo_send import _compose_memo, _render_extra_field

#: The SAME two-member enum as the vendored plan-tasks.schema.json 1.10.0's
#: `external_gate[].closure_key.kind` (and cross-repo-memo.schema.json
#: 1.7.0's `discharges.closure_key.kind`, coordinator_core.contract.
#: emit_memo_schema._DISCHARGES_PROPERTY). Not re-derived from either
#: schema module at import time (no schema-module dependency belongs in a
#: plain composer) — kept as a literal tuple here, deliberately identical,
#: with this comment as the drift tripwire for a human reader.
CLOSURE_KEY_KINDS = ("deliverable", "memo-thread")

#: `format: date` per cross-repo-memo.schema.json's `discharges.landed_at`.
_DATE_RE_SOURCE = r"^\d{4}-\d{2}-\d{2}$"

import re as _re

_DATE_RE = _re.compile(_DATE_RE_SOURCE)


def validate_closure_key(closure_key: Any) -> Optional[str]:
    """Return an error string, or None if `closure_key` is well-formed.

    Well-formed: a mapping with exactly `kind` (one of `CLOSURE_KEY_KINDS`)
    and `id` (a non-empty string) — mirrors `_DISCHARGES_PROPERTY`'s
    `additionalProperties: False` + `required: [kind, id]` shape.
    """
    if not isinstance(closure_key, dict):
        return f"closure_key must be a mapping, got {type(closure_key).__name__}"
    unknown = set(closure_key.keys()) - {"kind", "id"}
    if unknown:
        return f"closure_key has unrecognized sub-key(s) {sorted(unknown)} — known sub-keys: ['id', 'kind']"
    kind = closure_key.get("kind")
    if kind not in CLOSURE_KEY_KINDS:
        return f"closure_key.kind must be one of {CLOSURE_KEY_KINDS}, got {kind!r}"
    id_value = closure_key.get("id")
    if not isinstance(id_value, str) or not id_value.strip():
        return "closure_key.id is required (non-empty string)"
    return None


def validate_discharge_evidence(evidence: Any) -> Optional[str]:
    """Return an error string, or None if `evidence` is well-formed.

    Reuses `realized_by`'s validated shape by constructing a synthetic
    frontmatter dict that trips `_memo_cf_actioned_decision_requires_
    realized_by`'s well-formedness branch (status=actioned,
    decision=accepted, realized_by=evidence) and reading its verdict —
    the existing validator does the work; this function does not
    re-implement the sentinel/path/SHA regex.
    """
    if not isinstance(evidence, str):
        return f"evidence must be a string, got {type(evidence).__name__}"
    synthetic_fm = {"status": "actioned", "decision": "accepted", "realized_by": evidence}
    error = _memo_cf_actioned_decision_requires_realized_by(synthetic_fm)
    if error is not None:
        return f"evidence {error['error']}"
    return None


def validate_landed_at(landed_at: Any) -> Optional[str]:
    """Return an error string, or None if `landed_at` is a well-formed
    YYYY-MM-DD date string, per cross-repo-memo.schema.json's
    `discharges.landed_at` (`format: date`)."""
    if not isinstance(landed_at, str) or not _DATE_RE.match(landed_at):
        return f"landed_at must be a YYYY-MM-DD date string, got {landed_at!r}"
    return None


def validate_discharges_block(closure_key: Any, evidence: Any, landed_at: Any) -> list[str]:
    """Validate the whole `{closure_key, evidence, landed_at}` triple.

    Returns a list of error strings; empty list = valid. Presence-triggered
    completeness (per the schema's own description): this function is only
    ever called when a caller has decided to emit a discharge — there is no
    "omit the whole block" branch here, that decision belongs to the caller.
    """
    errors: list[str] = []
    for message in (
        validate_closure_key(closure_key),
        validate_discharge_evidence(evidence),
        validate_landed_at(landed_at),
    ):
        if message is not None:
            errors.append(message)
    return errors


def render_discharges_block(closure_key: dict, evidence: str, landed_at: str) -> str:
    """Render the `discharges:` frontmatter fragment via the existing
    `_render_extra_field` nested-mapping renderer (memo_send.py) — never a
    hand-rolled YAML string, so `scoped_to`-shaped nested mappings and this
    one share exactly one rendering path."""
    return _render_extra_field(
        "discharges",
        {
            "closure_key": {"kind": closure_key["kind"], "id": closure_key["id"]},
            "evidence": evidence,
            "landed_at": landed_at,
        },
    )


def emit_discharge(
    *,
    from_id: str,
    to: str,
    topic: str,
    title: str,
    body: str,
    closure_key: dict,
    evidence: str,
    landed_at: str,
    kind: str = "fyi",
    summary: Optional[str] = None,
    today: str,
    scoped_to: Optional[dict] = None,
    campaign_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    space: Optional[str] = None,
    sent_by: Optional[str] = None,
) -> str:
    """Compose a schema-valid cross-repo memo document carrying a
    `discharges:` block.

    Validates `{closure_key, evidence, landed_at}` first (raises
    `ValueError` naming every failing sub-field, never a partial write) and
    otherwise delegates every other frontmatter concern to
    `memo_send._compose_memo` — this function does not re-implement
    required-field self-validation, summary derivation, or YAML quoting.

    The `discharges` fragment is inserted into the composed frontmatter
    immediately before the closing `---` delimiter (after any of
    `_compose_memo`'s own optional trailing fields — `supersedes`, `space`,
    `campaign_id`, `in_reply_to`, `sent_by`, `scoped_to`), via a single
    textual split on `_compose_memo`'s documented one-and-only `"\\n---\\n"`
    frontmatter-closing marker.

    Does not write a file and does not send — see module docstring's Scope
    note. The returned string is a composed memo document ready for a
    caller to persist through the existing `memo.send` delivery path.
    """
    errors = validate_discharges_block(closure_key, evidence, landed_at)
    if errors:
        raise ValueError(
            "emit_discharge: invalid discharges block: " + "; ".join(errors)
        )

    composed = _compose_memo(
        from_id=from_id,
        to=to,
        topic=topic,
        title=title,
        body=body,
        kind=kind,
        summary=summary,
        supersedes=None,
        today=today,
        scoped_to=scoped_to,
        campaign_id=campaign_id,
        in_reply_to=in_reply_to,
        space=space,
        sent_by=sent_by,
    )

    marker = "\n---\n"
    idx = composed.index(marker)
    frontmatter_body, closing_and_rest = composed[:idx], composed[idx:]
    discharges_fragment = render_discharges_block(closure_key, evidence, landed_at)
    return frontmatter_body + "\n" + discharges_fragment + closing_and_rest
