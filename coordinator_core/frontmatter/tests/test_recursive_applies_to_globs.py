"""Month-shard paths resolve to the same engine schema as flat paths, and the
overlapping-glob precedence rule (fewer '*' wins; tie: longer glob wins) is pinned.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.frontmatter.schema_validate import load_schemas, match_schema

_SCHEMAS_DIR = Path(__file__).parent.parent / "schemas"

_PER_SCHEMA = [
    ("seam-check", "docs/plans/x.seam.yaml", "docs/plans/2026-11/x.seam.yaml"),
    ("plan", "docs/plans/x.md", "docs/plans/2026-11/x.md"),
    ("handoff", "state/handoffs/x.md", "state/handoffs/2026-11/x.md"),
    ("sizing-object", "state/sizings/x.yaml", "state/sizings/2026-11/x.yaml"),
    ("research-claim", "docs/research/x.claims.json", "docs/research/2026-11/x.claims.json"),
    ("research-synthesis", "docs/research/x.md", "docs/research/2026-11/x.md"),
]


@pytest.fixture(scope="module")
def schemas():
    return load_schemas(_SCHEMAS_DIR)


@pytest.mark.parametrize("name,flat,shard", _PER_SCHEMA)
def test_flat_and_shard_resolve_to_same_schema(schemas, name, flat, shard):
    for path in (flat, shard):
        matched = match_schema(path, None, schemas)
        assert matched is not None and matched["schemaName"] == name, path


@pytest.mark.parametrize(
    "path,expected",
    [
        ("docs/research/spikes/x/verdict.md", "spike-result"),
        ("archive/sizings/2026-08/x.yaml", "sizing-object"),
        ("docs/plans/x.seam.yaml", "seam-check"),
        ("docs/plans/2026-11/x.seam.yaml", "seam-check"),
    ],
)
def test_overlaps_resolve_by_precedence(schemas, path, expected):
    matched = match_schema(path, None, schemas)
    assert matched is not None and matched["schemaName"] == expected, path


def test_precedence_rule_on_synthetic_two_glob_overlap(tmp_path):
    def write(name: str, glob: str) -> None:
        (tmp_path / f"{name}.schema.json").write_text(
            '{"x-schema-name": "%s", "applies_to": "%s", "type": "object"}' % (name, glob),
            encoding="utf-8",
        )

    # Fewer wildcards beats more, regardless of glob length.
    write("narrow", "a/b/*.md")
    write("wide", "a/**/*.md")
    # Equal wildcard count: the longer glob wins.
    write("long", "c/**/*.seam.yaml")
    write("short", "c/**/*.yaml")
    loaded = load_schemas(tmp_path)
    assert match_schema("a/b/x.md", None, loaded)["schemaName"] == "narrow"
    assert match_schema("a/b/d/x.md", None, loaded)["schemaName"] == "wide"
    assert match_schema("c/d/x.seam.yaml", None, loaded)["schemaName"] == "long"
    assert match_schema("c/d/x.yaml", None, loaded)["schemaName"] == "short"
