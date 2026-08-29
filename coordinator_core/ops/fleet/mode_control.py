"""
coordinator_core.ops.fleet.mode_control -- "fleet.mode_set" / "fleet.mode_show"
ops: the human-invoked half of the fleet-scoped settings plane.

Purpose: `set <key> <value>` and `show` over the C1 fleet record
(`coordinator_core.session.fleet_mode`), landing in the EXISTING
`coordinator_core/ops/fleet/` namespace alongside `archive_plans` /
`memo_send` / `record_history` / `sweep_status` -- same `@register_op`
placement, same thin-handler shape, no new op namespace, no `.cmd`
forwarder (DR-347: an interpreter start ahead of warmth is break-class;
the door is the native binary already published).

WHY THE KEY REGISTRY IS DEFINED HERE, LOCALLY, RATHER THAN IMPORTED. This
chunk's `depends_on` names ONLY C1 (`fleet_mode.py`'s record layer); C2
(`coordinator_core/session/mode_resolution.py`, the behavioural
`MODE_KEYS` registry consumed by the hooks at resolve time) is a sibling
chunk this one does not depend on and whose file this chunk's scope does
not include. `_KNOWN_KEYS` below is therefore a CLI-validation registry,
not the resolution registry -- it exists so a human's typo is caught here,
cheaply, at the door, rather than silently absorbed by C1's read-side
fail-open (`read_fleet_mode()` treats an unrecognized key as ordinary
degradation input, by design -- see that module's own docstring). It
intentionally mirrors C2's planned two keys (`autonomous`, session-wins;
`compaction_warnings`, fleet-wins, `session_pair: None`) by NAME and
PRECEDENCE, because both describe the same fleet record; it does not, and
must not, import from `mode_resolution` (out of this chunk's scope, and
not guaranteed to exist yet).

VALIDATION IS THE FLOOR HERE, NOT A SUBSTITUTE FOR C1's DEGRADATION PATH.
`set` rejects an unknown key by name (listing the known ones) and rejects
a value that does not match the key's declared `value_type` -- the CLI is
where a human's typo is cheapest to catch. C1's fail-open empty-mapping
degradation exists for a FORGED or CORRUPTED file reached by some other
path, never as a substitute for input validation on the one write path
this op is.

`show` MUST BE SELF-EXPLAINING -- LOAD-BEARING, NOT A NICETY. Every entry
names its own precedence rule and states which scope currently wins,
because a session reading this file (indirectly, through the resolver) has
to be able to explain its own behaviour and decline coherently on its own
evidence -- never on a relayed claim. The design's other half is exactly
the failure this plan's Problem section records: a relay that was
confident and WRONG fourteen times in one day, and every one of those was
caught by a session opening the artifact itself, not by the relayer. For
`compaction_warnings` -- a VARIANT SELECTOR, never an off switch (PM
ruling 2026-08-29, "compaction warnings are important to us") -- `show`
additionally names the variant that will fire and states explicitly that
no value of this key suppresses the advisory. A human reading `show` must
not come away believing they hold an off switch that does not exist.

Spec backlink: state/dispatch-briefs/2026-08-28-the-fleet-gets-one-file-and-the-floor-moves-to-the-reader/C4.md,
chunk C4.

Negative-spec:
    - Sends NO message to any session, and enumerates NO sessions. This
      module writes (via C1's `write_fleet_mode`) and reads (via C1's
      `read_fleet_mode`) a single file; that is the whole of what either
      op does. No session registry lookup, no peer address resolution, no
      messaging surface of any kind is imported or called.
    - Does NOT add a `.cmd` forwarder or a second op namespace -- both ops
      register into the existing `coordinator_core.ops.fleet` package the
      same way `record_history`/`sweep_status` do.
    - Does NOT spawn a subprocess or shell out for any reason -- `set` and
      `show` are pure Python over C1's record layer.
    - Does NOT read or write `coordinator_core/session/mode_resolution.py`
      or import from it -- out of this chunk's file scope (see module
      docstring "WHY THE KEY REGISTRY IS DEFINED HERE, LOCALLY" above).
    - Does NOT coerce an invalid value to a default -- an unknown key or a
      value failing its declared `value_type` raises `ValueError` naming
      the problem, never a silent best-effort write.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.session.fleet_mode import read_fleet_mode, write_fleet_mode

# CLI-validation registry -- see module docstring "WHY THE KEY REGISTRY IS
# DEFINED HERE, LOCALLY" for why this mirrors, rather than imports, C2's
# planned MODE_KEYS shape. Each entry:
#   value_type   -- "bool" or "enum"
#   enum_values  -- tuple of accepted string tokens, only for "enum"
#   precedence   -- "fleet-wins" or "session-wins"
#   session_pair -- True if a session-scoped sentinel exists to pair with
#                   (precedence is meaningful as a contest); None if this
#                   key is fleet-only (no session-scoped counterpart exists
#                   at all, so "fleet-wins" is the only coherent value --
#                   see C2's registry invariant this mirrors).
#   is_variant_selector -- True for a key that selects which advisory
#                   variant fires rather than an on/off toggle; `show`
#                   states explicitly that no value of such a key
#                   suppresses the advisory (see module docstring).
#   description  -- one line, surfaced verbatim by `show`.
_KNOWN_KEYS: dict = {
    "autonomous": {
        "value_type": "bool",
        "precedence": "session-wins",
        "session_pair": True,
        "is_variant_selector": False,
        "description": (
            "Autonomous-run posture. Session-wins: the cost of a wrong "
            "posture lands on the shared tree, not on the human, so only "
            "the session itself -- positioned to know whether its current "
            "work makes the posture unsafe -- can turn this on for itself. "
            "A fleet value never overrides an absent or opposing session "
            "sentinel."
        ),
    },
    "compaction_warnings": {
        "value_type": "enum",
        "enum_values": ("standard", "informational"),
        "precedence": "fleet-wins",
        "session_pair": None,
        "is_variant_selector": True,
        "description": (
            "Context-pressure advisory VARIANT selector -- fleet-only, no "
            "session-scoped counterpart exists. Fleet-wins: the cost of "
            "turning this off lands on the human who chose not to be "
            "told, so it is their call outright. Selects WHICH variant of "
            "the advisory fires; no value of this key suppresses the "
            "advisory itself -- see 'standard'/'informational' below."
        ),
    },
}

_BOOL_TRUE_TOKENS = frozenset({"on", "true"})
_BOOL_FALSE_TOKENS = frozenset({"off", "false"})


def known_keys() -> tuple:
    """Return the known key names, sorted -- used both by `set`'s
    unknown-key error message and by `show`'s enumeration order."""
    return tuple(sorted(_KNOWN_KEYS))


