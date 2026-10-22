"""Cross-repo memo frontmatter contract digest: every required field, allowed value and conditional rule.

Pure string assembly over an already-loaded memo schema dict; no I/O, no spawn.
"""

from __future__ import annotations

from coordinator_core.ops.fleet.memo_kinds import VALID_KINDS

# Keys are the `__name__` of each rule in `schema_validate._MEMO_CROSS_FIELD_RULES`
# (grandfather excluded), in rule order. States no numeric limit: those drift.
MEMO_RULE_CONTRACT: dict[str, str] = {
    "_memo_cf_in_progress_needs_picked_up_by": "status=in_progress requires picked_up_by",
    "_memo_cf_actioned_decision_requires_realized_by": (
        "status=actioned with decision=accepted|partial requires realized_by "
        "(inline, a path, or a commit sha)"
    ),
    "_memo_cf_action_taken_requires_companions": "status=action_taken requires action_taken_at and decision",
    "_memo_cf_closed_requires_companions": "status=closed requires closed_at, action_taken_at and decision or actioned_note",
    "_memo_cf_superseded_requires_superseded_by": "status=superseded requires superseded_by",
    "_memo_cf_central_only_requires_to": "delivery_mode=central-only requires to",
    "_memo_cf_summary_length_cap": "summary is one line within the length cap",
    "_memo_cf_kind_enum": "kind is a sender-declared value; a received memo's kind is left as sent",
    "_memo_cf_distill_fate": "distill_fate=ratification requires in_repo_capture pointing at an in-repo path",
    "_memo_cf_disposition_superseded_requires_companions": (
        "disposition_superseded=true requires superseding_note, superseding_realized_by, "
        "superseded_at and status actioned|superseded"
    ),
    "_memo_cf_scoped_to_well_formed": (
        "scoped_to, when present, needs artifact, exactly one of version|sha, and seam"
    ),
}


def memo_contract_digest(schema: dict) -> str:
    """One-line digest: required fields with allowed values, kind vocabulary, then each conditional rule."""
    props = schema.get("properties") or {}
    parts: list[str] = []
    for field in schema.get("required") or []:
        enum = (props.get(field) or {}).get("enum")
        parts.append(f"{field} ({'|'.join(str(v) for v in enum)})" if enum else str(field))
    parts.append(f"kind (optional): {', '.join(VALID_KINDS)}")
    parts.extend(MEMO_RULE_CONTRACT.values())
    return "; ".join(parts)
