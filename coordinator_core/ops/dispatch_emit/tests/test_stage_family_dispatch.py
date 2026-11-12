"""Pins per-stage family dispatch: a family row hands to the runner, an unset one stays claude."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import stage_family
from coordinator_core.ops.dispatch_emit.ask_stage import stage
from coordinator_core.ops.dispatch_emit.ask_stage_blocks import AskStageError
from coordinator_core.ops.dispatch_emit.spine_read import InvalidFieldTypeError, read_spine
from coordinator_core.ops.dispatch_emit.tests.conftest import write_run_scripts

PLAN_REL = "docs/plans/2026-11-01-family-fixture.md"
SIZING_REL = "state/sizings/2026-11-01-xs-family.yaml"

_PLAN = """---
deliverable_id: dlv-family-abc123
---
# Family fixture

## Problem

Rows with and without a family.

## Tasks

```yaml plan-tasks
- id: C1
  title: first
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
{c1_extra}- id: C2
  title: second
  change_kind: code-edit
  surface: pkg/b.py
  writes:
    - pkg/b.py
```
"""


def _plan(c1_extra: str = "") -> str:
    return _PLAN.format(c1_extra=c1_extra)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    write_run_scripts(tmp_path, "r1", "x1run")
    return tmp_path


def _write_plan(repo, c1_extra=""):
    path = repo / PLAN_REL
    path.write_text(_plan(c1_extra), encoding="utf-8", newline="\n")
    return path


def test_family_row_dispatches_to_runner_with_family_in_brief(repo):
    path = _write_plan(repo, "  family: codex\n")
    rows = {r.id: r for r in read_spine(path, schema_preflight=False)}
    assert rows["C1"].agent_type == stage_family.RUNNER_AGENT
    assert "family: codex" in rows["C1"].body and "  - pkg/a.py" in rows["C1"].body
    assert rows["C2"].agent_type is None and "family:" not in rows["C2"].body


def test_staged_manifest_swaps_only_the_family_row(repo):
    _write_plan(repo, "  family: codex\n")
    m = stage(repo, run_id="r1", plan_rel=PLAN_REL)
    by_id = {r.id: r for r in m.rows}
    assert by_id["C1"].agent_type == "coordinator:family-stage-runner"
    assert by_id["C2"].agent_type == "coordinator:executor"
    assert "family: codex" in (repo / by_id["C1"].brief_path).read_text(encoding="utf-8")
    assert "family:" not in (repo / by_id["C2"].brief_path).read_text(encoding="utf-8")


def test_absent_and_explicit_claude_family_stay_claude(repo):
    _write_plan(repo, "  family: claude\n")
    m = stage(repo, run_id="r1", plan_rel=PLAN_REL)
    assert {r.agent_type for r in m.rows} == {"coordinator:executor"}


def test_unknown_family_is_a_parse_refusal(repo):
    path = _write_plan(repo, "  family: gpt\n")
    with pytest.raises(InvalidFieldTypeError, match=r"claude\|codex\|grok\|agy"):
        read_spine(path, schema_preflight=False)


def test_family_beside_explicit_agent_type_is_refused(repo):
    path = _write_plan(repo, "  family: codex\n  agent_type: coordinator:executor\n")
    with pytest.raises(InvalidFieldTypeError, match="keep one"):
        read_spine(path, schema_preflight=False)


def _sizing(**over):
    doc = {
        "intent": "Fix the thing",
        "exit_criterion": {"statement": "it works"},
        "deliverable_id": "dlv-xs-abc123",
    }
    doc.update(over)
    return doc


def test_sizing_family_routes_its_rows_to_the_runner(repo):
    (repo / SIZING_REL).write_text(yaml.safe_dump(_sizing(family="agy")), encoding="utf-8", newline="\n")
    m = stage(repo, run_id="x1run", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
    assert [r.agent_type for r in m.rows] == ["coordinator:family-stage-runner"]
    assert "family: agy" in (repo / m.rows[0].brief_path).read_text(encoding="utf-8")


def test_sizing_without_family_stays_executor(repo):
    (repo / SIZING_REL).write_text(yaml.safe_dump(_sizing()), encoding="utf-8", newline="\n")
    m = stage(repo, run_id="x1run", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
    assert [r.agent_type for r in m.rows] == ["coordinator:executor"]


def test_sizing_unknown_family_is_refused(repo):
    (repo / SIZING_REL).write_text(yaml.safe_dump(_sizing(family="gpt")), encoding="utf-8", newline="\n")
    with pytest.raises(AskStageError, match="claude"):
        stage(repo, run_id="x1run", sizing_rel=SIZING_REL, writes=["pkg/a.py"])


BINDING_YAML = """schema_version: 1
runner: coordinator:family-stage-runner
fallback: claude
roles:
  scout: grok
  research: agy
  review: codex
"""

_ROLE_PLAN = """---
deliverable_id: dlv-family-abc123
---
# Family fixture

## Problem

Rows with stage roles.

## Tasks