def _validate_value(key: str, value: str) -> object:
    """Validate `value` (always a str -- CLI input) against `key`'s
    declared `value_type`, returning the coerced value to persist.

    Raises `ValueError` naming the key, the offered value, and the
    accepted set on any mismatch -- never coerces to a default and never
    silently drops the value. See module docstring "VALIDATION IS THE
    FLOOR HERE".
    """
    key_def = _KNOWN_KEYS[key]
    value_type = key_def["value_type"]

    if value_type == "bool":
        lowered = value.strip().lower()
        if lowered in _BOOL_TRUE_TOKENS:
            return True
        if lowered in _BOOL_FALSE_TOKENS:
            return False
        raise ValueError(
            f"fleet.mode_set: key '{key}' takes a bool "
            f"(one of: on, off, true, false); got {value!r}"
        )

    if value_type == "enum":
        enum_values = key_def["enum_values"]
        if value not in enum_values:
            raise ValueError(
                f"fleet.mode_set: key '{key}' takes one of "
                f"{list(enum_values)}; got {value!r}"
            )
        return value

    raise AssertionError(f"unreachable: unknown value_type {value_type!r} for key {key!r}")


def set_fleet_mode_key(key: str, value: str) -> dict:
    """Validate and persist `key = value` into the fleet record.

    Unknown `key` raises `ValueError` naming the known keys. A `value` not
    matching the key's declared `value_type` raises `ValueError` naming
    the accepted set. On success, reads the CURRENT fleet record (so a
    `set` of one key never clobbers another key already on record),
    updates the one key, writes it back atomically via C1's
    `write_fleet_mode`, and returns the resulting full record.
    """
    if key not in _KNOWN_KEYS:
        raise ValueError(
            f"fleet.mode_set: unknown key {key!r}; known keys: {list(known_keys())}"
        )

    coerced = _validate_value(key, value)

    record = read_fleet_mode()
    if not isinstance(record, dict):
        record = {}
    record = dict(record)
    record[key] = coerced

    ok = write_fleet_mode(record)
    if not ok:
        raise OSError(f"fleet.mode_set: failed to persist key {key!r} (write_fleet_mode returned False)")

    return record


