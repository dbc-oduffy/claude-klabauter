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
  - ``job_mode`` is ``environment-wins``: the cost of this key being wrong
    lands on whoever consumes the mode a session was actually invoked as.
    The baton's Specification states the mode "is asserted by the
    environment that launched the session," so that explicit assertion must
    not be silently shadowed by a stale fleet-wide record — ``fleet_mode.
    read_fleet_mode()`` is fail-open by design and would otherwise surface
    nothing when it disagrees. This is a THIRD ``precedence`` value, not a
    second axis alongside the existing enum (see ``_validate_registry``) —
    the module's own thesis stays true of one axis, not two.

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
from typing import Callable, Dict, FrozenSet, Mapping, Optional, Union

from coordinator_core.session import autonomous_sentinel
from coordinator_core.session.fleet_mode import read_fleet_mode

#: The advisory variants ``_check_context_pressure_sync`` already
#: implements: ``"standard"`` is the default handoff-recommending wording;
#: ``"informational"`` is the autonomous-run wording that replaces the
#: recommendation rather than appending to it. See module docstring.
COMPACTION_WARNING_VARIANTS: FrozenSet[str] = frozenset({"standard", "informational"})

#: The wire variable name C1 (``coordinator_core.warm.env_forwarding``)
#: forwards through the door's ``FORWARDING_SET``. C1 declared it as a bare
#: string literal inside that module's ``FORWARDING_SET`` tuple rather than
#: an exported constant, so there is nothing importable to bind here -- this
#: is the ONE place the wire name is spelled fleet-wide.
#: ``coordinator_core.warm.env_forwarding`` imports it for its
#: ``FORWARDING_SET`` entry rather than repeating the literal -- two spellings
#: that "must stay byte-identical" fail SILENTLY when they drift, because a
#: forwarded-but-unread variable and an unset one are indistinguishable here:
#: both fall through to the conservative anchor with nothing raised. The
#: direction is fixed by the existing layering (``warm`` already imports
#: ``session``; the reverse would invert it), not by which module names it
#: first.
COORDINATOR_JOB_MODE = "COORDINATOR_JOB_MODE"

#: The three job-mode consumers the sizing object names. A frozenset enum,
#: same shape as ``COMPACTION_WARNING_VARIANTS``.
JOB_MODE_VALUES: FrozenSet[str] = frozenset({"blitz", "cron", "interactive"})

#: Conservative anchor for ``job_mode``: the only one of the three values
#: that assumes an operator is present to be surfaced to. Mirrors DoE's
#: ``_MOST_CAUTIOUS_POSTURE`` (renamed 2026-09-06 from ``_FAIL_OPEN_POSTURE``
#: -- that vocabulary is not reintroduced here; see
#: ``coordinator_core.conservatism`` for the fuller RAISE/FALL_BACK split).
#:
#: FAIL DIRECTION: FALL_BACK to ``"interactive"``. Declared explicitly here,
#: beside the anchor, per this chunk's own spec, so a later conservatism
#: primitive (``cloud-em-01``) can generalise this site without re-deriving
#: the reasoning:
#:   - Getting this WRONG toward ``"interactive"`` (the actual mode was
#:     ``blitz``/``cron``) costs a redundant surface -- an advisory or
#:     confirmation shown to nobody, wasted but harmless.
#:   - Getting this wrong the OTHER way (defaulting to ``blitz``/``cron``
#:     when the actual mode was ``interactive``) costs an unwitnessed
#:     autonomous act on an attended box -- unbounded and not survivable the
#:     way a redundant surface is.
#: That asymmetry is why ``"interactive"`` is the anchor and not, say, the
#: numerically- or alphabetically-first value.
_MOST_CAUTIOUS_JOB_MODE: str = "interactive"

ValueType = Union[type, FrozenSet[str]]


