"""ask_stage: briefs equal the plan route's prompts, the manifest round-trips, XS yields row X1."""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import emit, reply_sidecar
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT, StageManifest
from coordinator_core.ops.dispatch_emit.ask_stage import AskStageError, _handler, stage
from coordinator_core.ops.dispatch_emit.commit_request import parse_marker
from coordinator_core.ops.dispatch_emit.spine_read import read_spine
from coordinator_core.ops.dispatch_emit.wave_map import build_waves

def read_stage_reply(root, reply: dict) -> dict:
    """A refusal reply as-is; a success reply's manifest read through its verified `manifest_handle`."""
    if "error" in reply:
        return reply
    assert set(reply) - {"already_staged"} == {"manifest_handle"}, reply
    return reply_sidecar.read_reply(root, reply["manifest_handle"])


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
    on_disk = json.loads((repo / RECORD_DIR_ROOT / "r1" / "manifest.json").read_text(encoding="utf-8"))
    assert StageManifest.from_json(on_disk) == m
    assert m.run_dir == f"{RUN_DIR_ROOT}/r1"
    assert not (repo / m.run_dir / "manifest.json").exists()
    req = parse_marker((repo / m.marker_path).read_text(encoding="utf-8"))
    assert {c.id for c in req.chunks} == {"C1", "C2", "C3", "docs"}
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
    assert (repo / RECORD_DIR_ROOT / "x1run" / "2026-10-01-xs-fixture.spine.md").is_file()
    assert not list((repo / RUN_DIR_ROOT / "x1run").glob("*.spine.md"))


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
    reply = read_stage_reply(repo, _handler(
        {"run_id": "gitrun", "sizing_path": SIZING_REL, "writes": ["pkg/a.py"]},
        repo_root=repo / ".git",
    ))
    assert "error" not in reply, reply
    assert [r["id"] for r in reply["rows"]] == ["X1"]


