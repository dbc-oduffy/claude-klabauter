"""Pure verdict function for `baton.close_report`: one baton's header plus its target evidence in, one verdict out.

No I/O. Evidence arrives from the target readers; the live-successor flag from the header index.
"""

from __future__ import annotations

from coordinator_core.frontmatter.baton_class import is_spinoff_kind
from coordinator_core.ops.baton_close_report_contract import (
    BatonHeader,
    Evidence,
    ResidueReason,
    Verdict,
)

# Evidence.target_status sentinels for a target that could not be read. None is a readable
# target with no status line (unstatused), a third case distinct from both.
STATUS_MISSING = ResidueReason.TARGET_MISSING.value
STATUS_UNREADABLE = ResidueReason.TARGET_UNREADABLE.value

_OPEN = "open"


def _kind(item: Evidence) -> str:
    """done | contradicts | missing | unreadable | unstatused. Only a real non-done status contradicts."""
    if item["done"]:
        return "done"
    status = item["target_status"]
    if status is None:
        return "unstatused"
    if status == STATUS_UNREADABLE:
        return "unreadable"
    if status == STATUS_MISSING:
        return "missing"
    return "contradicts"


_DELIVERABLE_PREFIX = "dlv-"


def close_refusal(kind: object, deliverable_id: object) -> ResidueReason | None:
    """Why close_commit refuses a baton on header facts alone, or None.

    NO_DELIVERABLE_ID when the stripped id is not a str starting `dlv-`, else SPINOFF_KIND
    when `kind` is a spinoff under any spelling. Shared by `decide` and close_commit's select.
    """
    if not isinstance(deliverable_id, str) or not deliverable_id.strip().startswith(
        _DELIVERABLE_PREFIX
    ):
        return ResidueReason.NO_DELIVERABLE_ID
    if is_spinoff_kind(kind if isinstance(kind, str) else None):
        return ResidueReason.SPINOFF_KIND
    return None


def decide(
    header: BatonHeader, evidence: list[Evidence], has_live_successor: bool
) -> tuple:
    """Return ("closable", evidence), ("residue", reason, evidence) or ("no_evidence",).

    Precedence: nothing cited, claimed-with-done, live successor, done-vs-contradicting
    conflict, header refusal (`close_refusal`), closable, then residue by target kind
    (unreadable, unstatused, missing), then no_evidence (cited targets all really not
    done). A missing, unreadable or unstatused target never contradicts a done one.
    """
    if not evidence:
        return (Verdict.NO_EVIDENCE.value,)
    kinds = [_kind(e) for e in evidence]
    any_done = "done" in kinds
    if any_done and header["status"] != _OPEN:
        return (Verdict.RESIDUE.value, ResidueReason.CLAIMED.value, evidence)
    if has_live_successor:
        return (Verdict.RESIDUE.value, ResidueReason.LIVE_SUCCESSOR.value, evidence)
    if any_done:
        if "contradicts" in kinds:
            return (Verdict.RESIDUE.value, ResidueReason.EVIDENCE_CONFLICT.value, evidence)
        refusal = close_refusal(header.get("kind"), header.get("deliverable_id"))
        if refusal is not None:
            return (Verdict.RESIDUE.value, refusal.value, evidence)
        return (Verdict.CLOSABLE.value, evidence)
    if "unreadable" in kinds:
        return (Verdict.RESIDUE.value, ResidueReason.TARGET_UNREADABLE.value, evidence)
    if "unstatused" in kinds:
        return (Verdict.RESIDUE.value, ResidueReason.TARGET_UNSTATUSED.value, evidence)
    if "missing" in kinds:
        return (Verdict.RESIDUE.value, ResidueReason.TARGET_MISSING.value, evidence)
    return (Verdict.NO_EVIDENCE.value,)
