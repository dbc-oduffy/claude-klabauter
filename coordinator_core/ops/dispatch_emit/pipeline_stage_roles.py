"""Compose-time map from research-pipeline stages to stage-family binding roles.

Pure: no I/O. A stage with no entry in `ROLE_BY_STAGE`, or whose bound family is
claude, is absent from the result and stays on claude.
"""
from __future__ import annotations

from typing import Sequence

from coordinator_core.ops.dispatch_emit import stage_family, stage_family_binding

ROLE_BY_STAGE: dict[tuple[str, str], str] = {
    ("scouts", "scout"): "scout",
    ("web", "scout"): "scout",
    ("repo", "scout-haiku"): "scout",
    ("repo", "scout-sonnet"): "scout",
    ("web", "specialists"): "research",
    ("repo", "specialists"): "research",
}


def stage_families(
    segments: Sequence[tuple],
    binding: stage_family_binding.Binding,
) -> dict[tuple[str, str], str]:
    """Return `{(pipeline, stage_id): family}` for mapped stages bound to a non-claude family.

    `segments` are `(manifest, inputs, schedule)` tuples. Each family value passes
    through `stage_family.resolve`, which raises ValueError on an unknown family.
    """
    out: dict[tuple[str, str], str] = {}
    for segment in segments:
        manifest = segment[0]
        for stage in manifest.stages:
            key = (manifest.pipeline, stage.id)
            role = ROLE_BY_STAGE.get(key)
            if role is None:
                continue
            family = stage_family.resolve(binding.roles.get(role))
            if family != stage_family.FALLBACK:
                out[key] = family
    return out
