"""research_groups: the per-group fan-out contract of the research door.

A group is one notebooklm target of a sizing's research block; sizing order is priority order.
This module names the op/CLI/close/reply keys, lists the groups, refuses shapes that cannot be
split, and renders a group's run line.

Negative-spec: writes nothing, loads no manifest, reads no sizing file, spawns no process.
No key here is the bare string "groups": `fanout.groups` (fanout_cost) already means
agent-batching groups.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from coordinator_core.ops import _research_contract as rc
from coordinator_core.ops.dispatch_emit import research_emit

__all__ = [
    "PER_GROUP_PARAM",
    "GROUPS_PARAM",
    "GROUPS_REPLY_KEY",
    "GROUP_RUNS_CLOSE_PARAM",
    "GroupSpec",
    "groups_for",
    "per_group_refusals",
    "run_line",
]

PER_GROUP_PARAM = "per_group"
GROUPS_PARAM = "group_refs"
GROUPS_REPLY_KEY = "research_groups"
GROUP_RUNS_CLOSE_PARAM = "group_runs"

_NOTEBOOKLM = "notebooklm"
_SPLITTABLE_PIPELINES = frozenset({rc.PREFLIGHT_PIPELINE, _NOTEBOOKLM})
_GROUP_SLUG_CAP = research_emit.MAX_SLUG_LEN // 2


@dataclass(frozen=True)
class GroupSpec:
    """One group run: `group` is the slug of `ref`; `priority` is 1-based target order in the full sizing."""

    group: str
    ref: str
    priority: int
    topic_slug: str


def _notebook_refs(research: Mapping) -> list[str]:
    return [
        str(t["ref"])
        for t in research.get("targets") or ()
        if isinstance(t, Mapping) and t.get("source") == _NOTEBOOKLM and t.get("ref")
    ]


def _group_slug(ref: str) -> str:
    return research_emit._slug(ref)[:_GROUP_SLUG_CAP].strip("-")


def _topic_for(topic: str, group: str) -> str:
    """`<topic>-<group>` within MAX_SLUG_LEN; the topic prefix gives way so the group always survives."""
    budget = research_emit.MAX_SLUG_LEN - len(group) - 1
    prefix = research_emit._slug(topic)[:max(budget, 0)].strip("-")
    return f"{prefix}-{group}" if prefix else group


def groups_for(research: Mapping, topic: str, selected: Sequence[str] = ()) -> list[GroupSpec]:
    """Notebooklm targets of `research` in sizing order; `selected` filters by ref and keeps full-list priorities."""
    wanted = set(selected)
    specs = []
    for priority, ref in enumerate(_notebook_refs(research), start=1):
        if wanted and ref not in wanted:
            continue
        group = _group_slug(ref)
        specs.append(GroupSpec(group=group, ref=ref, priority=priority, topic_slug=_topic_for(topic, group)))
    return specs


def per_group_refusals(shape: Mapping, research: Mapping, selected: Sequence[str] = ()) -> list[str]:
    """Every reason `research` cannot be emitted per group; empty when it can."""
    reasons: list[str] = []
    refs = _notebook_refs(research)
    if len(refs) < 2:
        reasons.append(f"{PER_GROUP_PARAM} needs at least two notebooklm targets; the sizing has {len(refs)}")
    other = [p for p in shape.get("pipelines") or () if p not in _SPLITTABLE_PIPELINES]
    if other:
        reasons.append(f"{PER_GROUP_PARAM} splits only the notebooklm preflight and notebooklm pipelines; shape also has {', '.join(other)}")
    missing = [r for r in selected if r not in refs]
    if missing:
        reasons.append(f"{GROUPS_PARAM} names refs the sizing lacks: {', '.join(missing)}")
    seen: dict[str, str] = {}
    for ref in refs:
        slug = _group_slug(ref)
        if not slug:
            reasons.append(f"notebooklm target {ref!r} has no slug characters; rename it")
            continue
        if slug in seen and seen[slug] != ref:
            reasons.append(f"notebooklm targets {seen[slug]!r} and {ref!r} slug alike ({slug!r}); rename one")
        seen.setdefault(slug, ref)
    return reasons


def run_line(entry: Mapping) -> str:
    """The one-line invocation summary for a reply entry: priority, group, run id, script path."""
    return f"[{entry['priority']}] {entry['group']} run_id={entry['run_id']} script={entry['path']}"