@dataclass(frozen=True)
class ModeKey:
    """One ``MODE_KEYS`` entry.

    ``session_pair``: a callable taking ``session_id`` and returning that
    session's own value in ``value_type``, or ``None`` for a fleet-only key
    (see module docstring "FLEET-ONLY KEYS"). Deliberately a callable, never
    a module reference — see module docstring.

    ``precedence``: ``"fleet-wins"``, ``"session-wins"``, or
    ``"environment-wins"``, decided by cost-incidence (see module
    docstring). ``"environment-wins"`` requires an ``environment_default``
    (see ``_validate_registry``).

    ``value_type``: ``bool`` or a ``frozenset`` of allowed string values
    (an enum). ``resolve_mode`` validates a fleet-supplied raw value against
    this at the registry boundary.

    ``default``: the value returned when neither side has an opinion —
    this is what makes an empty fleet mapping (``fleet_mode.
    read_fleet_mode()``'s own degradation) reproduce today's pre-plan
    behaviour exactly.

    ``environment_default``: an optional callable taking the caller's
    ``env`` (``Optional[Mapping[str, str]]``, the same value
    ``per_request_state`` already carries -- see ``resolve_mode``) and
    returning a value in ``value_type``, or ``None`` to abstain. Consulted
    AFTER both explicit sides and BEFORE ``default`` — so an operator who
    states a value always beats it, and it only ever speaks where nobody
    else has. A callable for the same reason ``session_pair`` is one: the
    fact it reads is resolved at call time, not frozen at import. It takes
    ``env`` as a PARAMETER, never an ambient read — mirroring
    ``env_locality.locality(env)``'s own contract — because the pool-broken
    ``isolated=False`` fallback leg in ``warm/server.py`` shares no
    ``os.environ`` borrow with the caller, and an ambient read there would
    silently answer with the daemon's environment instead of the caller's
    (or the caller's absence).

    It exists because some defaults are wrong in a way that is knowable from
    the environment rather than from the operator. The motivating case: the
    context-pressure advisory's `standard` variant recommends `/handoff`, and
    on a cloud box that remedy does not exist — no `/clear`, and passing a
    baton means a PR merge, a new session and a re-point. Making the operator
    remember to set that per box is exactly the failure the discharge test
    names; the environment already knows.
    """

    session_pair: Optional[Callable[[str], object]]
    precedence: str
    value_type: ValueType
    default: object
    environment_default: Optional[Callable[[Optional["Mapping[str, str]"]], object]] = None


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


def _compaction_default_for_environment(
    env: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """`informational` on a box that is not the developer's own; abstain
    otherwise.

    The `standard` variant tells the reader to run `/handoff`. That is the
    right call on an attended box and an unavailable one on a cloud box, where
    a session rides compaction by design. Abstaining (returning ``None``)
    rather than answering `standard` matters: it leaves the static default in
    charge wherever locality is uncertain, so a `suspect` reading never silently
    changes behaviour.

    ``env`` is threaded through to ``locality(env)`` rather than read
    ambiently. Before this widening, this call site passed nothing, so on
    the `isolated=False` fallback leg it silently read the daemon's
    environment instead of the caller's — a latent instance of the same
    defect ``env_locality`` itself was built to close (its own docstring:
    "``env`` IS A PARAMETER, NEVER AN AMBIENT READ"). Closed here for free as
    part of threading ``env`` through for ``job_mode``.
    """
    try:
        from coordinator_core.env_locality import locality

        got = locality(env)
        # Only a confident cloud reading moves the default. `suspect` abstains
        # by construction, and so does a low-confidence cloud call.
        if got.call == "cloud" and got.confidence in ("certain", "high"):
            return "informational"
    except Exception:  # pragma: no cover - resolution must never block a mode read
        pass
    return None


def _job_mode_from_environment(env: Optional[Mapping[str, str]]) -> Optional[str]:
    """This key's environment rung: the caller's own ``COORDINATOR_JOB_MODE``
    value, or ``None`` to abstain.

    ``env`` is a PARAMETER, never an ambient read (see ``ModeKey.
    environment_default`` docstring) — an absent ``env`` (no caller context
    threaded this far) abstains rather than falling back to ``os.environ``,
    which on the pool-broken ``isolated=False`` leg would be the daemon's
    environment, not the caller's (that leg mirrors nothing, so an ambient
    read would leak a daemon-inherited value, worst case a leaked ``blitz``,
    to a caller that never asserted one).

    An empty string and an unrecognised value are both returned as-is here,
    not filtered — ``resolve_mode``'s own ``_validate_value`` already
    degrades an out-of-enum string to ``None`` at the registry boundary
    (see module docstring "``resolve_mode`` RETURNS THE KEY'S DECLARED VALUE
    TYPE"); reimplementing that filter here would duplicate it.
    """
    if not env:
        return None
    raw = env.get(COORDINATOR_JOB_MODE)
    return raw if raw else None


MODE_KEYS: Dict[str, ModeKey] = {
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
        # Late-bound by name, NOT a direct reference: the entry is a frozen
        # dataclass built at import time, so a direct reference would freeze
        # this seam shut and make it unpatchable in tests. The lambda resolves
        # the name through module globals at call time instead.
        environment_default=lambda env: _compaction_default_for_environment(env),
    ),
    # COST-INCIDENCE: `job_mode` is `environment-wins`. The cost of this key
    # being wrong lands on whoever consumes the mode a session was actually
    # invoked as — the baton's Specification states the mode "is asserted by
    # the environment that launched the session," so a stale fleet-mode
    # record silently overriding that explicit assertion externalizes a cost
    # the fleet record was never in a position to bear (`read_fleet_mode` is
    # fail-open by design and would otherwise surface nothing). This is why
    # `job_mode` sits on `environment-wins`, distinct from `compaction_
    # warnings`' `fleet-wins`: that key's environment rung is a tiebreaker
    # over a static default (see its own module-docstring paragraph above),
    # while `job_mode`'s environment rung is the caller's own explicit
    # assertion and must not be shadowed by a stale fleet record.
    "job_mode": ModeKey(
        session_pair=None,
        precedence="environment-wins",
        value_type=JOB_MODE_VALUES,
        default=_MOST_CAUTIOUS_JOB_MODE,
        # Same late-bound-by-name reasoning as `compaction_warnings` above.
        environment_default=lambda env: _job_mode_from_environment(env),
    ),
}


