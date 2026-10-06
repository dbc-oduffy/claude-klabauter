"""ask_stage: briefs equal the plan route's prompts, the manifest round-trips, XS yields row X1."""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT, StageManifest
from coordinator_core.ops.dispatch_emit.ask_stage import AskStageError, _handler, stage
from coordinator_core.ops.dispatch_emit.commit_request import parse_marker
from coordinator_core.ops.dispatch_emit.spine_read import read_spine
from coordinator_core.ops.dispatch_emit.wave_map import build_waves

PLAN_REL = "docs/plans/2026-10-01-fixture.md"
SIZING_REL = "state/sizings/2026-10-01-xs-fixture.yaml"

PLAN = """---
deliverable_id: dlv-fixture-abc123
---
# Fixture plan

## Problem

Three rows exercise the stage.

## Tasks

```yaml plan-tasks
- id: C1
  title: first
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
- id: C2
  title: second
  change_kind: code-edit
  surface: pkg/b.py
  writes:
    - pkg/b.py
  depends_on:
    - chunk: C1
      gate_kind: output-consumption-runtime
- id: C3
  title: third
  change_kind: code-edit
  surface: pkg/c.py
  writes:
    - pkg/c.py
```
"""


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")
    return tmp_path


def test_three_row_plan_yields_briefs_equal_to_the_plan_routes_prompts(repo):
    m = stage(repo, run_id="r1", plan_rel=PLAN_REL)
    plan_path = repo / PLAN_REL
    text = plan_path.read_text(encoding="utf-8")
    ctx = emit.derive_plan_context(text, fallback_title=plan_path.stem, repo_root=repo.as_posix())
    rows = [r for w in build_waves(read_spine(plan_path)) for r in w]
    assert {r.id for r in m.rows} == {"C1", "C2", "C3"}
    for mr in m.rows:
        row = next(r for r in rows if r.id == mr.id)
        brief = (repo / mr.brief_path).read_text(encoding="utf-8")
        assert brief == emit._row_prompt(row, PLAN_REL, ctx)
    waves = {r.id: r.wave for r in m.rows}
    assert waves["C2"] > waves["C1"]
    assert {"pkg/a.py", "pkg/b.py", "pkg/c.py"} <= set(m.review_declared_paths)


def test_manifest_round_trips_and_request_names_every_row(repo):
    m = stage(repo, run_id="r1", plan_rel=PLAN_REL)
    on_disk = json.loads((repo / m.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert StageManifest.from_json(on_disk) == m
    assert m.run_dir == f"{RUN_DIR_ROOT}/r1"
    req = parse_marker((repo / m.marker_path).read_text(encoding="utf-8"))
    assert {c.id for c in req.chunks} == {"C1", "C2", "C3"}
    assert req.deliverable_id == "dlv-fixture-abc123"
    assert req.plan_path == PLAN_REL


def test_xs_sizing_yields_one_row_x1(repo):
    doc = {
        "intent": "Fix the thing",
        "exit_criterion": {"statement": "it works"},
        "deliverable_id": "dlv-xs-abc123",
    }
    (repo / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    m = stage(repo, run_id="x1run", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
    assert [r.id for r in m.rows] == ["X1"]
    assert m.rows[0].writes[0] == "pkg/a.py"
    assert (repo / m.rows[0].brief_path).is_file()
    assert (repo / RUN_DIR_ROOT / "x1run" / "2026-10-01-xs-fixture.spine.md").is_file()


@pytest.mark.parametrize("run_id", ["..", "a/b", ""])
def test_unsafe_run_id_refused(repo, run_id):
    with pytest.raises(AskStageError):
        stage(repo, run_id=run_id, plan_rel=PLAN_REL)


def test_plan_outside_repo_refused(repo):
    with pytest.raises(AskStageError):
        stage(repo, run_id="r1", plan_rel="../elsewhere.md")


def test_both_or_neither_input_refused(repo):
    with pytest.raises(AskStageError):
        stage(repo, run_id="r1")
    with pytest.raises(AskStageError):
        stage(repo, run_id="r1", plan_rel=PLAN_REL, sizing_rel=SIZING_REL)


def test_handler_resolves_the_sizing_from_the_common_dir_the_engine_passes(repo):
    """The dispatcher hands common_dir-scoped ops `<repo>/.git`, not the worktree root."""
    (repo / ".git").mkdir()
    doc = {
        "intent": "Fix the thing",
        "exit_criterion": {"statement": "it works"},
        "deliverable_id": "dlv-xs-abc123",
    }
    (repo / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    reply = _handler(
        {"run_id": "gitrun", "sizing_path": SIZING_REL, "writes": ["pkg/a.py"]},
        repo_root=repo / ".git",
    )
    assert "error" not in reply, reply
    assert [r["id"] for r in reply["rows"]] == ["X1"]


def test_xs_spine_and_manifest_carry_a_real_form_plan_id(repo):
    import re

    from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter

    doc = {"intent": "Fix the thing", "exit_criterion": {"statement": "it works"}}
    (repo / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    m = stage(repo, run_id="x1run", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
    spine = repo / RUN_DIR_ROOT / "x1run" / "2026-10-01-xs-fixture.spine.md"
    plan_id = read_fm_field_unquoted(split_frontmatter(spine.read_text(encoding="utf-8")).fm_text, "plan_id")
    assert re.fullmatch(r"pln-[a-z0-9-]+-[0-9a-f]{6}", plan_id)
    assert m.plan_id == plan_id
    assert StageManifest.from_json(json.loads((repo / m.run_dir / "manifest.json").read_text())).plan_id == plan_id


@pytest.mark.parametrize(
    "params",
    [
        None,
        [],
        {},
        {"run_id": 5},
        {"run_id": "r", "plan_path": 3},
        {"run_id": "r", "sizing_path": ["x"]},
        {"run_id": "r", "writes": "a.py"},
        {"run_id": "r", "writes": [1]},
    ],
)
def test_handler_refuses_malformed_params_with_a_structured_error(repo, params):
    reply = _handler(params, repo_root=repo)
    assert set(reply) == {"error"} and "dispatch.ask_stage" in reply["error"]


def test_stage_registers_declared_paths_as_the_sessions_review_targets(repo):
    m = stage(repo, run_id="r1", plan_rel=PLAN_REL, session_id="sess-1")
    targets = (repo / ".git" / "coordinator-sessions" / "sess-1" / "review-targets.txt").read_text(encoding="utf-8")
    assert set(targets.split()) >= set(m.review_declared_paths)
