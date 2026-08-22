"""C3a re-vendor regression: a sprint-path roadmap OVERVIEW both validates
AND is returned by a real ``query-records --type roadmap`` call.

Purpose: `roadmap.schema.json` re-vendored at 1.4.0 widens `applies_to` from
`state/roadmap/*/OVERVIEW.md` to `state/roadmap/**/OVERVIEW.md` so a
sprint-scoped roadmap living under a nested directory
(`state/roadmap/<run-id>/sprint-<N>/OVERVIEW.md`) validates. Re-vendoring the
schema string alone is not enough: `records_query.py`'s `_TYPE_TO_GLOB['roadmap']`
answers "which files ARE roadmap records" independently of `applies_to`, and a
test asserting only that the schema string changed would miss a nested
roadmap that validates while staying invisible to `--type roadmap` — the
exact surface the sprint-split assemblers consume (empty result set, not an
error). This module asserts both halves together, never just one.

Spec backlink: cross-repo/inbox/2026-08-21-doe-claude-em-roadmap-glob-widened-
and-spine-homing-answered.md; state/dispatch-briefs/2026-08-21-engine-half-of-
the-roadmap-sprint-spine-split/C3a.md

Negative-spec: does NOT re-test the schema's field-level validation (title/
created/status/etc.) — that is `test_schema_validate.py`'s remit. This module
only pins the glob-widen's two-surface consequence.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

# Import guard — fires all @register_op(...) side-effects, including
# "records.query", before the handler is imported below.
import coordinator_core.ops  # noqa: F401 — populates _REGISTRY

from coordinator_core.frontmatter.schema_validate import (
    load_schemas,
    match_schema,
    parse_frontmatter,
    validate_frontmatter,
)
from coordinator_core.ops.records_query import _handler

# Two of this module's tests seed a real git repo so `--type roadmap`
# exercises the actual records-query collection path (glob walk +
# _load_record), not a stubbed reader — this module's purpose is the
# two-surface (schema + consumer glob) consequence, which only a real
# collection call can catch. Mirrors coordinator_core/ops/tests/test_records_query.py.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SCHEMAS_DIR = Path(__file__).parent.parent / "schemas"
_ROADMAP_SCHEMA = _SCHEMAS_DIR / "roadmap.schema.json"

_SPRINT_OVERVIEW = dedent(
    """\
    ---
    title: Sprint-scoped roadmap overview
    created: 2026-08-21
    status: active
    ---
    Body content.
    """
)


def _run(coro):
    if asyncio.iscoroutine(coro):
        return asyncio.run(coro)
    return coro


def _make_git_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(root), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "roadmap-schema-glob-test@claude-klabauter.test"],
        cwd=str(root),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Roadmap Schema Glob Test"],
        cwd=str(root),
        capture_output=True,
        check=True,
    )
    return (root / ".git").resolve()


def test_roadmap_schema_is_vendored_at_1_4_0_with_the_widened_glob():
    schemas = load_schemas(_SCHEMAS_DIR)
    schema = schemas["roadmap"]
    assert schema["x-schema-version"] == "1.4.0"
    assert schema["applies_to"] == "state/roadmap/**/OVERVIEW.md"


def test_sprint_path_overview_validates_against_the_vendored_schema():
    fm = parse_frontmatter(_SPRINT_OVERVIEW)["frontmatter"]
    errors = validate_frontmatter(fm, _ROADMAP_SCHEMA)
    assert errors == []


def test_sprint_path_overview_matches_roadmap_schema_via_match_schema():
    schemas = load_schemas(_SCHEMAS_DIR)
    fm = parse_frontmatter(_SPRINT_OVERVIEW)["frontmatter"]
    resolved = match_schema(
        "state/roadmap/claude-klabauter-strangler/sprint-1/OVERVIEW.md", fm, schemas
    )
    assert resolved is not None
    assert resolved.get("schemaName") == "roadmap"


def test_sprint_path_overview_is_returned_by_a_real_query_records_call(tmp_path: Path):
    """The half schema validation alone cannot catch: `--type roadmap` must
    actually collect a nested sprint-path overview, not just admit it as
    schema-valid."""
    worktree = tmp_path / "repo"
    git_dir = _make_git_repo(worktree)
    overview_dir = worktree / "state" / "roadmap" / "claude-klabauter-strangler" / "sprint-1"
    overview_dir.mkdir(parents=True)
    (overview_dir / "OVERVIEW.md").write_text(_SPRINT_OVERVIEW, encoding="utf-8")

    result = _run(
        _handler(params={"type": "roadmap", "format": "paths"}, repo_root=git_dir)
    )

    paths = [p for p in result["records"].split("\n") if p]
    assert any(
        Path(p).as_posix().endswith("claude-klabauter-strangler/sprint-1/OVERVIEW.md")
        for p in paths
    )


def test_top_level_overview_still_matches_the_widened_glob(tmp_path: Path):
    """DoE's memo flags the widen additionally admits a top-level
    `state/roadmap/OVERVIEW.md`, which nothing writes today but is a known,
    non-regressive consequence — pin it so the widen's full match set is
    intentional, not accidental."""
    worktree = tmp_path / "repo"
    git_dir = _make_git_repo(worktree)
    overview_dir = worktree / "state" / "roadmap"
    overview_dir.mkdir(parents=True)
    (overview_dir / "OVERVIEW.md").write_text(_SPRINT_OVERVIEW, encoding="utf-8")

    result = _run(
        _handler(params={"type": "roadmap", "format": "paths"}, repo_root=git_dir)
    )
    paths = [p for p in result["records"].split("\n") if p]
    assert any(p.endswith("OVERVIEW.md") for p in paths)