def show_fleet_mode() -> dict:
    """Render every known key, self-explaining: precedence rule, which
    scope currently wins, and (for a variant-selector key) the variant
    that will fire plus an explicit statement that no value suppresses
    the advisory. See module docstring "`show` MUST BE SELF-EXPLAINING".

    Returns:
        {
          "keys": [
            {
              "key": str,
              "precedence": "fleet-wins" | "session-wins",
              "wins": str,  # one-line statement of which scope wins
              "fleet_value": the current fleet-record value for this key,
                             or None if absent,
              "is_variant_selector": bool,
              "description": str,
              # present only when is_variant_selector is True:
              "variant_that_fires": str,     # the fleet_value, or the
                                              # declared default variant
                                              # when absent
              "suppressible": False,         # ALWAYS False for a variant
                                              # selector -- no value of it
                                              # suppresses the advisory
            },
            ...
          ],
        }
    """
    record = read_fleet_mode()
    if not isinstance(record, dict):
        record = {}

    entries = []
    for key in known_keys():
        key_def = _KNOWN_KEYS[key]
        fleet_value = record.get(key)
        precedence = key_def["precedence"]

        if precedence == "fleet-wins":
            wins = "fleet -- this key's value (if set) always applies"
        else:
            wins = (
                "session -- a session's own sentinel wins over any fleet "
                "value; the fleet value here never overrides it"
            )

        entry = {
            "key": key,
            "precedence": precedence,
            "wins": wins,
            "fleet_value": fleet_value,
            "is_variant_selector": key_def["is_variant_selector"],
            "description": key_def["description"],
        }

        if key_def["is_variant_selector"]:
            enum_values = key_def["enum_values"]
            default_variant = enum_values[0]
            entry["variant_that_fires"] = fleet_value if fleet_value in enum_values else default_variant
            entry["suppressible"] = False

        entries.append(entry)

    return {"keys": entries}


@register_op("fleet.mode_set")
def _fleet_mode_set(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "fleet.mode_set" handler -- writes one key into the fleet
    record. Scope "none" (fleet-generic, matching `fleet.record_history`
    and `fleet.archive_sweep_status`'s own scoping) -- `repo_root` is
    unused.

    Params: `key` (required), `value` (required, always a str).
    """
    key = params.get("key")
    value = params.get("value")
    if not key:
        raise ValueError(
            f"fleet.mode_set requires 'key'; known keys: {list(known_keys())}"
        )
    if value is None:
        raise ValueError("fleet.mode_set requires 'value'")
    return set_fleet_mode_key(key, str(value))


@register_op("fleet.mode_show")
def _fleet_mode_show(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "fleet.mode_show" handler -- read-only, renders every
    known key with its precedence rule and which scope currently wins.
    Scope "none", matching `fleet.mode_set`. `params` accepted (standard
    handler signature) but unused -- this op has no filter surface.
    """
    return show_fleet_mode()
