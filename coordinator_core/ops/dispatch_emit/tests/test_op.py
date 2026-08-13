"""
Tests for coordinator_core.ops.dispatch_emit.op ("dispatch.emit").

Spec backlink: pln-the-emitter-turns-a-plan-spine-d08dda § C5.
"""

from __future__ import annotations

import textwrap

import pytest

from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.dispatch_emit.emit import NoWavesError
from coordinator_core.ops.dispatch_emit.op import PathEscapeError, _dispatch_emit
from coordinator_core.ops.dispatch_emit.pathspec import NoWritesDeclaredError

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


def _write_fixture_plan(tmp_path):
    plan_path = tmp_path / "fixture-plan.md"
    plan_path.write_text(_FIXTURE_PLAN, encoding="utf-8")
    return plan_path


# ---------------------------------------------------------------------------
# Registry resolution
# ---------------------------------------------------------------------------


def test_dispatch_emit_resolves_through_the_op_registry():
    assert "dispatch.emit" in _REGISTRY
    assert _REGISTRY["dispatch.emit"] is _dispatch_emit


def test_dispatch_emit_is_classified_mutating():
    assert "dispatch.emit" in OP_CLASSIFICATION
    assert OP_CLASSIFICATION["dispatch.emit"] is OpClass.MUTATING


# ---------------------------------------------------------------------------
# Path guard — required params, and rejecting an out-of-bounds output path
# ---------------------------------------------------------------------------


def test_dispatch_emit_requires_plan_path():
    with pytest.raises(ValueError, match="plan_path"):
        _dispatch_emit({"output_path": "/tmp/whatever.mjs"})


def test_dispatch_emit_requires_output_path(tmp_path):
    plan_path = _write_fixture_plan(tmp_path)
    with pytest.raises(ValueError, match="output_path"):
        _dispatch_emit({"plan_path": str(plan_path)})


def test_dispatch_emit_rejects_an_out_of_bounds_output_path(tmp_path):
    plan_path = _write_fixture_plan(tmp_path)
    contained_dir = tmp_path / "contained"
    contained_dir.mkdir()
    escaping_output = tmp_path / "outside" / "escaped.mjs"

    with pytest.raises(PathEscapeError):
        _dispatch_emit(
            {
                "plan_path": str(plan_path),
                "output_path": str(escaping_output),
                "target_root": str(contained_dir),
            }
        )

    assert not escaping_output.exists()


# ---------------------------------------------------------------------------
# Round trip — emits to a temp path, returns both the path and the verdict
# ---------------------------------------------------------------------------


def test_dispatch_emit_round_trip_writes_and_returns_verdict(tmp_path):
    plan_path = _write_fixture_plan(tmp_path)
    output_path = tmp_path / "out" / "emitted.mjs"
    output_path.parent.mkdir()

    result = _dispatch_emit(
        {
            "plan_path": str(plan_path),
            "output_path": str(output_path),
        }
    )

    assert result["path"] == str(output_path.resolve())
    assert output_path.is_file()
    written = output_path.read_text(encoding="utf-8")
    assert written  # non-empty script was actually written
    assert result["ok"] is True
    assert result["error_count"] == 0
    assert isinstance(result["findings"], list)
    assert isinstance(result["warn_count"], int)


def test_dispatch_emit_defaults_target_root_to_output_path_parent(tmp_path):
    plan_path = _write_fixture_plan(tmp_path)
    output_path = tmp_path / "emitted.mjs"

    result = _dispatch_emit(
        {
            "plan_path": str(plan_path),
            "output_path": str(output_path),
        }
    )

    assert result["path"] == str(output_path.resolve())
    assert output_path.is_file()


def test_dispatch_emit_defaults_target_root_to_repo_root_when_given(tmp_path):
    # (Review: code-reviewer 8479038e, Finding 1) — when the request carries
    # a repo_root, a caller-named output_path OUTSIDE that repo_root escapes
    # even without an explicit target_root: the default now meaningfully
    # constrains, rather than trivially satisfying containment against its
    # own parent.
    plan_path = _write_fixture_plan(tmp_path)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside_output = tmp_path / "elsewhere" / "emitted.mjs"
    outside_output.parent.mkdir()

    with pytest.raises(PathEscapeError):
        _dispatch_emit(
            {
                "plan_path": str(plan_path),
                "output_path": str(outside_output),
            },
            repo_root=repo_root,
        )
    assert not outside_output.exists()


