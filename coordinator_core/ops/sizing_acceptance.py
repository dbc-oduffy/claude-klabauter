"""Readers for a sizing's `exit_criterion.accepted` record, which is either the PM shape
(`pm_quote`, no `source`) or the APM shape (`source: apm`, `apm_ruling`). No I/O."""
from __future__ import annotations

SOURCE_PM = "pm"
SOURCE_APM = "apm"
APM_ADMISSIBLE_MODES = ("pm", "ceo")


def acceptance_words(accepted) -> str | None:
    """The non-empty `pm_quote` or `apm_ruling` of an acceptance record, else None."""
    if not isinstance(accepted, dict):
        return None
    key = "apm_ruling" if accepted.get("source") == SOURCE_APM else "pm_quote"
    words = accepted.get(key)
    return words if isinstance(words, str) and words.strip() else None


def acceptance_source(accepted) -> str | None:
    """"apm" for an APM record, "pm" for any other non-empty record (a sourceless one is
    PM), None for null or empty."""
    if not isinstance(accepted, dict) or not accepted:
        return None
    return SOURCE_APM if accepted.get("source") == SOURCE_APM else SOURCE_PM
