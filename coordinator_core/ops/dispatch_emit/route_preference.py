"""An EM's route preference and rationale for an engine-ask: contract, validation, scout rendering.

Stdlib-only leaf: it imports nothing from ask_contract so ask_contract may import it. The
preference is advisory input to the sizing scout, never an input to route computation.
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
ROUTE_VERDICT_KEY = "route_verdict"
VERDICT_PUSHBACK = "pushback"
VERDICT_INDEPENDENT = "independent"
SCOUT_BLOCK_HEADING = "## EM route preference (advisory, not the PM's words)"


@dataclass(frozen=True)
class RoutePreference:
    route_preference: Optional[str]
    em_rationale: Optional[str]

    def is_empty(self) -> bool:
        return self.route_preference is None and self.em_rationale is None


_NESTED_PREFERENCE_KEYS = (ROUTE_PREFERENCE_KEY, "preference", "route", "value")
_NESTED_RATIONALE_KEYS = (EM_RATIONALE_KEY, "rationale", "reason", "why")
_TRUNCATION_MARK = " [truncated]"


def refusal(where: str, route_preference: object, em_rationale: object) -> Optional[str]:
    """None when each key is absent or valid; else one sentence naming the offending key.

    The first line carries a valid example. Call `normalize` first: it repairs every
    well-meant shape, so a refusal here means the preference itself is unusable.
    """
    if route_preference is not None and (
        not isinstance(route_preference, str) or route_preference.strip() not in ROUTE_PREFERENCES
    ):
        return (
            f'{where}.route_preference: not one of {", ".join(ROUTE_PREFERENCES)} '
            f'(e.g. route_preference: "plan").'
        )
    if isinstance(em_rationale, str) and len(em_rationale.strip()) > EM_RATIONALE_MAX_CHARS:
        return (
            f"{where}.em_rationale: {len(em_rationale.strip())} chars, cap {EM_RATIONALE_MAX_CHARS}. "
            f"Trim it to {EM_RATIONALE_MAX_CHARS}; put the rest in `context`."
        )
    if em_rationale is not None and (
        not isinstance(em_rationale, str)
        or not em_rationale.strip()
        or len(em_rationale.strip()) > EM_RATIONALE_MAX_CHARS
    ):
        return (
            f'{where}.em_rationale: not a non-empty string of at most {EM_RATIONALE_MAX_CHARS} '
            f'chars (e.g. em_rationale: "spans two workstreams").'
        )
    return None


def normalize(route_preference: object, em_rationale: object) -> tuple[object, object]:
    """(route_preference, em_rationale) repaired to the shape `refusal` accepts.

    Repairs: a `route_preference` mapping carrying the preference and/or rationale
    (a flat `em_rationale` wins over a nested one); a list/number/mapping rationale
    rendered to text; blank rationale read as absent; an over-length rationale
    truncated; a preference matched case-insensitively or by its sizing route name.
    An unrecognisable preference passes through unchanged so `refusal` names it.
    """
    pref = route_preference
    if isinstance(pref, Mapping):
        nested_why = next((pref[k] for k in _NESTED_RATIONALE_KEYS if pref.get(k) is not None), None)
        pref = next((pref[k] for k in _NESTED_PREFERENCE_KEYS if pref.get(k) is not None), None)
        if em_rationale is None:
            em_rationale = nested_why
    if isinstance(pref, str):
        key = pref.strip().lower()
        by_route = {route: name for name, route in PREFERENCE_ROUTES.items()}
        key = by_route.get(key, key)
        pref = key if key in ROUTE_PREFERENCES else pref
        if not pref.strip():
            pref = None
    return pref, _rationale_text(em_rationale)


def _rationale_text(value: object) -> object:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value
    elif isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, Mapping):
        text = json.dumps(value, ensure_ascii=False, default=str)
    elif isinstance(value, (list, tuple)):
        text = "\n".join(str(v) for v in value if v is not None)
    else:
        return value
    text = text.strip()
    if not text:
        return None
    if len(text) > EM_RATIONALE_MAX_CHARS:
        text = text[: EM_RATIONALE_MAX_CHARS - len(_TRUNCATION_MARK)].rstrip() + _TRUNCATION_MARK
    return text


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
        f"This is the EM's suggestion; size from what you find in the substrate. It does not "
        f"override your finding. When the route you assemble differs, state why as "
        f"`{ROUTE_OVERRIDE_REASON_KEY}`."
    )
    return "\n".join(lines) + "\n"


def route_verdict(
    pref: RoutePreference, route: object, *, reason: Optional[str], basis: Optional[str]
) -> Optional[dict]:
    """The EM-facing verdict on the preference; None without a preference or a sized route.

    Reports on the route only; it never feeds route computation.
    """
    if pref.route_preference is None or not isinstance(route, str) or not route:
        return None
    if diverges(pref, route):
        because = f" because {reason}" if reason else ""
        return {
            "verdict": VERDICT_PUSHBACK,
            "em_preferred": pref.route_preference,
            "sized_route": route,
            "reason": reason,
            "message": f"EM preferred {pref.route_preference}; sized as {route}{because}.",
        }
    return {
        "verdict": VERDICT_INDEPENDENT,
        "em_preferred": pref.route_preference,
        "sized_route": route,
        "basis": basis,
        "message": (
            f"Sized as {route} from the substrate; matches the EM preference, "
            f"which did not decide it."
        ),
    }


def _lenient(route_preference: object, em_rationale: object) -> RoutePreference:
    pref = route_preference.strip() if isinstance(route_preference, str) else ""
    why = em_rationale.strip() if isinstance(em_rationale, str) else ""
    return RoutePreference(pref if pref in ROUTE_PREFERENCES else None, why or None)
