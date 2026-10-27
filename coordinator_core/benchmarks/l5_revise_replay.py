"""Pure L5 revise-loop evaluator over a recorded rounds fixture.

Per round, answers two questions against the last complete round: did spend
drop (`spend_axis`) and was every prior requirement carried (`content_axis`).
Reads only the dict it is handed; no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoundVerdict:
    """One round's verdict. `lost_requirement_ids` lists prior requirement ids not covered."""

    round: int
    complete: bool
    spend_total: int | None
    spend_axis: str  # 'first' | 'drop' | 'rise' | 'incomplete'
    content_axis: str  # 'first' | 'holds' | 'lost' | 'incomplete'
    lost_requirement_ids: tuple[str, ...]


def _normalise(text: str) -> str:
    return " ".join(text.split()).lower()


def requirement_covered(prior_source_text: str, current_source_texts: list[str]) -> bool:
    """True when the normalised prior text is a substring of some current text.

    Traps: row ids are re-minted on every refire, so ids are never compared.
    A relayed refire also paraphrases its requirement, so under substring
    coverage every non-first complete r52 round reads 'lost'; that is a
    property of this measure, not proof the content was dropped.
    """
    prior = _normalise(prior_source_text)
    return any(prior in _normalise(text) for text in current_source_texts)


def _is_complete(rnd: dict) -> bool:
    return rnd.get("status") == "completed" and bool(rnd.get("plan_rows"))


def _total(rnd: dict) -> int | None:
    tokens = rnd.get("tokens")
    return tokens.get("total") if isinstance(tokens, dict) else None


def evaluate_rounds(data: dict) -> list[RoundVerdict]:
    """Verdict per round of `data["rounds"]`; incomplete rounds are skipped as comparison points.

    Raises ValueError when a complete round's `requirements_before` ids differ
    from the last complete round's requirement ids.
    """
    verdicts: list[RoundVerdict] = []
    prior: dict | None = None
    for rnd in data["rounds"]:
        number = rnd["round"]
        total = _total(rnd)
        if not _is_complete(rnd):
            verdicts.append(RoundVerdict(number, False, total, "incomplete", "incomplete", ()))
            continue
        reqs = rnd.get("requirements") or []
        if prior is None:
            verdicts.append(RoundVerdict(number, True, total, "first", "first", ()))
            prior = rnd
            continue
        prior_reqs = prior.get("requirements") or []
        before = rnd.get("requirements_before")
        if before is not None and [r["id"] for r in before] != [r["id"] for r in prior_reqs]:
            raise ValueError(
                f"round {number} requirements_before ids do not match round {prior['round']} requirements"
            )
        current_texts = [r["source_text"] for r in reqs]
        lost = tuple(
            r["id"] for r in prior_reqs if not requirement_covered(r["source_text"], current_texts)
        )
        prior_total = _total(prior)
        spend = "drop" if total is not None and prior_total is not None and total < prior_total else "rise"
        verdicts.append(RoundVerdict(number, True, total, spend, "lost" if lost else "holds", lost))
        prior = rnd
    return verdicts


def holds(verdicts: list[RoundVerdict]) -> bool:
    """True iff every complete non-first round is a spend drop and holds content."""
    return all(
        v.spend_axis == "drop" and v.content_axis == "holds"
        for v in verdicts
        if v.complete and v.spend_axis != "first"
    )
