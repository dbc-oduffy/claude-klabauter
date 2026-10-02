"""coordinator_core.completion_receipts.verdict — the refusal predicates and the run verdict.

`mint_refusal` is `review_stamp.mint`'s refusal ladder in the order mint states it, returning
mint's exact message strings; `judge_verdict` decides agent-delivered from the same record before
the terminal commit exists.
"""

from __future__ import annotations

from typing import Any

_JUDGE_IDENTITY = "execute-review"


def _count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return len(value) if isinstance(value, (list, tuple, dict)) else 0


def superseding_delivery(record: dict, delivery: dict) -> dict:
    """The delivery block a superseding record is minted against: its own when it carries a
    verdict, else PASS read off a `met` criterion with an observation (the fresh review's
    judgment), since a stranded run has no delivery stage to return one. A recorded FAIL stands."""
    if delivery.get("verdict") is not None:
        return delivery
    criterion = record.get("criterion")
    if not isinstance(criterion, dict) or criterion.get("status") != "met":
        return delivery
    observation = criterion.get("observation")
    if not isinstance(observation, str) or not observation.strip():
        return delivery
    return {**delivery, "verdict": "PASS"}


def _unbacked_suffix(delivery: object) -> str:
    """The verifier's unbacked claims (claim and the evidence it lacked) as a refusal suffix; empty when none."""
    items = delivery.get("unbacked") if isinstance(delivery, dict) else None
    if not isinstance(items, list) or not items:
        return ""
    lines = [
        f"- {c.get('claim')} [lacked: {c.get('anchor')}]" for c in items if isinstance(c, dict)
    ]
    return "; unbacked claims:\n" + "\n".join(lines)


def mint_refusal(integration: dict, prep: dict, build_test: dict) -> str | None:
    """The first mint refusal message that applies to this record, else `None`."""
    footprint = prep.get("slice_files")
    foreign_claims = [
        c for c in prep.get("foreign_claims") or []
        if not isinstance(footprint, list) or str(c).split(" ", 1)[0] in footprint
    ]
    reviewed_files = max(_count(prep.get("slice_files")), _count(prep.get("product_files")))

    delivery = integration.get("delivery")
    delivery_verdict = delivery.get("verdict") if isinstance(delivery, dict) else None
    unresolved = integration.get("unresolved") or []
    confinement_violations = _count(integration.get("confinement_violations"))
    tests_status = build_test.get("status")
    criterion = integration.get("criterion")
    criterion_status = criterion.get("status") if isinstance(criterion, dict) else None

    if delivery_verdict != "PASS":
        return (
            f"review-stamp: refusing to mint: delivery verdict is {delivery_verdict!r}, not PASS"
            + _unbacked_suffix(delivery)
        )
    if criterion_status in ("not_met", "indeterminate"):
        return f"review-stamp: refusing to mint: exit criterion is {criterion_status}"
    if tests_status != "pass" and not (tests_status == "not_run" and criterion_status == "met"):
        return (
            f"review-stamp: refusing to mint: build/test verdict is {tests_status!r}, not pass"
            + (f" (exit criterion {criterion_status})" if tests_status == "not_run" else "")
        )
    if len(unresolved) > 0:
        return f"review-stamp: refusing to mint: {len(unresolved)} unresolved finding(s)"
    if confinement_violations > 0:
        return f"review-stamp: refusing to mint: {confinement_violations} confinement violation(s)"
    if len(foreign_claims) > 0:
        return f"review-stamp: refusing to mint: {len(foreign_claims)} foreign claim(s) on spine paths"
    if reviewed_files == 0:
        return "review-stamp: refusing to mint: zero files in the reviewed diff"
    return None


def judge_verdict(
    record: dict | None, *, all_rows_landed: bool, now: str
) -> tuple[str | None, dict | None]:
    """`("agent-delivered", judge)` only for a record mint would accept, a met criterion with an
    observation, and every row landed; otherwise `(None, None)`."""
    if not isinstance(record, dict) or not all_rows_landed:
        return None, None
    prep = record.get("prep")
    build_test = record.get("tests")
    if not isinstance(prep, dict) or not isinstance(build_test, dict):
        return None, None
    if mint_refusal(record, prep, build_test) is not None:
        return None, None
    criterion = record.get("criterion")
    if not isinstance(criterion, dict) or criterion.get("status") != "met":
        return None, None
    observation = criterion.get("observation")
    if not isinstance(observation, str) or not observation.strip():
        return None, None
    return "agent-delivered", {
        "identity": _JUDGE_IDENTITY,
        "observation_summary": observation.strip(),
        "judged_at": now,
    }
