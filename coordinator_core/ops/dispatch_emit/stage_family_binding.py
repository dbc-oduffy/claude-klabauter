"""Reader for DoE's stage-family binding file, the opt-in third family source.

A run opts in by naming `BINDING_REF` as its sizing's `stage_family_binding`;
only then is the file read. The DoE root comes from `doe_drift.resolve_doe_clone`.
"""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple, Optional

import yaml

from coordinator_core.frontmatter.yaml_load import SAFE_LOADER
from coordinator_core.ops.dispatch_emit import stage_family
from coordinator_core.ops.emit import doe_drift

BINDING_REF = "coordinator-content-repo:coordinator/pipelines/stage-family-binding.yaml"
_BINDING_RELPATH = "coordinator/pipelines/stage-family-binding.yaml"
_REGISTRY_KEY = "engine.working_repos.content_root"


class BindingError(ValueError):
    """The binding file is unreadable, unresolvable or violates its contract."""


class Binding(NamedTuple):
    """`runner` and `fallback` echo validated constants; only `roles` has a consumer (`ask_stage.stage`)."""

    runner: str
    fallback: str
    roles: dict


def opted_in(sizing: Optional[dict]) -> bool:
    """True iff the sizing names `BINDING_REF`; any other non-empty value raises."""
    value = (sizing or {}).get("stage_family_binding")
    if value is None or value == "":
        return False
    if value != BINDING_REF:
        raise ValueError(
            f"stage_family_binding {value!r} is not the binding; expected {BINDING_REF}"
        )
    return True


def load(content_root: Optional[Path] = None) -> Binding:
    """Read and validate the binding under the DoE root (registry-resolved when None)."""
    if content_root is None:
        try:
            content_root = doe_drift.resolve_doe_clone()
        except doe_drift.DoeResolveError as exc:
            raise BindingError(
                f"cannot resolve the DoE root: registry key {_REGISTRY_KEY} unset "
                f"or its path absent ({exc})"
            ) from exc
    path = Path(content_root) / _BINDING_RELPATH
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=SAFE_LOADER)
    except (OSError, yaml.YAMLError) as exc:
        raise BindingError(f"cannot read binding {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BindingError(f"binding {path} is not a mapping")
    if data.get("schema_version") != 1:
        raise BindingError(
            f"binding schema_version {data.get('schema_version')!r} != 1"
        )
    runner = data.get("runner")
    if runner != stage_family.RUNNER_AGENT:
        raise BindingError(
            f"binding runner {runner!r} != {stage_family.RUNNER_AGENT}"
        )
    fallback = data.get("fallback")
    if fallback != stage_family.FALLBACK:
        raise BindingError(f"binding fallback {fallback!r} != {stage_family.FALLBACK}")
    roles = data.get("roles")
    if not isinstance(roles, dict) or not roles:
        raise BindingError("binding roles must be a non-empty mapping")
    for role, family in roles.items():
        if role not in stage_family.ROLES:
            raise BindingError(
                f"binding role {role!r} not one of {'|'.join(stage_family.ROLES)}"
            )
        if family not in stage_family.FAMILIES:
            raise BindingError(
                f"binding family {family!r} for role {role!r} not one of "
                f"{'|'.join(stage_family.FAMILIES)}"
            )
    return Binding(runner=runner, fallback=fallback, roles=dict(roles))
