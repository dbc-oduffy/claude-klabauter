"""Pure verdict function for `baton.close_report`: one baton's header plus its target evidence in, one verdict out.

No I/O. Evidence arrives from the target readers; the live-successor flag from the header index.
"""

from __future__ import annotations

from coordinator_core.ops.baton_close_report_contract import (
    BatonHeader,
    Evidence,
    ResidueReason,
    Verdict,
)

# Evidence.target_status sentinels for a target that could not be read. None counts as unreadable.
STATUS_MISSING = ResidueReason.TARGET_MISSING.value
STATUS_UNREADABLE = ResidueReason.TARGET_UNREADABLE.value

_OPEN = "open"


def _kind(item: Evidence) -> str:
    """done | contradicts | missing | unreadable. Only a real non-done status contradicts."""
    if item["done"]:
        return "done"
    status = item["target_status"]
    if status == STATUS_MISSING:
        return "missing"
    if status is None or status == STATUS_UNREADABLE:
        return "unreadable"
    return "contradicts"


def decide(
    header: BatonHeader, evidence: list[Evidence], has_live_successor: bool
) -> tuple:
    """Return ("closable", evidence), ("residue", reason, evidence) or ("no_evidence",).

    Precedence: nothing cited, claimed-with-done, live successor, done-vs-contradicting
    conflict, closable, unreadable/missing target, no_evidence (cited targets all really not
    done). A missing or unreadable target never contradicts a done one.
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
        return (Verdict.CLOSABLE.value, evidence)
    if "unreadable" in kinds:
        return (Verdict.RESIDUE.value, ResidueReason.TARGET_UNREADABLE.value, evidence)
    if "missing" in kinds:
        return (Verdict.RESIDUE.value, ResidueReason.TARGET_MISSING.value, evidence)
    return (Verdict.NO_EVIDENCE.value,)