_FLEET_ONLY_PRECEDENCES = ("fleet-wins", "environment-wins")


def _validate_registry(registry: dict) -> None:
    """Enforce two invariants over ``MODE_KEYS`` itself, at IMPORT TIME,
    rather than shipping a toggle whose precedence is undecidable:

      1. A key declaring ``session_pair=None`` MUST declare ``fleet-wins``
         OR ``environment-wins``. A session-wins key with no session-scoped
         value could never be won by anything (see module docstring
         "FLEET-ONLY KEYS"). ``environment-wins`` joined this set alongside
         ``fleet-wins`` when ``job_mode`` was added: it is likewise a valid
         resolution path for a key with no session-scoped value.

      2. A key declaring ``environment-wins`` MUST declare an
         ``environment_default`` — the precedence value is meaningless
         without an environment rung to promote.
    """
    for key, entry in registry.items():
        if entry.session_pair is None and entry.precedence not in _FLEET_ONLY_PRECEDENCES:
            raise ValueError(
                f"MODE_KEYS[{key!r}]: session_pair=None requires precedence "
                f"in {_FLEET_ONLY_PRECEDENCES!r}, got {entry.precedence!r}"
            )
        if entry.precedence == "environment-wins" and entry.environment_default is None:
            raise ValueError(
                f"MODE_KEYS[{key!r}]: precedence='environment-wins' requires "
                f"an environment_default"
            )


_validate_registry(MODE_KEYS)


def resolve_mode(
    key: str,
    session_id: str,
    env: Optional[Mapping[str, str]] = None,
) -> object:
    """Resolve ``key`` for ``session_id`` and return the key's declared
    ``value_type``.

    ``env`` is the caller's environment (the same value ``per_request_state``
    already carries), threaded to each entry's ``environment_default`` — it
    is a PARAMETER, never an ambient read, mirroring ``env_locality.
    locality(env)``'s own contract (see ``ModeKey.environment_default``
    docstring). Defaults to ``None`` so existing call sites that predate the
    environment rung are unaffected: none of their keys declare
    ``environment-wins``, so an absent ``env`` there is inert.

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
      - ``environment-wins``: a validated environment value governs if
        present; else a validated fleet value governs if present; else the
        session's own value (where the key declares one) governs; else the
        key's ``default``. The environment rung is consulted FIRST here,
        unlike ``fleet-wins`` below, because this precedence exists
        precisely so a stale fleet record cannot silently override an
        environment's explicit assertion (see ``job_mode``'s registry
        comment).
      - ``fleet-wins``: a validated fleet value governs if present; else the
        session's own value (where the key declares one) governs; else the
        key's ``environment_default`` (where it declares one and that callable
        does not abstain); else the key's ``default``.

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

    def _resolved_environment_value() -> Optional[object]:
        if entry.environment_default is None:
            return None
        # Defensive: resolving the environment must never block a mode read.
        # The shipped callables swallow their own failures, but the registry
        # is an extension point and a future entry's callable is not this
        # module's to trust.
        try:
            return _validate_value(entry.environment_default(env), entry.value_type)
        except Exception:
            return None

    if entry.precedence == "environment-wins":
        env_value = _resolved_environment_value()
        if env_value is not None:
            return env_value
        fleet_map = read_fleet_mode()
        fleet_value = _validate_value(fleet_map.get(key), entry.value_type)
        if fleet_value is not None:
            return fleet_value
        if entry.session_pair is not None:
            return entry.session_pair(session_id)
        return entry.default

    # fleet-wins
    fleet_map = read_fleet_mode()
    fleet_value = _validate_value(fleet_map.get(key), entry.value_type)
    if fleet_value is not None:
        return fleet_value
    if entry.session_pair is not None:
        return entry.session_pair(session_id)
    env_value = _resolved_environment_value()
    if env_value is not None:
        return env_value
    return entry.default
