"""The intake-sizing contract: the record `sizing.intake_mint` writes and engine-ask reads.

Answers "what did the EM direct for this run" (`read_em_brief`) and "is this sizing the
EM's intake surface" (`is_intake`). Pure: no I/O, no clock, no git.

The brief rides `em_analysis.em_brief` because the sizing-object schema is vendored and a
new top-level field is not ours to add. `premise.provenance` is a closed enum, so the intake
mark rides `em_analysis.intake` instead.
"""

from __future__ import annotations

from typing import Mapping, Optional

EM_BRIEF_KEY = "em_brief"
INTAKE_MARK_KEY = "intake"
INTAKE_MARK = "sizing.intake_mint"

_NAME_MAX = 60


def _em_analysis(doc: Mapping) -> Mapping:
    analysis = doc.get("em_analysis") if isinstance(doc, Mapping) else None
    return analysis if isinstance(analysis, Mapping) else {}


def read_em_brief(doc: Mapping) -> Optional[str]:
    """The stripped `em_analysis.em_brief`, or None when absent or blank."""
    value = _em_analysis(doc).get(EM_BRIEF_KEY)
    if not isinstance(value, str):
        return None
    return value.strip() or None


def is_intake(doc: Mapping) -> bool:
    """True for a sizing carrying the mint's mark and still at `draft`."""
    if not isinstance(doc, Mapping):
        return False
    return (_em_analysis(doc).get(INTAKE_MARK_KEY) == INTAKE_MARK
            and doc.get("status") == "draft")


def intake_record(*, name: str, turn: Mapping, today: str) -> dict:
    """The schema-valid sizing dict for one PM turn.

    `turn` is a PM-turn row: `text` (or the log's `prompt`), optional `turn` and `ts`.
    The estimate and route are placeholders, not a sizing judgment: the run's size stage
    judges them. `em_brief` is not written here; the caller adds it only when non-blank.
    """
    text = turn.get("text", turn.get("prompt"))
    if not isinstance(text, str) or not text.strip():
        raise ValueError("turn carries no text")
    verbatim: dict = {"text": text}
    index = turn.get("turn")
    if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
        verbatim["turn"] = index
    if "ts" in turn:
        verbatim["ts"] = turn["ts"]
    record: dict = {
        "schema": "sizing-object",
        "name": (name or "").strip()[:_NAME_MAX] or "intake",
        "intent": text,
        "intent_source": "pm-verbatim",
        "estimate": {"tshirt": "M", "provisional": True},
        "route": "plan",
        "detents": [],
        "fork": None,
        "xl_exit": None,
        "status": "draft",
        "premise": {
            "provenance": "not-applicable",
            "evidence": f"intake sizing minted {today} by {INTAKE_MARK}; "
                        "no premise judged until the size stage runs",
        },
        "pm_verbatims": [verbatim],
        "em_analysis": {INTAKE_MARK_KEY: INTAKE_MARK},
    }
    return record
