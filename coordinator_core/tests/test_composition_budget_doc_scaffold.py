"""coordinator_core.tests.test_composition_budget_doc_scaffold -- C8's
per-host spawn assertion, folded into the composition-budget surface
(module docstring convention shared with `test_composition_budget.py` and
its siblings in this directory).

Purpose: every host this plan's B-wave (C3-C6) touches is a COMPUTE half
-- it constructs a `coordinator-doc-new` directive through the shared
constructor (`roadmap_planning_assemble.scaffold_directive.
build_scaffold_directive`) and never shells out to build it (§ Anti-scope:
"Does NOT build an apply half for any compute-half ceremony"). This test
asserts that invariant directly, for every `DOCTYPE_HOSTS` row this
plan's B-wave newly emits: computing the row's directive spawns ZERO
subprocesses (`subprocess.run`/`subprocess.Popen`), so a future edit that
accidentally has a host shell out to invoke `coordinator-doc-new` itself
-- rather than merely naming it in `directives[].cli` for something else
to dispatch -- fails here rather than quietly adding an uncounted spawn to
whichever composition eventually calls that host's `brief()`.

Table-driven, re-enumerates nothing beyond the per-host INVOCATION RECIPE:
`DOCTYPE_HOSTS` (C0) still supplies the `(type, ceremony, module)` set;
this file's `_RECIPES` maps each already-landed C3-C6 host module to the
minimal kwargs its OWN `test_scaffold_directive_parity.py` uses to trigger
that host's B-wave directive(s) -- a call-shape mapping, not a second copy
of the type list, since the recipe carries no `(type, ceremony)` fact
`DOCTYPE_HOSTS` doesn't already carry. The two pre-existing donor rows
(`handoff`/`roadmap-baton` at `baton-continuation`) are out of scope here:
they predate this plan's C1 constructor and are not part of the B-wave
this composition-budget assertion is folded for.

Negative-spec: does NOT assert on directive `args` shape, `--out`
containment, or CLI-parser parity -- those stay each host's own C3-C6
parity pin. Does NOT invoke `coordinator-doc-new` itself (a compute-half
test, same as every parity pin it borrows recipes from). Does NOT touch
`coordinator_core/composition_budget.py` or its fleet dials -- this is an
assertion ABOUT the doc-scaffold hosts' spawn behaviour, not a change to
the budget mechanism itself.

Spec backlink: docs/plans/2026-09-11-document-scaffolding-is-emitted-not-
remembered.md, chunk C8.

Run:
    pytest coordinator_core/tests/test_composition_budget_doc_scaffold.py -v
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


class _SpawnDetected(AssertionError):
    pass


@pytest.fixture
def no_spawn_guard(monkeypatch: pytest.MonkeyPatch):

    def _blow_up(*args: Any, **kwargs: Any) -> Any:
        raise _SpawnDetected(f"uncounted subprocess spawn: args={args!r} kwargs={kwargs!r}")

    monkeypatch.setattr(subprocess, "run", _blow_up)
    monkeypatch.setattr(subprocess, "Popen", _blow_up)


def _roadmap_planning_recipes() -> list[tuple[str, Callable[[], dict]]]:
    import coordinator_core.roadmap_planning_assemble as rpa

    return [
        ("roadmap-baton", lambda: rpa.brief(stub_id="stub-x")),
        ("roadmap-seed", lambda: rpa.brief(goals=["goal-a", "goal-b"])),
    ]


def _plan_assemble_recipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[], dict]:
    import coordinator_core.plan_assemble as plan_assemble
    from coordinator_core.plan_assemble import residue as residue_mod
    from coordinator_core.plan_assemble.test_residue import (
        _make_residue_dir,
        _patch_content_root,
    )

    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)
    monkeypatch.setattr(residue_mod, "show_toplevel", lambda: str(tmp_path))

    sizing_path = tmp_path / "sizing.yaml"
    sizing_path.write_text(
        "schema: sizing-object\nintent: 'Do the thing'\nroute: plan\n", encoding="utf-8"
    )

    return lambda: plan_assemble.brief(sizing_object_path=sizing_path)


def _sizing_assemble_recipe() -> Callable[[], dict]:
    import coordinator_core.sizing_assemble as sizing_assemble

    return lambda: sizing_assemble.route(
        estimate={"tshirt": "M"}, intent="Ship the scaffold emitter"
    )


def _review_assemble_recipe() -> Callable[[], dict]:
    import coordinator_core.review_assemble as review_assemble

    return lambda: review_assemble.brief(
        slice_id="A", scope=["coordinator_core/foo.py", "coordinator_core/bar.py"]
    )


def _goals_recipes() -> list[tuple[str, Callable[[], dict]]]:
    import coordinator_core.goals as goals

    return [
        ("goal", lambda: goals.brief(goal_title="Ship the widget")),
        ("goal-seed", lambda: goals.brief(goal_seed_title="Deferred vision slice")),
    ]


def _plugin_health_recipe() -> Callable[[], dict]:
    import coordinator_core.plugin_health as plugin_health

    return lambda: plugin_health.brief(emit_health_status=True)


def _execute_plan_assemble_recipe() -> Callable[[], dict]:
    import coordinator_core.execute_plan_assemble as execute_plan_assemble

    return lambda: execute_plan_assemble.brief(
        plan_path="docs/plans/2026-09-11-my-plan.md", chunk_id="C6"
    )


def _backlog_grind_assemble_recipe() -> Callable[[], dict]:
    from coordinator_core.backlog_grind_assemble import directives

    return lambda: directives.build_decision_scaffold_directive(
        id="d-scaffold-decision", title="Adopt the shared constructor"
    )


def test_roadmap_baton_and_seed_directives_spawn_nothing(no_spawn_guard) -> None:
    for _label, call in _roadmap_planning_recipes():
        call()


def test_plan_directive_spawns_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recipe = _plan_assemble_recipe(tmp_path, monkeypatch)

    def _blow_up(*a, **k):
        raise _SpawnDetected("uncounted subprocess spawn during plan directive computation")

    monkeypatch.setattr(subprocess, "run", _blow_up)
    monkeypatch.setattr(subprocess, "Popen", _blow_up)
    recipe()


def test_sizing_object_directive_spawns_nothing(no_spawn_guard) -> None:
    _sizing_assemble_recipe()()


def test_review_findings_directive_spawns_nothing(no_spawn_guard) -> None:
    _review_assemble_recipe()()


def test_goal_and_goal_seed_directives_spawn_nothing(no_spawn_guard) -> None:
    for _label, call in _goals_recipes():
        call()


def test_health_status_directive_spawns_nothing(no_spawn_guard) -> None:
    _plugin_health_recipe()()


def test_run_report_directive_spawns_nothing(no_spawn_guard) -> None:
    _execute_plan_assemble_recipe()()


def test_decision_directive_spawns_nothing(no_spawn_guard) -> None:
    _backlog_grind_assemble_recipe()()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
