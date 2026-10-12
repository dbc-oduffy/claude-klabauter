"""The vendored sizing-object and handoff schemas sit at the research-by-value-class versions; byte parity with DoE is schema_drift_watch's job via vendored-from.json."""

from __future__ import annotations

import json
import re
from pathlib import Path

from coordinator_core.frontmatter.schema_validate import validate_frontmatter

_SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"
_SIZING = _SCHEMAS / "sizing-object.schema.json"
_HANDOFF = _SCHEMAS / "handoff.schema.json"
_PLAN_TASKS = _SCHEMAS / "plan-tasks.schema.json"


def _version(path: Path) -> str:
    return json.loads(path.read_text(encoding="utf-8"))["x-schema-version"]


def _sizing(**overrides) -> dict:
    fm = {
        "schema": "sizing-object",
        "intent": "Example PM ask, verbatim.",
        "estimate": {"tshirt": "M", "provisional": True},
        "route": "plan",
        "detents": [],
        "fork": None,
        "xl_exit": None,
        "status": "routed",
        "premise": {"provenance": "unrecorded"},
    }
    fm.update(overrides)
    return fm


def _research_errors(research: dict) -> list[dict]:
    errors = validate_frontmatter(_sizing(research=research), _SIZING)
    return [e for e in errors if e["field"].startswith("research")]


def test_vendored_versions():
    # Re-pinned 1.36.0 -> 1.37.0: DoE c01e16d913 adds `unrelated_turns` and the `visual` surface.
    assert _version(_SIZING) == "1.37.0"
    assert _version(_HANDOFF) == "11.0.1"
    assert _version(_PLAN_TASKS) == "3.15.0"


def _find_key(node, key):
    if isinstance(node, dict):
        if key in node:
            yield node[key]
        for v in node.values():
            yield from _find_key(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from _find_key(v, key)


def test_register_rows_shape():
    spec = json.loads(_PLAN_TASKS.read_text(encoding="utf-8"))["properties"]["register_rows"]
    assert spec["minItems"] == 1 and spec["uniqueItems"] is True
    pattern = re.compile(spec["items"]["pattern"])
    assert pattern.match("R1.a-b_c")
    assert not pattern.match("../x") and not pattern.match("")


def test_sizing_unrelated_turns_and_visual_surface():
    schema = json.loads(_SIZING.read_text(encoding="utf-8"))
    (turns,) = list(_find_key(schema, "unrelated_turns"))
    assert turns["items"]["additionalProperties"] is False
    assert turns["items"]["required"] == ["turn", "reason"]
    assert turns["items"]["properties"]["turn"]["minimum"] == 0
    assert any("visual" in e for e in _find_key(schema, "enum") if isinstance(e, list))


def test_research_block_accepts_repo_deep_target():
    research = {"value_class": "deep", "sources": ["repo"], "targets": [{"source": "repo", "ref": "claude-klabauter"}]}
    assert _research_errors(research) == []


def test_research_block_rejects_unknown_value_class():
    assert _research_errors({"value_class": "bogus"})
