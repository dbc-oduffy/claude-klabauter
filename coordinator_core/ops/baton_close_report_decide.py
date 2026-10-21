"""Pure verdict function for `baton.close_report`: one baton's header plus its target evidence in, one verdict out.

No I/O. Evidence arrives from the target readers; the live-successor flag from the header index.
"""

from __future__ import annotations

from collections.abc import Iterable

from coordinator_core.frontmatter.baton_class import is_spinoff_kind
from coordinator_core.ops.baton_close_report_contract import (
    CONFLICT_RULE_OWN_PLAN,
    REVERSE_FIELD_SIZING,
    RUN_LIVE,
    BatonHeader,
    ConflictNote,
    Evidence,
    EvidenceClass,
    ResidueReason,
    Verdict,
)

# Evidence.target_status sentinels for a target that could not be read. None is a readable
# target with no status line (unstatused), a third case distinct from both.
STATUS_MISSING = ResidueReason.TARGET_MISSING.value
STATUS_UNREADABLE = ResidueReason.TARGET_UNREADABLE.value

_OPEN = "open"
_RUN = EvidenceClass.RUN.value


def _kind(item: Evidence) -> str:
    """done | run_ended | run_live | contradicts | missing | unreadable | unstatused.

    Only a real non-done status contradicts; an ended or live run is absence of proof.
    """
    if item["done"]:
        return "done"
    if item["class"] == _RUN:
        return "run_live" if item["target_status"] == RUN_LIVE else "run_ended"
    status = item["target_status"]
    if status is None:
        return "unstatused"
    if status == STATUS_UNREADABLE:
        return "unreadable"
    if status == STATUS_MISSING:
        return "missing"
    return "contradicts"


_DELIVERABLE_PREFIX = "dlv-"
_OWN_PLAN_FIELD = "governing_plan"


def _closes_on_own_claim(evidence: Iterable[Evidence]) -> bool:
    return any(
        e["done"] and (e["field"] == _OWN_PLAN_FIELD or e["class"] == _RUN) for e in evidence
    )


def _conflict_note(evidence: list[Evidence], kinds: list[str]) -> ConflictNote:
    """A committed run beats contradicting status even when the run joined only by shared sizing.

    A done `governing_plan` also wins when every contradicting item is an inferred sizing-join leg.
    """
    done = [e for e, k in zip(evidence, kinds) if k == "done"]
    contra = [e for e, k in zip(evidence, kinds) if k == "contradicts"]
    if any(e["class"] == _RUN for e in done):
        return {
            "done": done,
            "contradicting": contra,
            "winner": "done",
            "rule": "a committed run is ground truth; a contradicting status line lags it",
        }
    if any(e["field"] == _OWN_PLAN_FIELD for e in done) and all(
        e["field"] == REVERSE_FIELD_SIZING for e in contra
    ):
        return {
            "done": done,
            "contradicting": contra,
            "winner": "done",
            "rule": CONFLICT_RULE_OWN_PLAN,
        }
    return {
        "done": done,
        "contradicting": contra,
        "winner": "contradicting",
        "rule": "no committed run backs the done evidence, so the contradicting status stands",
    }


def close_refusal(
    kind: object, deliverable_id: object, evidence: Iterable[Evidence] = ()
) -> ResidueReason | None:
    """Why close_commit refuses a baton on header facts alone, or None.

    NO_DELIVERABLE_ID when the stripped id is not a str starting `dlv-`, else SPINOFF_KIND
    when `kind` is a spinoff under any spelling, unless `evidence` carries a done
    `governing_plan` or `run` item (the spinoff's own plan or run; a deliverable_id join to a
    parent never qualifies). Shared by `decide` and close_commit's select.
    """
    if not isinstance(deliverable_id, str) or not deliverable_id.strip().startswith(
        _DELIVERABLE_PREFIX
    ):
        return ResidueReason.NO_DELIVERABLE_ID
    if is_spinoff_kind(kind if isinstance(kind, str) else None) and not _closes_on_own_claim(
        evidence
    ):
        return ResidueReason.SPINOFF_KIND
    return None


def decide(
    header: BatonHeader, evidence: list[Evidence], has_live_successor: bool
) -> tuple:
    """Return ("closable", evidence, conflict), ("residue", reason, evidence, conflict) or ("no_evidence",).

    `conflict` is a ConflictNote when done and contradicting evidence coexist, else None.
    Precedence: nothing cited, claimed-with-done (not when every run naming the baton has
    ended), live successor, done-vs-contradicting conflict (a done run, or a done
    `governing_plan` whose only contradiction is a `plan.sizing_object` leg, wins and the verdict
    continues), header refusal (`close_refusal`), closable, then residue by target kind
    (unreadable, unstatused, missing), then no_evidence (cited targets all really not
    done). A missing, unreadable, unstatused, ended or live-run target never contradicts a
    done one.
    """
    if not evidence:
        return (Verdict.NO_EVIDENCE.value,)
    kinds = [_kind(e) for e in evidence]
    any_done = "done" in kinds
    conflict = _conflict_note(evidence, kinds) if any_done and "contradicts" in kinds else None
    run_kinds = [k for e, k in zip(evidence, kinds) if e["class"] == _RUN]
    claim_released = bool(run_kinds) and "run_live" not in run_kinds
    if any_done and header["status"] != _OPEN and not claim_released:
        return (Verdict.RESIDUE.value, ResidueReason.CLAIMED.value, evidence, conflict)
    if has_live_successor:
        return (Verdict.RESIDUE.value, ResidueReason.LIVE_SUCCESSOR.value, evidence, conflict)
    if any_done:
        if conflict is not None and conflict["winner"] == "contradicting":
            return (
                Verdict.RESIDUE.value,
                ResidueReason.EVIDENCE_CONFLICT.value,
                evidence,
                conflict,
            )
        refusal = close_refusal(header.get("kind"), header.get("deliverable_id"), evidence)
        if refusal is not None:
            return (Verdict.RESIDUE.value, refusal.value, evidence, conflict)
        return (Verdict.CLOSABLE.value, evidence, conflict)
    if "unreadable" in kinds:
        return (Verdict.RESIDUE.value, ResidueReason.TARGET_UNREADABLE.value, evidence, None)
    if "unstatused" in kinds:
        return (Verdict.RESIDUE.value, ResidueReason.TARGET_UNSTATUSED.value, evidence, None)
    if "missing" in kinds:
        return (Verdict.RESIDUE.value, ResidueReason.TARGET_MISSING.value, evidence, None)
    return (Verdict.NO_EVIDENCE.value,)
