"""`semantic_shape_hash` ignores root-level `applies_to` (a routing locator,
not validation shape) but still sees a PROPERTY named `applies_to`."""

import json

from coordinator_core.frontmatter.schema_shape import semantic_shape_hash

BASE = {
    "type": "object",
    "x-schema-version": "1.0.0",
    "properties": {"alpha": {"type": "string"}},
}


def _copy():
    return json.loads(json.dumps(BASE))


def test_root_applies_to_does_not_move_the_hash():
    located = _copy()
    located["applies_to"] = "state/bug-backlog/*.yaml"
    assert semantic_shape_hash(located) == semantic_shape_hash(BASE)


def test_changed_root_applies_to_does_not_move_the_hash():
    a, b = _copy(), _copy()
    a["applies_to"] = "docs/a/*.md"
    b["applies_to"] = ["docs/b/*.md", "docs/c/*.md"]
    assert semantic_shape_hash(a) == semantic_shape_hash(b)


def test_property_named_applies_to_still_moves_the_hash():
    with_prop = _copy()
    with_prop["properties"]["applies_to"] = {"type": "string"}
    assert semantic_shape_hash(with_prop) != semantic_shape_hash(BASE)
