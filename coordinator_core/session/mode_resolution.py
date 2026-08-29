"""
coordinator_core.session.mode_resolution — the resolution seam: a key
registry whose precedence is declared per key, on cost-incidence.

WHY THIS EXISTS

Before this module, ``autonomous_sentinel``, ``dispatch_nudge_sentinel``, and
``guard_unlock_sentinel`` were three siblings with three ``sentinel_path()``
functions and no shared resolution point — precedence between a session's
own local sentinel and a fleet-wide record (``coordinator_core.session.
fleet_mode``) had nowhere to live. This module is that point: a registry
(``MODE_KEYS``) that declares, per key, which of the two sides wins, and a
single resolver (``resolve_mode``) every caller goes through.

THE DISCRIMINATOR IS COST-INCIDENCE, NOT SUPPRESSION-VS-ESCALATION.

Each ``MODE_KEYS`` entry's ``precedence`` is decided by asking: who bears the
cost of this key being wrong? Not whether the key suppresses a signal or
escalates one — that split misclassifies the very next key added here.
``compaction_warnings`` looks like a "suppression" concern (it selects among
advisory variants), and ``autonomous`` looks like an "escalation" concern
(it changes what a session does under pressure), but sorting on that axis
would put them on the wrong sides of the line. Sorting on cost-incidence
gets both right:

  - ``compaction_warnings`` is ``fleet-wins``: the cost of this key being
    wrong lands on the human who chose not to be told — a cost that is
    externalized past the session itself, so the fleet-wide choice governs.
  - ``autonomous`` is ``session-wins``: the cost of this key being wrong
    lands on a shared tree with ~50 concurrent peers. A session cannot
    consent to that cost on third parties' behalf merely because a fleet
    record says so, so only the session's own local sentinel — something a
    human deliberately dropped for THIS session — can turn it on. A fleet
    ``autonomous: on`` value never overrides an absent session sentinel;
    see ``resolve_mode``.

``compaction_warnings`` NEVER SILENCES THE WARNING. It selects which VARIANT
of the context-pressure advisory fires (see ``_check_context_pressure_sync``
in ``coordinator_core.hooks.postuse_advisory_dispatch``, which already
implements the two variants this key's ``value_type`` enumerates) — there is
no value of this key that removes the signal. The mode changes what the
session is told to DO; it never stops the session being told. This is why
``value_type`` for this key is an enum over the existing variants, never a
boolean off-switch.

FLEET-ONLY KEYS, AND THE INVARIANT THAT KEEPS THE REGISTRY FROM LYING.
``compaction_warnings`` has no per-session sentinel to pair with today (zero
occurrences of a session-scoped compaction-warning suppression anywhere in
``coordinator_core/``) — that does not make it inert (a reader is wired
elsewhere), it makes it FLEET-ONLY: ``session_pair=None``. A key declaring
``session_pair=None`` MUST declare ``fleet-wins`` — a session-wins key with
no session-scoped value could never be won by anything, so the registry
refuses such an entry at IMPORT TIME (see ``_validate_registry`` below)
rather than shipping a toggle whose precedence is undecidable. That
invariant is asserted over ``MODE_KEYS`` itself, not merely over the two
keys shipped today, so a future key cannot reintroduce the mistake.

TWO PATHS, VISIBLY DISTINCT. An unknown key raises at the CALL site (a
programming error — the caller asked this module about a key that was never
registered) via ``KeyError``, never at the FILE-read layer: a malformed or
unrecognised key *inside* the fleet record is untrusted input, already
absorbed by ``fleet_mode.read_fleet_mode()``'s own empty-mapping
degradation, and is silently ignored here rather than raised.

``resolve_mode`` RETURNS THE KEY'S DECLARED VALUE TYPE, NOT A BOOL. Each
entry declares ``value_type``, and the resolver validates a fleet-supplied
value against it at the REGISTRY boundary — a value of the wrong type is
treated as malformed input (degrades exactly like ``fleet_mode``'s own
empty-mapping case), never coerced. ``autonomous`` declares ``bool``.
``compaction_warnings`` declares an enum (``COMPACTION_WARNING_VARIANTS``)
over the advisory variants the site already implements. This module does
not ship a boolean-only signature to widen later — the first non-boolean
key already exists at authoring time, so there is no "later" to defer to.

Negative-spec:
    - Do NOT grow a "read the key and refuse it" branch here for
      irreversible-harm guards. That is the rejected write-time shape
      wearing a new name — no hard-deny guard imports this module (see C5).
    - Do NOT enumerate sessions, ever. ``resolve_mode`` takes a
      ``session_id`` it is given and never asks who else is alive.
    - Do NOT couple ``MODE_KEYS`` to import-time knowledge of the sentinel
      siblings by storing a module reference — each entry's session-scoped
      reader is a CALLABLE taking ``session_id`` and returning that
      session's own value, nothing more.
    - Do NOT edit ``autonomous_sentinel``, ``dispatch_nudge_sentinel``, or
      ``guard_unlock_sentinel`` from this module — import them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, FrozenSet, Optional, Union

from coordinator_core.session import autonomous_sentinel
from coordinator_core.session.fleet_mode import read_fleet_mode

#: The advisory variants ``_check_context_pressure_sync`` already
#: implements: ``"standard"`` is the default handoff-recommending wording;
#: ``"informational"`` is the autonomous-run wording that replaces the
#: recommendation rather than appending to it. See module docstring.
COMPACTION_WARNING_VARIANTS: FrozenSet[str] = frozenset({"standard", "informational"})

ValueType = Union[type, FrozenSet[str]]


@dataclass(frozen=True)
class ModeKey:
    """One ``MODE_KEYS`` entry.

    ``session_pair``: a callable taking ``session_id`` and returning that
    session's own value in ``value_type``, or ``None`` for a fleet-only key
    (see module docstring "FLEET-ONLY KEYS"). Deliberately a callable, never
    a module reference — see module docstring.

    ``precedence``: ``"fleet-wins"`` or ``"session-wins"``, decided by
    cost-incidence (see module docstring).

    ``value_type``: ``bool`` or a ``frozenset`` of allowed string values
    (an enum). ``resolve_mode`` validates a fleet-supplied raw value against
    this at the registry boundary.

    ``default``: the value returned when neither side has an opinion —
    this is what makes an empty fleet mapping (``fleet_mode.
    read_fleet_mode()``'s own degradation) reproduce today's pre-plan
    behaviour exactly.
    """

    session_pair: Optional[Callable[[str], object]]
    precedence: str
    value_type: ValueType
    default: object


def _validate_value(raw: object, value_type: ValueType) -> Optional[object]:
    """Validate ``raw`` against ``value_type``; ``None`` on any mismatch.

    A mismatch (wrong type, or a string outside a declared enum) is treated
    as malformed input — the same degradation ``fleet_mode.
    read_fleet_mode()`` already applies to a structurally-wrong record,
    never a coerced value.
    """
    if value_type is bool:
        return raw if isinstance(raw, bool) else None
    if isinstance(value_type, frozenset):
        return raw if isinstance(raw, str) and raw in value_type else None
    raise TypeError(f"unsupported value_type {value_type!r}")


def _autonomous_session_value(session_id: str) -> bool:
    """This key's session-scoped reader: is ``session_id``'s own
    autonomous-run sentinel present on disk right now?

    Deliberately reimplemented here as a small callable rather than storing
    ``autonomous_sentinel`` itself in the registry — see module docstring
    "Do NOT couple ``MODE_KEYS`` to import-time knowledge of the sentinel
    siblings by storing a module reference".
    """
    return autonomous_sentinel.sentinel_path(session_id).exists()


MODE_KEYS: dict = {
    "autonomous": ModeKey(
        session_pair=_autonomous_session_value,
        precedence="session-wins",
        value_type=bool,
        default=False,
    ),
    "compaction_warnings": ModeKey(
        session_pair=None,
        precedence="fleet-wins",
        value_type=COMPACTION_WARNING_VARIANTS,
        default="standard",
    ),
}


def _validate_registry(registry: dict) -> None:
    """Enforce: a key declaring ``session_pair=None`` MUST declare
    ``fleet-wins``. A session-wins key with no session-scoped value could
    never be won by anything, so a violating entry is refused at IMPORT
    TIME rather than shipping a toggle whose precedence is undecidable.
    Asserted over the registry itself so a future key cannot reintroduce
    the mistake this invariant exists to stop (see module docstring
    "FLEET-ONLY KEYS")."""
    for key, entry in registry.items():
        if entry.session_pair is None and entry.precedence != "fleet-wins":
            raise ValueError(
                f"MODE_KEYS[{key!r}]: session_pair=None requires "
                f"precedence='fleet-wins', got {entry.precedence!r}"
            )


_validate_registry(MODE_KEYS)


def resolve_mode(key: str, session_id: str) -> object:
    """Resolve ``key`` for ``session_id`` and return the key's declared
    ``value_type``.

    An unknown ``key`` raises ``KeyError`` HERE, at the call site — a
    programming error, distinct from an unrecognised key sitting inside the
    fleet record itself, which ``fleet_mode.read_fleet_mode()`` already
    absorbs into its empty-mapping degradation before this function ever
    sees it (see module docstring "TWO PATHS, VISIBLY DISTINCT").

    Precedence, per entry:
      - ``session-wins``: the session's own value governs unconditionally.
        The fleet record is never consulted for this key — a fleet value
        cannot override an absent session sentinel (see module docstring;
        this is the ``autonomous`` behaviour: a shared-tree cost a session
        cannot consent to on third parties' behalf).
      - ``fleet-wins``: a validated fleet value governs if present; else the
        session's own value (where the key declares one) governs; else the
        key's ``default``.

    Never enumerates sessions — only ever reads the one ``session_id`` it
    was given.
    """
    if key not in MODE_KEYS:
        raise KeyError(f"unknown mode key: {key!r}")
    entry = MODE_KEYS[key]

    if entry.precedence == "session-wins":
        if entry.session_pair is None:
            # Unreachable given _validate_registry, but keeps this branch
            # honest rather than silently falling through to a fleet read.
            return entry.default
        return entry.session_pair(session_id)

    # fleet-wins
    fleet_map = read_fleet_mode()
    fleet_value = _validate_value(fleet_map.get(key), entry.value_type)
    if fleet_value is not None:
        return fleet_value
    if entry.session_pair is not None:
        return entry.session_pair(session_id)
    return entry.default
