"""Readers for a sizing's `exit_criterion.accepted` record, which is the PM shape
(`pm_quote`, no `source`), the APM shape (`source: apm`, `apm_ruling`) or the engine's own
skip record (`source: engine-size-rule`, no words), and the engine size rule that writes it. No I/O, no imports: every gate consults this leaf."""
from __future__ import annotations

SOURCE_PM = "pm"
SOURCE_APM = "apm"
SOURCE_ENGINE = "engine-size-rule"
APM_ADMISSIBLE_MODES = ("pm", "ceo")


def acceptance_words(accepted) -> str | None:
    """The non-empty `pm_quote` or `apm_ruling` of an acceptance record, else None."""
    if not isinstance(accepted, dict):
        return None
    key = "apm_ruling" if accepted.get("source") == SOURCE_APM else "pm_quote"
    words = accepted.get(key)
    return words if isinstance(words, str) and words.strip() else None


def acceptance_source(accepted) -> str | None:
    """"apm" for an APM record, "engine-size-rule" for the engine's skip record, "pm" for any
    other non-empty record (a sourceless one is PM), None for null or empty."""
    if not isinstance(accepted, dict) or not accepted:
        return None
    source = accepted.get("source")
    return source if source in (SOURCE_APM, SOURCE_ENGINE) else SOURCE_PM


def lacks_human_acceptance(accepted) -> bool:
    """True for a null `accepted` and for the engine's skip record: neither is a PM or APM
    acceptance, so every gate that keys off "was this accepted by someone" treats them alike."""
    return accepted is None or acceptance_source(accepted) == SOURCE_ENGINE


#: PM ruling 2026-10-08: an agent-run sizing carries plan/dispatch work at XS-L straight
#: through to execution. `spec-dispatch` is the XS/S dispatch variant and has always skipped.
#: Every other route -- shape at any size, pm-decision, roadmap, goal-setting -- and every XL+
#: size keeps the touchpoints. Appetite is PM-stated only: nothing here infers it, and the
#: gate takes the route as recorded, never the `xl_exit`-resolved one.
_SKIP_SIZING_ACCEPTANCE_ROUTES = frozenset({"plan", "dispatch", "spec-dispatch"})
_SKIP_SIZING_ACCEPTANCE_TSHIRTS = frozenset({"XS", "S", "M", "L"})

#: The `by` every record of a skipped acceptance names: nobody was asked.
ENGINE_SIZE_RULE = "engine-size-rule"


def sizing_acceptance_skipped(route: object, tshirt: object) -> bool:
    """True when the engine, not the PM, discharges the sizing-stage acceptance for this
    route at this size, in every interaction mode."""
    return route in _SKIP_SIZING_ACCEPTANCE_ROUTES and tshirt in _SKIP_SIZING_ACCEPTANCE_TSHIRTS
