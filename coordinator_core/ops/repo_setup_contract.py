"""
coordinator_core.ops.repo_setup_contract — answers contract and verdict schema for ``repo.setup``.

Purpose: the one place that defines what a caller may pass as ``answers`` and the JSON shape the
op returns, so the op, its tests and the delivery note read the same definitions.

Negative-spec:
  - Does NOT touch the filesystem, git or any process; pure validation and serialisation.
  - Does NOT decide which answers are required; that belongs to the op.
  - Does NOT raise on bad input; every defect becomes a needs_attention item naming the key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

PROJECT_TYPES = ("game-dev", "web-dev", "data-science", "general")

# Canonical answer keys and their types: "str", "bool", "str_list", "project_type".
ANSWERS_KEYS: dict = {
    "project_name": "str",
    "project_type": "project_type",
    "project_subtypes": "str_list",
    "cross_platform": "bool",
    "workstreams": "str_list",
    "register_repo": "bool",
    "description": "str",
}

# Spellings the DoE repo-setup skill accepts, folded onto the canonical keys.
ANSWER_ALIASES: dict = {
    "name": "project_name",
    "p2.ask-project-name": "project_name",
    "p2.ask-project-type": "project_type",
    "p2.ask-workstreams": "workstreams",
}

# Lane step_ids with no first-class meaning here; accepted and passed through unchecked.
PASSTHROUGH_STEP_IDS = frozenset(
    {
        "p1.repo-classification-ask",
        "p15.ratification-ask",
        "p15.propose-workstreams",
        "p3d5.hand-section-prompt",
        "p3f.untrack-scratch-offer",
        "p3f5.custom-hook-skip",
        "p3l.curation-prompt",
        "p3x.memo-destination-offer",
        "tw.windows-console-offer",
    }
)

_ITEM_SCHEMA = {
    "type": "object",
    "required": ["step", "detail"],
    "properties": {
        "step": {"type": "string"},
        "path": {"type": "string"},
        "key": {"type": "string"},
        "detail": {"type": "string"},
    },
    "additionalProperties": False,
}

_ITEM_ARRAY = {"type": "array", "items": _ITEM_SCHEMA}

VERDICT_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "repo.setup verdict",
    "type": "object",
    "required": ["created", "already_existed", "open", "needs_attention"],
    "properties": {
        "created": _ITEM_ARRAY,
        "already_existed": _ITEM_ARRAY,
        "open": _ITEM_ARRAY,
        "needs_attention": _ITEM_ARRAY,
    },
    "additionalProperties": False,
}


def make_item(step: str, detail: str, path: str | None = None, key: str | None = None) -> dict:
    """Build one verdict item ``{step, detail[, path][, key]}``."""
    item: dict = {"step": step, "detail": detail}
    if path is not None:
        item["path"] = path
    if key is not None:
        item["key"] = key
    return item


@dataclass
class Verdict:
    """The ``repo.setup`` reply: four item lists, serialised by ``to_dict``."""

    created: list = field(default_factory=list)
    already_existed: list = field(default_factory=list)
    open: list = field(default_factory=list)
    needs_attention: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "created": list(self.created),
            "already_existed": list(self.already_existed),
            "open": list(self.open),
            "needs_attention": list(self.needs_attention),
        }


def _attention(key: str, detail: str) -> dict:
    return make_item("answers", detail, key=key)


def _check_value(kind: str, value: Any) -> str | None:
    """Return a defect description, or None when ``value`` fits ``kind``."""
    if kind == "str":
        if not isinstance(value, str) or not value.strip():
            return "expected a non-empty string"
    elif kind == "bool":
        if not isinstance(value, bool):
            return "expected a boolean"
    elif kind == "str_list":
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            return "expected a list of non-empty strings"
    elif kind == "project_type":
        if value not in PROJECT_TYPES:
            return "expected one of " + ", ".join(PROJECT_TYPES)
    return None


def validate_answers(obj_or_json_str: Any) -> tuple:
    """Parse and schema-check an answers payload.

    Accepts a dict or a JSON string holding an object; ``None`` means no answers. Returns
    ``(answers, items)``: ``answers`` is the canonical-key dict (aliases folded, passthrough
    step_ids kept) and is ``{}`` whenever ``items`` is non-empty; ``items`` holds one
    ``{step: "answers", key, detail}`` needs_attention item per unknown, mistyped or
    conflicting key (key ``"$"`` names an unparseable or non-object payload).
    """
    if obj_or_json_str is None:
        return {}, []
    obj = obj_or_json_str
    if isinstance(obj, (str, bytes)):
        try:
            obj = json.loads(obj)
        except ValueError as exc:
            return {}, [_attention("$", f"answers is not valid JSON: {exc}")]
    if not isinstance(obj, dict):
        return {}, [_attention("$", "answers must be a JSON object")]

    answers: dict = {}
    items: list = []
    for raw_key, value in obj.items():
        key = ANSWER_ALIASES.get(raw_key, raw_key)
        if key in PASSTHROUGH_STEP_IDS:
            answers[key] = value
            continue
        kind = ANSWERS_KEYS.get(key)
        if kind is None:
            items.append(_attention(str(raw_key), "unknown answers key"))
            continue
        defect = _check_value(kind, value)
        if defect is not None:
            items.append(_attention(str(raw_key), defect))
            continue
        if key in answers and answers[key] != value:
            items.append(_attention(str(raw_key), f"conflicts with another spelling of {key}"))
            continue
        answers[key] = value
    if items:
        return {}, items
    return answers, []
