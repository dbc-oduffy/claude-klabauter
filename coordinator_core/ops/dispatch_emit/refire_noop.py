"""Pure guard naming a refire that can only re-halt identically.

A ledger-check or all-landed resume with a blocking item, nothing executable staged, nothing derived,
no dispatching leg, and no change since the halt (no credited id, since-halt sha, unclaimed row, revised
id, revise note or blocking id the register has since ruled) would spend the ledger-check and halt-commit agents to reach the same verdict.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from .ask_contract import (
    ALL_LANDED_HALT_KIND,
    LEDGER_HALT_KIND,
    LEG_DISPATCH,
    RESUME_LEDGER_CREDITED_FIELD,
    RESUME_LEDGER_EM_DISCHARGED_FIELD,
    RESUME_LEDGER_HALT_SHA_FIELD,
    RESUME_LEDGER_KIND_FIELD,
    RESUME_LEDGER_NOTES_FIELD,
    RESUME_LEDGER_REVISED_FIELD,
    RESUME_LEDGER_SINCE_HALT_SHAS_FIELD,
    RESUME_LEDGER_UNCLAIMED_FIELD,
    RESUME_LEDGER_UNSERVED_FIELD,
    UNCLAIMED_ROWS_HALT_KIND,
)
from .run_truth_contract import MutationReply
from .spine_read import DISPOSITION_ALIASES, NON_DISPATCHABLE_DISPOSITIONS

_BLOCKING_FIELD = "blocking"
_LEG_ACTION_FIELD = "action"
# A blocking id absent from the register's rulable ids was ruled (met/waived) after the halt: a change.
_RULABLE_FIELD = "rulable_ids"


def identical_rehalt_reason(
    resume_ledger: Mapping | None,
    manifest_rows: Sequence,
    owed: Sequence,
    gated: Sequence,
    derived: Sequence[str],
    leg: Mapping | None,
) -> str | None:
    """One-line reason when the refire can only re-halt identically, else None.

    A memo_close entry is not an executable row, so it is not a parameter: the caller passes only
    staged rows (`manifest_rows`), rows still `owed`, and `gated` rows.
    """
    if not resume_ledger:
        return None
    kind = resume_ledger.get(RESUME_LEDGER_KIND_FIELD, LEDGER_HALT_KIND)
    if kind not in (LEDGER_HALT_KIND, ALL_LANDED_HALT_KIND, UNCLAIMED_ROWS_HALT_KIND):
        return None
    blocking = resume_ledger.get(_BLOCKING_FIELD) or []
    if not blocking:
        return None
    if manifest_rows or owed or gated or derived:
        return None
    rulable = resume_ledger.get(_RULABLE_FIELD)
    if rulable is not None and any(b not in rulable for b in blocking):
        return None
    if leg and leg.get(_LEG_ACTION_FIELD) == LEG_DISPATCH:
        return None
    for field in (
        RESUME_LEDGER_CREDITED_FIELD,
        RESUME_LEDGER_SINCE_HALT_SHAS_FIELD,
        RESUME_LEDGER_UNCLAIMED_FIELD,
        RESUME_LEDGER_UNSERVED_FIELD,
        RESUME_LEDGER_REVISED_FIELD,
        RESUME_LEDGER_NOTES_FIELD,
    ):
        if resume_ledger.get(field):
            return None
    halt_sha = str(resume_ledger.get(RESUME_LEDGER_HALT_SHA_FIELD) or "")[:10] or "unknown"
    ids = ", ".join(str(b) for b in blocking)
    return (
        f"refire would re-halt identically: {ids} still blocking and nothing changed since halt {halt_sha}. "
        "`workflow.revise` a note naming the work, or land it."
    )


def unserved_blocking_reason(
    resume_ledger: Mapping | None,
    serving: Sequence[Mapping],
) -> str | None:
    """One-line reason when a ledger-check refire has no open spine row serving a blocking id, else None.

    `serving` is the spine rows serving a blocking id (`ledger_remaining_paths.serving_rows`). A row is
    open when its disposition is dispatchable and the plan has not em-discharged it. A revise note or a
    blocking id the register has since ruled releases it. A dispatch leg built from the ledger's remaining
    paths does not: it carries no spine row, so it cannot discharge the id.
    """
    if not resume_ledger:
        return None
    if resume_ledger.get(RESUME_LEDGER_KIND_FIELD, LEDGER_HALT_KIND) != LEDGER_HALT_KIND:
        return None
    blocking = resume_ledger.get(_BLOCKING_FIELD) or []
    if not blocking or resume_ledger.get(RESUME_LEDGER_NOTES_FIELD):
        return None
    rulable = resume_ledger.get(_RULABLE_FIELD)
    if rulable is not None and any(b not in rulable for b in blocking):
        return None
    discharged = resume_ledger.get(RESUME_LEDGER_EM_DISCHARGED_FIELD) or {}
    for row in serving:
        disposition = DISPOSITION_ALIASES.get(str(row.get("disposition") or "").strip(), row.get("disposition"))
        if disposition not in NON_DISPATCHABLE_DISPOSITIONS and str(row.get("id")) not in discharged:
            return None
    ids = ", ".join(str(b) for b in blocking)
    return (
        f"refire would re-halt: no open spine row serves {ids}. "
        "`plan.tasks.mutate` add a row serving it, or `workflow.revise` a note naming the work."
    )


def identical_rehalt_refusal(
    resume_ledger: Mapping | None,
    manifest_rows: Sequence,
    owed: Sequence,
    gated: Sequence,
    derived: Sequence[str],
    leg: Mapping | None,
    target: str | None = None,
) -> MutationReply | None:
    """A refused MutationReply (nothing applied) when the refire can only re-halt identically, else None."""
    reason = identical_rehalt_reason(resume_ledger, manifest_rows, owed, gated, derived, leg)
    if reason is None:
        return None
    return MutationReply(applied=(), refused=(reason,), target=target)
