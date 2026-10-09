"""The vendored sizing-object and handoff schemas sit at the research-by-value-class versions; byte parity with DoE is schema_drift_watch's job via vendored-from.json."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.frontmatter.schema_validate import validate_frontmatter

_SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"
_SIZING = _SCHEMAS / "sizing-object.schema.json"
_HANDOFF = _SCHEMAS / "handoff.schema.json"


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
    assert _version(_SIZING) == "1.34.0"
    assert _version(_HANDOFF) == "11.0.1"


def test_research_block_accepts_repo_deep_target():
    research = {"value_class": "deep", "sources": ["repo"], "targets": [{"source": "repo", "ref": "claude-klabauter"}]}
    assert _research_errors(research) == []


def test_research_block_rejects_unknown_value_class():
    assert _research_errors({"value_class": "bogus"})