```yaml plan-tasks
- id: C1
  title: scout
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
  stage_role: scout
- id: C2
  title: research
  change_kind: code-edit
  surface: pkg/b.py
  writes:
    - pkg/b.py
  stage_role: research
- id: C3
  title: review
  change_kind: code-edit
  surface: pkg/c.py
  writes:
    - pkg/c.py
  stage_role: review
- id: C4
  title: plain
  change_kind: code-edit
  surface: pkg/d.py
  writes:
    - pkg/d.py
```
"""


@pytest.fixture
def content_root(tmp_path_factory, monkeypatch):
    from coordinator_core.ops.emit import doe_drift

    root = tmp_path_factory.mktemp("doe")
    (root / "coordinator" / "pipelines").mkdir(parents=True)
    (root / "coordinator" / "pipelines" / "stage-family-binding.yaml").write_text(
        BINDING_YAML, encoding="utf-8", newline="\n"
    )
    monkeypatch.setattr(doe_drift, "resolve_doe_clone", lambda: root)
    return root


def _stage_roles(repo, **sizing_over):
    from coordinator_core.ops.dispatch_emit import stage_family_binding

    (repo / PLAN_REL).write_text(_ROLE_PLAN, encoding="utf-8", newline="\n")
    sizing = _sizing(**sizing_over)
    (repo / SIZING_REL).write_text(yaml.safe_dump(sizing), encoding="utf-8", newline="\n")
    return stage(repo, run_id="r1", plan_rel=PLAN_REL, commit_sizing_rel=SIZING_REL)


def test_opted_in_role_rows_route_to_the_runner_from_the_binding(repo, content_root):
    from coordinator_core.ops.dispatch_emit import stage_family_binding

    m = _stage_roles(repo, stage_family_binding=stage_family_binding.BINDING_REF)
    by_id = {r.id: r for r in m.rows}
    for rid, fam, role in (("C1", "grok", "scout"), ("C2", "agy", "research"), ("C3", "codex", "review")):
        row = by_id[rid]
        assert row.agent_type == stage_family.RUNNER_AGENT
        assert (row.family, row.stage_role) == (fam, role)
        brief = (repo / row.brief_path).read_text(encoding="utf-8")
        assert f"family: {fam}\n" in brief and f"stage: {role}\n" in brief
        fb = repo / row.fallback_brief_path
        assert fb.is_file() and row.fallback_agent_type == "coordinator:executor"
        assert "family:" not in fb.read_text(encoding="utf-8").split("\n", 1)[0]
    plain = by_id["C4"]
    assert plain.agent_type == "coordinator:executor" and plain.family == ""
    assert "family" not in plain.to_json() and "fallback_brief_path" not in plain.to_json()


def test_role_rows_without_opt_in_never_open_the_binding(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import stage_family_binding

    def boom(*a, **k):
        raise AssertionError("binding read without opt-in")

    monkeypatch.setattr(stage_family_binding, "load", boom)
    m = _stage_roles(repo)
    assert {r.agent_type for r in m.rows} == {"coordinator:executor"}
    for r in m.rows:
        assert not any(k in r.to_json() for k in ("family", "stage_role", "fallback_brief_path", "fallback_agent_type"))
        assert not (repo / r.brief_path).with_name(f"{r.id}.fallback.md").exists()


def test_binding_read_once_per_stage_call(repo, content_root, monkeypatch):
    from coordinator_core.ops.dispatch_emit import stage_family_binding

    real, calls = stage_family_binding.load, []
    monkeypatch.setattr(stage_family_binding, "load", lambda *a, **k: calls.append(1) or real(*a, **k))
    _stage_roles(repo, stage_family_binding=stage_family_binding.BINDING_REF)
    assert len(calls) == 1


def test_sizing_naming_family_and_binding_is_refused(repo, content_root):
    from coordinator_core.ops.dispatch_emit import stage_family_binding

    with pytest.raises(AskStageError, match="keep one"):
        _stage_roles(repo, family="codex", stage_family_binding=stage_family_binding.BINDING_REF)


def test_unknown_binding_value_is_refused(repo, content_root):
    with pytest.raises(AskStageError, match="stage-family-binding.yaml"):
        _stage_roles(repo, stage_family_binding="somewhere-else")


def test_unresolvable_content_root_is_a_typed_refusal(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import stage_family_binding
    from coordinator_core.ops.emit import doe_drift

    def gone():
        raise doe_drift.DoeResolveError("no clone")

    monkeypatch.setattr(doe_drift, "resolve_doe_clone", gone)
    with pytest.raises(AskStageError, match="engine.working_repos.content_root"):
        _stage_roles(repo, stage_family_binding=stage_family_binding.BINDING_REF)


def test_manifest_row_round_trips_family_fields_and_omits_them_when_empty():
    from coordinator_core.ops.dispatch_emit.ask_contract import ManifestRow

    base = dict(id="C1", agent_type="a", model="m", brief_path="b", writes=(), wave=0)
    assert ManifestRow(**base).to_json().keys().isdisjoint(
        {"family", "stage_role", "fallback_brief_path", "fallback_agent_type"}
    )
    full = ManifestRow(**base, family="grok", stage_role="scout", fallback_brief_path="f.md", fallback_agent_type="x")
    assert ManifestRow.from_json(full.to_json()) == full
