"""
Tests for ``dispatch.emit``'s ``fire_args`` reply field (P139-C3,
docs/plans/2026-09-22-emitted-repo-anchor-binds-at-fire-time.md).

Purpose: falsify the three cases the chunk body names -- present with a
posix absolute value for a plan inside a ``.git`` tree, absent for a plan
with no ``.git`` ancestor, and absent on the queue route. Independent of
C1's ``fire.py`` binding (module docstring § Reply fields): this only
covers the reply's own ``fire_args`` convenience key.

Spec backlink: docs/plans/2026-09-22-emitted-repo-anchor-binds-at-fire-time.md
§ C3.
"""

from __future__ import annotations

import textwrap

from coordinator_core.ops.dispatch_emit.op import _dispatch_emit

_FIXTURE_PLAN = textwrap.dedent(
    """\
    ---
    title: "Fixture plan"
    created: 2026-08-13
    author: test
    status: draft
    branch: "work/fixture"
    plan_id: "pln-fixture"
    deliverable_id: "dlv-fixture"
    initiative: null
    sizing_object: "state/sizings/fixture.yaml"
    scope_mode: feature
    problem_set: inline
    ---

    # Fixture plan

    ## Tasks

    ```yaml plan-tasks
    - id: F1
      title: First fixture chunk
      change_kind: code-edit
      surface: coordinator_core/ops/dispatch_emit/spine_read.py
      writes:
        - coordinator_core/ops/dispatch_emit/spine_read.py
      reads: []
      queue_scope: project
      disposition: open
      body: |
        Fixture body.
    - id: F2
      title: Second fixture chunk
      change_kind: code-edit
      surface: coordinator_core/ops/dispatch_emit/wave_map.py
      writes:
        - coordinator_core/ops/dispatch_emit/wave_map.py
      reads: []
      queue_scope: project
      disposition: open
      body: |
        Fixture body.
    ```
    """
)

_FIXTURE_PROFILE_DIR = __import__("pathlib").Path(__file__).parent / "fixtures" / "queue-profiles"


def _write_fixture_plan(tmp_path):
    plan_path = tmp_path / "fixture-plan.md"
    plan_path.write_text(_FIXTURE_PLAN, encoding="utf-8")
    return plan_path


def test_fire_args_present_with_posix_root_for_plan_inside_git_tree(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()
    plan_path = _write_fixture_plan(repo_root)
    output_path = repo_root / "out.mjs"

    reply = _dispatch_emit(
        {
            "plan_path": str(plan_path),
            "output_path": str(output_path),
            "target_root": str(repo_root),
        }
    )

    assert reply["fire_args"] == {"repoRoot": repo_root.resolve().as_posix()}


def test_fire_args_absent_for_plan_with_no_git_ancestor(tmp_path):
    plan_path = _write_fixture_plan(tmp_path)
    output_path = tmp_path / "out.mjs"

    reply = _dispatch_emit(
        {
            "plan_path": str(plan_path),
            "output_path": str(output_path),
            "target_root": str(tmp_path),
        }
    )

    assert "fire_args" not in reply


def test_fire_args_absent_on_queue_route(tmp_path):
    repo_root = tmp_path / "repo"
    queue_dir = repo_root / "state" / "bug-backlog"
    run_dir = repo_root / "state" / "queue-grind" / "fixture" / "run-1"
    queue_dir.mkdir(parents=True)
    run_dir.mkdir(parents=True)
    (queue_dir / "row-a.yaml").write_text(
        'created: "2026-09-21"\n'
        'title: "a reproducible bug"\n'
        'body: "a body"\n'
        'status: "open"\n'
        'surface: "coordinator_core/x"\n'
        'severity: "P1"\n',
        encoding="utf-8",
    )
    output_path = run_dir / "script.mjs"

    reply = _dispatch_emit(
        {
            "queue": [str(queue_dir)],
            "profile": "fixture",
            "profile_dir": str(_FIXTURE_PROFILE_DIR),
            "output_path": str(output_path),
            "target_root": str(repo_root),
        }
    )

    assert "fire_args" not in reply
