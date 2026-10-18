"""An EM's route preference and rationale for an engine-ask: contract, validation, scout rendering.

Stdlib-only leaf: it imports nothing from ask_contract so ask_contract may import it. The
preference is a prior for the sizing scout's judgement, never an input to route computation.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Optional

ROUTE_PREFERENCES = ("goal", "roadmap", "plan")
#: The sizing `route` value each preference names.
PREFERENCE_ROUTES = {"goal": "goal-setting", "roadmap": "roadmap", "plan": "plan"}
EM_RATIONALE_MAX_CHARS = 2000
#: Run record file under `record_dir(root, run_id)`.
ROUTE_PREFERENCE_FILE = "route-preference.json"
ROUTE_PREFERENCE_KEY = "route_preference"
EM_RATIONALE_KEY = "em_rationale"
ROUTE_OVERRIDE_REASON_KEY = "route_override_reason"
SCOUT_BLOCK_HEADING = "## EM route preference (a strong prior, not the PM's words)"


@dataclass(frozen=True)
class RoutePreference:
    route_preference: Optional[str]
    em_rationale: Optional[str]

    def is_empty(self) -> bool:
        return self.route_preference is None and self.em_rationale is None


def refusal(where: str, route_preference: object, em_rationale: object) -> Optional[str]:
    """None when each key is absent or valid; else one sentence naming the offending key."""
    if route_preference is not None and (
        not isinstance(route_preference, str) or route_preference.strip() not in ROUTE_PREFERENCES
    ):
        return f"{where}.route_preference: not one of {', '.join(ROUTE_PREFERENCES)}."
    if em_rationale is not None and (
        not isinstance(em_rationale, str)
        or not em_rationale.strip()
        or len(em_rationale.strip()) > EM_RATIONALE_MAX_CHARS
    ):
        return (
            f"{where}.em_rationale: not a non-empty string of at most "
            f"{EM_RATIONALE_MAX_CHARS} chars."
        )
    return None


def parse(route_preference: object, em_rationale: object) -> RoutePreference:
    """Stripped preference; call after `refusal`. Non-strings and blanks read as absent."""
    pref = route_preference.strip() if isinstance(route_preference, str) else ""
    why = em_rationale.strip() if isinstance(em_rationale, str) else ""
    return RoutePreference(pref or None, why or None)


def from_em_analysis(doc: Mapping) -> RoutePreference:
    em = doc.get("em_analysis") if isinstance(doc, Mapping) else None
    if not isinstance(em, Mapping):
        return RoutePreference(None, None)
    return _lenient(em.get(ROUTE_PREFERENCE_KEY), em.get(EM_RATIONALE_KEY))


def to_em_analysis(pref: RoutePreference) -> dict:
    out: dict = {}
    if pref.route_preference is not None:
        out[ROUTE_PREFERENCE_KEY] = pref.route_preference
    if pref.em_rationale is not None:
        out[EM_RATIONALE_KEY] = pref.em_rationale
    return out


def to_json(pref: RoutePreference) -> str:
    return json.dumps(
        {ROUTE_PREFERENCE_KEY: pref.route_preference, EM_RATIONALE_KEY: pref.em_rationale},
        ensure_ascii=False,
    )


def from_json(text: str) -> RoutePreference:
    """Malformed or invalid content reads as empty."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return RoutePreference(None, None)
    if not isinstance(data, Mapping):
        return RoutePreference(None, None)
    return _lenient(data.get(ROUTE_PREFERENCE_KEY), data.get(EM_RATIONALE_KEY))


def diverges(pref: RoutePreference, route: object) -> bool:
    """True only when a preference is set and `route` is not the route it names."""
    if pref.route_preference is None:
        return False
    return route != PREFERENCE_ROUTES[pref.route_preference]


def scout_block(pref: RoutePreference) -> str:
    """The scout-prompt section carrying the EM's steer; empty string for an empty preference."""
    if pref.is_empty():
        return ""
    lines = [SCOUT_BLOCK_HEADING, ""]
    if pref.route_preference is not None:
        lines.append(
            f"Route preference: {pref.route_preference} "
            f"(sizing route `{PREFERENCE_ROUTES[pref.route_preference]}`)."
        )
    if pref.em_rationale is not None:
        lines.append("Rationale:")
        lines.extend(f"> {row}" for row in pref.em_rationale.splitlines() or [""])
    lines.append("")
    lines.append(
        f"Weigh this as a strong prior; size from the substrate. When the route you assemble "
        f"differs, state why as `{ROUTE_OVERRIDE_REASON_KEY}`."
    )
    return "\n".join(lines) + "\n"


def _lenient(route_preference: object, em_rationale: object) -> RoutePreference:
    pref = route_preference.strip() if isinstance(route_preference, str) else ""
    why = em_rationale.strip() if isinstance(em_rationale, str) else ""
    return RoutePreference(pref if pref in ROUTE_PREFERENCES else None, why or None)
