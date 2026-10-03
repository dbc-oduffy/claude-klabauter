"""Caller-side inputs of the dispatch.emit pipeline route: subject objects and named lists.

`subjects_from_value` turns a subjects list or a research spec (DoE `spec-format.md`: `subjects`
plus `topics`) into the string-or-object subjects the route takes; `read_structured_file` reads a
JSON or YAML file; `normalize_lists` coerces raw list values to each declared list kind. Reads only
the files it is handed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

import yaml

from coordinator_core.ops.dispatch_emit.pipeline_contract import PipelineEmitRefused

__all__ = ["STRUCTURED_SUFFIXES", "read_structured_file", "subjects_from_value", "normalize_lists"]

STRUCTURED_SUFFIXES = (".json", ".yaml", ".yml")


def read_structured_file(path: Path) -> object:
    """Parse a JSON or YAML file (JSON is YAML); refuses an unreadable or invalid file."""
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PipelineEmitRefused([f"{path.as_posix()}: unreadable as JSON or YAML: {exc}"]) from exc


def _verifiers(topics: object) -> list[dict]:
    if not isinstance(topics, list) or not topics:
        raise PipelineEmitRefused(["spec topics must be a non-empty list"])
    out = []
    for index, topic in enumerate(topics):
        if not isinstance(topic, dict) or not isinstance(topic.get("id"), str) or not isinstance(topic.get("name"), str):
            raise PipelineEmitRefused([f"spec topics[{index}] needs a string id and a string name"])
        out.append({"role": f"verifier-{topic['id']}", "topic": topic["id"], "name": topic["name"]})
    return out


def _spec_subjects(spec: Mapping, base: Path | None) -> list:
    block = spec.get("subjects")
    key_field = "subject"
    if isinstance(block, Mapping):
        key_field = block.get("key_field") or "subject"
        block = block.get("source")
        if isinstance(block, str):
            if block == "inline":
                raise PipelineEmitRefused(["spec subjects.source 'inline' needs the list under subjects.source"])
            path = Path(block)
            path = path if path.is_absolute() or base is None else base / path
            block = read_structured_file(path)
            if isinstance(block, Mapping):
                block = block.get("subjects")
    if not isinstance(block, list) or not block:
        raise PipelineEmitRefused(["spec carries no subjects list (subjects, or subjects.source)"])
    out: list = []
    for index, entry in enumerate(block):
        if isinstance(entry, Mapping):
            key = entry.get("subject", entry.get(key_field))
            if not isinstance(key, str) or not key.strip():
                raise PipelineEmitRefused([f"spec subjects[{index}] has no string {key_field!r} key"])
            out.append({**entry, "subject": key})
        else:
            out.append(entry)
    return out


def subjects_from_value(value: object, *, base: Path | None = None) -> list:
    """Subjects as the route takes them: a list passes through; a spec mapping yields its subjects.

    A spec with `topics` gives every subject without `verifiers` one verifier per topic
    (`{role: verifier-<id>, topic: <id>, name}`); a bare-string subject becomes an object then.
    `base` resolves a relative `subjects.source`.
    """
    if isinstance(value, list):
        return value
    if not isinstance(value, Mapping):
        raise PipelineEmitRefused(["subjects must be a list or a spec object with a subjects list"])
    subjects = _spec_subjects(value, base)
    if "topics" not in value:
        return subjects
    verifiers = _verifiers(value["topics"])
    return [
        {**(s if isinstance(s, dict) else {"subject": s}), **({} if isinstance(s, dict) and "verifiers" in s else {"verifiers": verifiers})}
        for s in subjects
    ]


def normalize_lists(kinds: Mapping[str, str], raw: Mapping[str, object]) -> dict[str, tuple]:
    """Coerce each raw list to its declared kind: a roster string `slug=agent_type` becomes an object.

    A name no manifest list declares passes through untouched; `validate` refuses it.
    """
    out: dict[str, tuple] = {}
    for name, value in raw.items():
        if not isinstance(value, (list, tuple)):
            raise PipelineEmitRefused([f"list {name!r} must be a list"])
        if kinds.get(name) == "roster":
            items = []
            for entry in value:
                if isinstance(entry, str):
                    slug, sep, agent_type = entry.partition("=")
                    entry = {"slug": slug.strip(), "agent_type": agent_type.strip()} if sep else entry
                items.append(entry)
            value = items
        out[name] = tuple(value)
    return out
