"""Who signed off: the one provenance shape for the four approval surfaces (plan row
`pm_approved`, `grouping_approvals.<g>`, sizing `exit_criterion.accepted`, the exec-auth stamp),
the readers that normalise each, the PM countersign, and APM admissibility. No I/O.

The pinned shape (contract quoted to DoE):

    signoff:
      source: pm | apm | g-em | uhura  # pm = the PM personally verified; the rest are delegated
      pm_quote: "<PM verbatim>" # source pm only
      apm_ruling: "<APM verbatim ruling>"   # source apm only
      approver: "<session name>"            # source g-em or uhura only (DR-399, groupings only)
      ruling: "<G-EM or Uhura verbatim ruling>"  # source g-em or uhura only
      ruling_ref: "<run id or repo-relative path where the ruling is recorded>"  # delegated only, required
      on: YYYY-MM-DD
      history:                  # any source: the records a superseding re-sign replaced
        - {source: apm, apm_ruling: ..., ruling_ref: ..., on: ...}

Per surface: a row carries `signoff` beside `pm_approved: true`; a grouping block carries
`signoff`; a sizing `accepted` record IS the shape (plus `history`); the exec-auth stamp
records `execution_authorized_by` PM or APM with the words or ruling ref in the note.

A surface marked approved with no `signoff` reads `unrecorded`, never PM-verified. A non-PM
stamp `by` never reads as PM-verified.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from coordinator_core.ops.dispatch_emit.pm_adjudication import IRREVERSIBLE_GATE_SOURCE
from coordinator_core.ops.sizing_acceptance import (
    SOURCE_APM,
    SOURCE_ENGINE,
    SOURCE_PM,
    acceptance_source,
    acceptance_words,
)

SOURCE_GEM = "g-em"
SOURCE_UHURA = "uhura"
#: Every source that is not the PM: signed without human verification.
DELEGATED_SOURCES = (SOURCE_APM, SOURCE_GEM, SOURCE_UHURA)

#: The APM outranks G-EM and Uhura because its signing surface is a strict superset (rows and
#: groupings vs groupings only, DR-399); G-EM and Uhura are co-equal (DR-399).
SOURCE_RANK = {SOURCE_PM: 3, SOURCE_APM: 2, SOURCE_GEM: 1, SOURCE_UHURA: 1}

__all__ = [
    "SOURCE_PM", "SOURCE_APM", "SOURCE_GEM", "SOURCE_UHURA", "DELEGATED_SOURCES", "SOURCE_RANK",
    "may_resign", "supersede", "Signoff", "build_signoff", "countersign", "apm_admissible",
    "read_row", "read_grouping", "read_sizing_accepted", "read_exec_stamp",
]

_IRREVERSIBLE = re.compile(IRREVERSIBLE_GATE_SOURCE, re.IGNORECASE)
_ABSOLUTE = re.compile(r"^([A-Za-z]:[\\/]|[\\/])")

SURFACE_ROW = "row"
SURFACE_GROUPING = "grouping"
SURFACE_SIZING = "sizing"
SURFACE_EXEC = "exec-auth"


@dataclass(frozen=True)
class Signoff:
    """`source` is pm, a delegated source, or None when the surface is approved but
    `unrecorded`. `history` holds the records any superseding re-sign replaced, on any source. `approver` is the
    G-EM or Uhura session name."""

    source: str | None
    words: str | None
    ruling_ref: str | None
    on: str | None
    history: tuple[dict, ...]
    surface: str
    unrecorded: bool
    approver: str | None = None


def apm_admissible(ruling: Any) -> bool:
    """False when the ruling text names an irreversible or external act per
    IRREVERSIBLE_GATE_SOURCE (merge to main, push to main, force-push, publish, release, ...);
    those stay the PM's. A bare "push" is not matched. Non-string or empty text is not admissible."""
    return isinstance(ruling, str) and bool(ruling.strip()) and not _IRREVERSIBLE.search(ruling)


