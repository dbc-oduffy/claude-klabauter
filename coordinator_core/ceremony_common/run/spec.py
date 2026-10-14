"""CeremonySpec: the one declaration a ceremony makes to the shared runner, and the spec registry."""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence


@dataclass(frozen=True)
class CeremonySpec:
    """Everything the runner needs to know about one ceremony.

    `name` is the digest's `ceremony` value. `marker_path` is None for a ceremony with no
    once-per-day rule. `envelope_step` is the directive id whose stdout is the brief envelope.
    `stage_of(directive_id)` returns the stage and raises on an unknown id. `rules` maps rule
    id to callable; its keys are the ceremony's rule ids and no second list is kept.
    `spec_points(envelope, flags, ledger_rows)` returns the judgment points the spec makes
    itself; `agent_groups(envelope, ledger_rows, resolution)` returns digest groups;
    `finish_steps(decisions, ledger_rows, *, run)` returns the steps a decision opens, `run`
    being the lock holder's record. `render` is the spec's receipt renderer, or None for
    `receipt.render`. `chain` is an argv or None. `launch_flags` is the closed set of flags
    `launch` accepts.
    """

    name: str
    dispatch_key: str
    run_dir_name: str
    template: str
    marker_path: Callable[..., Any] | None
    launch_stages: tuple[str, ...]
    finish_stages: tuple[str, ...]
    front_door: Any
    envelope_step: str
    stage_of: Callable[[str], str]
    residue: Sequence[Any]
    rules: Mapping[str, Callable[..., Any]]
    spec_points: Callable[..., Sequence[Any]]
    agent_groups: Callable[..., list[dict[str, Any]]]
    finish_steps: Callable[..., Sequence[Any]]
    receipt_sections: Sequence[str]
    render: Callable[..., str] | None
    costs: Any
    chain: Sequence[str] | None
    launch_flags: frozenset[str]


class SpecUnavailable(LookupError):
    """The named ceremony has no spec module, or the name is not registered."""


CEREMONY_SPECS: Mapping[str, str] = {
    "workday_start": "coordinator_core.workday_start.run_spec:SPEC",
    "workday_complete": "coordinator_core.workday_complete.run_spec:SPEC",
    "workweek_start": "coordinator_core.workweek_start.run_spec:SPEC",
}


def load_spec(name: str) -> CeremonySpec:
    """Import and return the registered `SPEC`; raises SpecUnavailable with a one-line message."""
    path = CEREMONY_SPECS.get(name)
    if path is None:
        raise SpecUnavailable(f"unknown ceremony {name!r}; known: {', '.join(CEREMONY_SPECS)}")
    module_name, _, attr = path.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if not (exc.name and module_name.startswith(exc.name)):
            raise
        raise SpecUnavailable(f"ceremony {name!r} has no spec module {module_name}") from exc
    return getattr(module, attr)
