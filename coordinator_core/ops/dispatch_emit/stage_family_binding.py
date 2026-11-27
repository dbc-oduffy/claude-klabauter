"""The sizing-driven stage-family binding: `from_sizing` builds a `Binding` from the sizing's `stage_families`."""
from __future__ import annotations

from typing import NamedTuple, Optional

from coordinator_core.ops.dispatch_emit import stage_family


class BindingError(ValueError):
    """The sizing's `stage_families` violates its contract."""


class Binding(NamedTuple):
    """`runner` and `fallback` echo validated constants; only `roles` has a consumer (`ask_stage.stage`)."""

    runner: str
    fallback: str
    roles: dict


def from_sizing(sizing: Optional[dict]) -> Optional[Binding]:
    """The `Binding` the sizing's `stage_families` (role -> family) names, or None when the EM chose none.

    Roles mapped to claude are dropped (claude is the absence of a choice); a map left empty is None.
    Raises `BindingError` for a role outside `stage_family.ROLES`, an unknown family, a non-mapping
    value, or a sizing still carrying the retired `stage_family_binding` key.
    """
    sizing = sizing or {}
    if sizing.get("stage_family_binding") not in (None, ""):
        raise BindingError(
            "sizing key stage_family_binding is retired; name the EM's choice as "
            "`stage_families: {role: family}`"
        )
    chosen = sizing.get("stage_families")
    if chosen is None or chosen == {}:
        return None
    if not isinstance(chosen, dict):
        raise BindingError("stage_families must be a mapping of role to family")
    roles: dict = {}
    for role, value in chosen.items():
        if role not in stage_family.ROLES:
            raise BindingError(
                f"stage_families role {role!r} not one of {'|'.join(stage_family.ROLES)}"
            )
        try:
            family = stage_family.resolve(value)
        except ValueError as exc:
            raise BindingError(f"stage_families role {role!r}: {exc}") from exc
        if family != stage_family.FALLBACK:
            roles[role] = family
    if not roles:
        return None
    return Binding(runner=stage_family.RUNNER_AGENT, fallback=stage_family.FALLBACK, roles=roles)