def _clean(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{name} must not contain a newline")
    return value


def build_signoff(source: str, words: str, ruling_ref: str | None, on: str,
                  approver: str | None = None) -> dict:
    """The pinned record. Refuses an unknown source, a delegated record without `ruling_ref`,
    a g-em or uhura record without `approver`, an absolute `ruling_ref`, and a newline in any
    value."""
    if source not in (SOURCE_PM, *DELEGATED_SOURCES):
        raise ValueError(f"source must be one of pm, apm, g-em, uhura, got {source!r}")
    words = _clean("words", words)
    on = _clean("on", on)
    if ruling_ref is not None:
        ruling_ref = _clean("ruling_ref", ruling_ref)
        if _ABSOLUTE.match(ruling_ref):
            raise ValueError("ruling_ref must be repo-relative or a run id, not an absolute path")
    if source == SOURCE_APM:
        if ruling_ref is None:
            raise ValueError("an apm signoff requires ruling_ref")
        return {"source": SOURCE_APM, "apm_ruling": words, "ruling_ref": ruling_ref, "on": on}
    if source in (SOURCE_GEM, SOURCE_UHURA):
        if ruling_ref is None:
            raise ValueError(f"a {source} signoff requires ruling_ref")
        return {"source": source, "approver": _clean("approver", approver), "ruling": words,
                "ruling_ref": ruling_ref, "on": on}
    record: dict = {"source": SOURCE_PM, "pm_quote": words, "on": on}
    if ruling_ref is not None:
        record["ruling_ref"] = ruling_ref
    return record


def may_resign(prior_source: Any, new_source: Any) -> bool:
    """True when a `new_source` signer may replace a `prior_source` approval: rank(new) >=
    rank(prior). An unknown source on either side is False."""
    prior_rank = SOURCE_RANK.get(prior_source)
    new_rank = SOURCE_RANK.get(new_source)
    return prior_rank is not None and new_rank is not None and new_rank >= prior_rank


def supersede(prior: dict, record: dict) -> dict:
    """A copy of `record` whose `history` is the prior's history plus the prior itself (minus
    its own `history`), so history stays flat. `prior` is not mutated."""
    if not isinstance(prior, dict) or prior.get("source") not in SOURCE_RANK:
        raise ValueError("supersede needs a prior signoff record with a known source")
    out = dict(record)
    out["history"] = [*(prior.get("history") or []), {k: v for k, v in prior.items() if k != "history"}]
    return out


def countersign(prior: dict, pm_quote: str, on: str) -> dict:
    """A pm record whose `history` ends with the prior delegated record (apm, g-em or uhura).
    A prior pm record is refused: there is nothing to upgrade."""
    if not isinstance(prior, dict) or prior.get("source") not in DELEGATED_SOURCES:
        raise ValueError("countersign upgrades a delegated signoff; the prior record is not one")
    return supersede(prior, build_signoff(SOURCE_PM, pm_quote, None, on))


def _from_record(rec: dict, surface: str) -> Signoff:
    source = rec.get("source")
    if source == SOURCE_APM:
        words = rec.get("apm_ruling")
    elif source == SOURCE_PM:
        words = rec.get("pm_quote")
    elif source in (SOURCE_GEM, SOURCE_UHURA):
        words = rec.get("ruling")
    else:
        source, words = None, None
    ref = rec.get("ruling_ref")
    history = rec.get("history")
    return Signoff(
        source=source,
        words=words if isinstance(words, str) and words.strip() else None,
        ruling_ref=ref if isinstance(ref, str) and ref else None,
        on=str(rec["on"]) if rec.get("on") is not None else None,
        history=tuple(h for h in history if isinstance(h, dict)) if isinstance(history, list) else (),
        surface=surface,
        unrecorded=source is None,
        approver=rec.get("approver") if isinstance(rec.get("approver"), str) else None,
    )


def _unrecorded(surface: str) -> Signoff:
    return Signoff(None, None, None, None, (), surface, True)


def _read_embedded(block: Any, approved: bool, surface: str) -> Signoff | None:
    if not isinstance(block, dict):
        return None
    sig = block.get("signoff")
    if isinstance(sig, dict) and sig:
        return _from_record(sig, surface)
    return _unrecorded(surface) if approved else None


def read_row(row: Any) -> Signoff | None:
    """A plan-tasks row: None when not approved; `unrecorded` for `pm_approved: true` with no
    `signoff`."""
    if not isinstance(row, dict):
        return None
    return _read_embedded(row, row.get("pm_approved") is True, SURFACE_ROW)


def read_grouping(block: Any) -> Signoff | None:
    """A `grouping_approvals.<g>` block: `unrecorded` when approved with no `signoff`."""
    if not isinstance(block, dict):
        return None
    return _read_embedded(block, block.get("status") == "approved", SURFACE_GROUPING)


def read_sizing_accepted(accepted: Any) -> Signoff | None:
    """A sizing `accepted` record. The engine's skip record and null read None; a sourceless
    record with words is a legacy PM acceptance; a PM record with no words is `unrecorded`."""
    src = acceptance_source(accepted)
    if src is None or src == SOURCE_ENGINE:
        return None
    words = acceptance_words(accepted)
    if words is None:
        return _unrecorded(SURFACE_SIZING)
    rec = dict(accepted)
    rec["source"] = src
    return _from_record(rec, SURFACE_SIZING)


def read_exec_stamp(fm: Any) -> Signoff | None:
    """The exec-auth stamp. `by` PM reads pm, APM reads apm, engine-size-rule and absent read
    None, and any other `by` is a delegate (apm-class) with the note as its words."""
    if not isinstance(fm, dict):
        return None
    by = fm.get("execution_authorized_by")
    if not isinstance(by, str) or not by.strip() or by.strip() == SOURCE_ENGINE:
        return None
    note = fm.get("execution_authorized_note")
    note = note if isinstance(note, str) and note.strip() else None
    at = fm.get("execution_authorized_at")
    source = SOURCE_PM if by.strip() == "PM" else SOURCE_APM
    return Signoff(
        source=source,
        words=note,
        ruling_ref=None,
        on=str(at) if at is not None else None,
        history=(),
        surface=SURFACE_EXEC,
        unrecorded=False,
    )