# ---------------------------------------------------------------------------
# Findings and refusals propagate uncaught through the op boundary
# (docstring's "Raises" contract, Finding 2 — code-reviewer 8479038e)
# ---------------------------------------------------------------------------


def test_dispatch_emit_propagates_a_missing_plan_path(tmp_path):
    missing_plan = tmp_path / "does-not-exist.md"
    output_path = tmp_path / "emitted.mjs"

    with pytest.raises(FileNotFoundError):
        _dispatch_emit(
            {
                "plan_path": str(missing_plan),
                "output_path": str(output_path),
            }
        )
    assert not output_path.exists()


def test_dispatch_emit_propagates_an_unwritable_output_path(tmp_path):
    plan_path = _write_fixture_plan(tmp_path)
    # output_path names an existing DIRECTORY, so write_text() raises --
    # the guard/emit steps succeed, only the final write fails, so this
    # exercises the "write itself fails" leg distinct from the escape guard.
    unwritable_output = tmp_path / "a-directory"
    unwritable_output.mkdir()

    with pytest.raises(IsADirectoryError):
        _dispatch_emit(
            {
                "plan_path": str(plan_path),
                "output_path": str(unwritable_output),
            }
        )


def test_dispatch_emit_propagates_an_emitter_refusal(tmp_path):
    # A spine with a LOCATED but empty task-spine block derives zero waves,
    # so emit_script -> compose_script raises NoWavesError uncaught -- no
    # false-success shape, no partial write.
    empty_spine_plan = tmp_path / "empty-spine-plan.md"
    empty_spine_plan.write_text(
        textwrap.dedent(
            """\
            ---
            title: "Empty spine plan"
            created: 2026-08-13
            author: test
            status: draft
            branch: "work/fixture"
            plan_id: "pln-empty"
            deliverable_id: "dlv-empty"
            initiative: null
            sizing_object: "state/sizings/empty.yaml"
            scope_mode: feature
            problem_set: inline
            ---

            # Empty spine plan

            ## Tasks

            ```yaml plan-tasks
            []
            ```
            """
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "emitted.mjs"

    with pytest.raises(NoWavesError):
        _dispatch_emit(
            {
                "plan_path": str(empty_spine_plan),
                "output_path": str(output_path),
            }
        )
    assert not output_path.exists()


def test_dispatch_emit_propagates_a_pathspec_refusal(tmp_path):
    # A row with no `writes:` and a non-concrete (subsystem-shaped) surface
    # contributes no path, so `pathspec.commit_pathspec` refuses with
    # NoWritesDeclaredError -- a DIFFERENT refusal site than NoWavesError
    # (raised downstream, in pathspec.py, not emit.py), same
    # propagates-uncaught contract.
    no_writes_plan = tmp_path / "no-writes-plan.md"
    no_writes_plan.write_text(
        textwrap.dedent(
            """\
            ---
            title: "No writes plan"
            created: 2026-08-13
            author: test
            status: draft
            branch: "work/fixture"
            plan_id: "pln-no-writes"
            deliverable_id: "dlv-no-writes"
            initiative: null
            sizing_object: "state/sizings/no-writes.yaml"
            scope_mode: feature
            problem_set: inline
            ---

            # No writes plan

            ## Tasks

            ```yaml plan-tasks
            - id: F1
              title: No-writes chunk
              change_kind: code-edit
              surface: dispatch_emit
              reads: []
              queue_scope: project
              disposition: open
              body: |
                Fixture body.
            ```
            """
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "emitted.mjs"

    with pytest.raises(NoWritesDeclaredError):
        _dispatch_emit(
            {
                "plan_path": str(no_writes_plan),
                "output_path": str(output_path),
            }
        )
    assert not output_path.exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