def test_xs_spine_and_manifest_carry_a_real_form_plan_id(repo):
    import re

    from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter

    doc = {"intent": "Fix the thing", "exit_criterion": {"statement": "it works"}}
    (repo / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    m = stage(repo, run_id="x1run", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
    spine = repo / RECORD_DIR_ROOT / "x1run" / "2026-10-01-xs-fixture.spine.md"
    plan_id = read_fm_field_unquoted(split_frontmatter(spine.read_text(encoding="utf-8")).fm_text, "plan_id")
    assert re.fullmatch(r"pln-[a-z0-9-]+-[0-9a-f]{6}", plan_id)
    assert m.plan_id == plan_id
    assert StageManifest.from_json(json.loads((repo / RECORD_DIR_ROOT / "x1run" / "manifest.json").read_text())).plan_id == plan_id


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
    (repo / ".git").mkdir(exist_ok=True)
    m = stage(repo, run_id="r1", plan_rel=PLAN_REL, session_id="sess-1")
    targets = (repo / ".git" / "coordinator-sessions" / "sess-1" / "review-targets.txt").read_text(encoding="utf-8")
    assert set(targets.split()) >= set(m.review_declared_paths)


def test_marker_carries_the_sizing_record_so_the_terminal_commit_lands_it(repo):
    m = stage(repo, run_id="r2", plan_rel=PLAN_REL, commit_sizing_rel=SIZING_REL)
    req = parse_marker((repo / m.marker_path).read_text(encoding="utf-8"))
    sizing = [c for c in req.chunks if c.id == "sizing"]
    assert [c.paths for c in sizing] == [(SIZING_REL,)]
    assert SIZING_REL not in m.review_declared_paths


def test_a_row_waits_on_its_own_predecessors_never_on_wave_siblings(repo):
    # C3 shares wave 0 with C1 but depends on nothing; C2 waits on C1 alone.
    m = stage(repo, run_id="r1", plan_rel=PLAN_REL)
    deps = {r.id: r.deps for r in m.rows}
    assert deps == {"C1": (), "C2": ("C1",), "C3": ()}


def _pinned_plan(repo, pinned_text, sizing_text):
    from coordinator_core.ops import requirement_register as rr

    doc = {"requirement_register": {"sources": [{"kind": "pm"}],
                                    "rows": [{"id": "R1", "source_text": sizing_text}]}}
    (repo / "state" / "sizings" / "pinned.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    claims = (
        "register_claims:\n  sizing: state/sizings/pinned.yaml\n  rows: [R1]\n"
        f"  pins:\n    R1: '{rr.row_pin(pinned_text)}'\n"
    )
    (repo / PLAN_REL).write_text(PLAN.replace("---\n#", f"{claims}---\n#", 1), encoding="utf-8", newline="\n")


def test_drifted_pin_refuses_naming_the_row(repo):
    _pinned_plan(repo, "original words", "edited words")
    with pytest.raises(AskStageError, match="register row R1"):
        stage(repo, run_id="r1", plan_rel=PLAN_REL)


def test_matching_pin_stages_normally(repo):
    _pinned_plan(repo, "same words", "same   words")
    assert {r.id for r in stage(repo, run_id="r1", plan_rel=PLAN_REL).rows} == {"C1", "C2", "C3"}


def test_audit_and_scratch_only_rows_declare_no_reviewable_path(repo):
    """Rows writing only bookkeeping or scratch freeze no reviewable diff: an empty
    `review_declared_paths` makes the composed script skip review instead of halting at prep."""
    plan = PLAN.split("```yaml plan-tasks")[0] + """```yaml plan-tasks
- id: C1
  title: audit
  change_kind: code-edit
  surface: state/audits/x.md
  writes:
    - state/audits/x.md
    - scratch/notes.md
```
"""
    (repo / PLAN_REL).write_text(plan, encoding="utf-8", newline="\n")
    m = stage(repo, run_id="r2", plan_rel=PLAN_REL)
    assert [r.id for r in m.rows] == ["C1"]
    assert m.review_declared_paths == ()


def test_plan_branch_stages_the_spines_writes_not_the_sizing_footprint_or_caller_writes(repo):
    """A sizing footprint guess and caller `writes` never reach a plan-branch row or the review-declared paths."""
    sizing_rel = "state/sizings/2026-10-01-guess.yaml"
    (repo / sizing_rel).write_text(
        yaml.safe_dump({"intent": "x", "footprint": ["guess/old.py"]}), encoding="utf-8", newline="\n"
    )
    plan = PLAN.replace("---\n#", f"sizing_object: {sizing_rel}\n---\n#", 1)
    (repo / PLAN_REL).write_text(plan, encoding="utf-8", newline="\n")
    (repo / ".git").mkdir(exist_ok=True)
    reply = read_stage_reply(repo, _handler(
        {"run_id": "spine", "plan_path": PLAN_REL, "writes": ["guess/old.py", "guess/other.py"]},
        repo_root=repo / ".git",
    ))
    assert "error" not in reply, reply
    staged = {w for r in reply["rows"] for w in r["writes"]}
    assert {"pkg/a.py", "pkg/b.py", "pkg/c.py"} <= staged
    assert not any(w.startswith("guess/") for w in staged)
    assert not any(w.startswith("guess/") for w in reply["review_declared_paths"])


def test_plugin_agent_row_without_agent_model_stages_with_frontmatter_model(repo, tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home")
    agents = home / ".claude" / "plugins" / "game-dev" / "agents"
    agents.mkdir(parents=True)
    (agents / "staff-game-dev.md").write_text(
        "---\nname: staff-game-dev\nmodel: opus\n---\nbody\n", encoding="utf-8", newline="\n"
    )
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    plan = PLAN.split("```yaml plan-tasks")[0] + """```yaml plan-tasks
- id: C1
  title: plugin agent row
  change_kind: code-edit
  surface: pkg/a.py
  agent_type: game-dev:staff-game-dev
  writes:
    - pkg/a.py
```
"""
    (repo / PLAN_REL).write_text(plan, encoding="utf-8", newline="\n")
    m = stage(repo, run_id="r3", plan_rel=PLAN_REL)
    row = next(r for r in m.rows if r.id == "C1")
    assert (row.agent_type, row.model) == ("game-dev:staff-game-dev", "opus")
    assert row.brief_path
