"""
Tests for coordinator_core.ops.dispatch_emit.emit.

Spec backlink: pln-the-emitter-turns-a-plan-spine-d08dda § C4.
"""

from __future__ import annotations

import re
import textwrap

import pytest

from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit.emit import (
    NoWavesError,
    assert_zero_errors,
    compose_script,
    emit_script,
)
from coordinator_core.ops.dispatch_emit.pathspec import NoTestTargetError, NoWritesDeclaredError
from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

_AGENT_CALL_RE = re.compile(r"agent\s*\(")
_META_PHASE_LINE_RE = re.compile(r"phases\s*:\s*\[([^\]]*)\]")
# Matches one single-quoted JS string literal as emitted by
# `_js_string_literal` (backslash/quote-escaped, never containing a bare
# unescaped `'`). Used to split `meta.phases`'s bracket contents into its
# individual title literals -- a naive `.split(",")` on that contents string
# is unsafe because a wave title itself can contain a comma (e.g. a
# multi-row wave title "Wave 1: C1, C6"), which would wrongly split mid-title.
_JS_STRING_LITERAL_RE = re.compile(r"'((?:[^'\\]|\\.)*)'")


def _extract_phase_titles(script: str) -> list[str]:
    """Parse `meta.phases`'s emitted quoted-literal list from `script`.

    Extracts each individual `'...'` JS string literal inside the
    `phases: [...]` bracket (unescaping `\\'`/`\\\\`), rather than naively
    splitting the bracket contents on `,` -- a title containing a comma
    (e.g. a multi-row wave title "Wave 1: C1, C6") would otherwise be
    split mid-title.
    """
    m = _META_PHASE_LINE_RE.search(script)
    assert m is not None
    return [
        literal.replace("\\'", "'").replace("\\\\", "\\")
        for literal in _JS_STRING_LITERAL_RE.findall(m.group(1))
    ]


def _wave_row(id_, writes, reads=None, surface="dispatch_emit"):
    return WaveRow(
        id=id_,
        title=f"title-{id_}",
        surface=surface,
        writes=writes,
        reads=reads or [],
        depends_on=[],
    )


def _two_wave_fixture():
    return [
        [_wave_row("C1", ["coordinator_core/ops/dispatch_emit/spine_read.py"])],
        [_wave_row("C2", ["coordinator_core/ops/dispatch_emit/wave_map.py"])],
    ]


# ---------------------------------------------------------------------------
# compose_script — refusal on empty waves (the _normalize_phases substitute)
# ---------------------------------------------------------------------------


def test_compose_script_refuses_on_empty_waves():
    with pytest.raises(NoWavesError):
        compose_script([], name="empty", description="empty spine")


def test_compose_script_refuses_before_touching_pathspec_derivation():
    # No commit_pathspec/terminal_test_scope call should ever run against an
    # empty wave list -- the refusal must fire first. Regression guard: if
    # this ever silently proceeded it would raise a different, more
    # confusing error (or fabricate a phase) instead of NoWavesError.
    with pytest.raises(NoWavesError) as excinfo:
        compose_script([], name="empty", description="empty spine")
    assert "zero waves" in str(excinfo.value)


# ---------------------------------------------------------------------------
# compose_script — ordering (AC9)
# ---------------------------------------------------------------------------


def test_terminal_test_phase_is_last_and_preceded_by_a_commit_phase():
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="two waves")

    phase_titles = _extract_phase_titles(script)

    assert phase_titles[-1] == "Scoped test run"
    assert phase_titles[-2].startswith("Commit wave")


def test_every_wave_gets_one_executor_phase_and_one_commit_phase():
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="two waves")

    phase_titles = _extract_phase_titles(script)

    # 1 preflight phase + 2 waves -> 2 executor phases + 2 commit phases + 1
    # terminal test phase.
    assert len(phase_titles) == 6
    assert phase_titles[0] == "Preflight: commit claimability"
    assert phase_titles[1].startswith("Wave 1")
    assert phase_titles[2].startswith("Commit wave")
    assert phase_titles[3].startswith("Wave 2")
    assert phase_titles[4].startswith("Commit wave")
    assert phase_titles[5] == "Scoped test run"


# ---------------------------------------------------------------------------
# compose_script — agentType per phase kind
# ---------------------------------------------------------------------------


def test_wave_phase_carries_executor_agent_type():
    waves = [[_wave_row("C1", ["coordinator_core/ops/dispatch_emit/spine_read.py"])]]
    script = compose_script(waves, name="wf", description="one wave")
    assert "agentType: 'coordinator:executor'" in script


def test_commit_phase_carries_git_commit_agent_type():
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="two waves")
    # 2 commit phases + 1 preflight phase, all agentType coordinator:git-commit-agent.
    assert script.count("agentType: 'coordinator:git-commit-agent'") == 3


def test_terminal_phase_carries_test_runner_agent_type():
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="two waves")
    assert "agentType: 'coordinator:test-runner'" in script


def test_multi_row_wave_uses_parallel():
    waves = [
        [
            _wave_row("C1", ["coordinator_core/ops/dispatch_emit/spine_read.py"]),
            _wave_row("C2", ["coordinator_core/ops/dispatch_emit/wave_map.py"]),
        ]
    ]
    script = compose_script(waves, name="wf", description="parallel wave")
    assert "await parallel([" in script
    assert script.count("agentType: 'coordinator:executor'") == 2


def test_single_row_wave_is_a_plain_await_agent():
    waves = [[_wave_row("C1", ["coordinator_core/ops/dispatch_emit/spine_read.py"])]]
    script = compose_script(waves, name="wf", description="serial wave")
    assert "await parallel(" not in script
    assert "await agent(" in script


# ---------------------------------------------------------------------------
# Commit-claimability preflight (AC14)
# ---------------------------------------------------------------------------


def test_preflight_phase_is_first_and_precedes_first_executor_phase():
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="two waves")

    phase_titles = _extract_phase_titles(script)

    assert phase_titles[0] == "Preflight: commit claimability"
    first_wave_index = next(
        i for i, t in enumerate(phase_titles) if t.startswith("Wave 1")
    )
    assert first_wave_index == 1  # preflight strictly precedes the first executor phase

    preflight_pos = script.index("Preflight: commit claimability")
    first_wave_pos = script.index("await agent(", script.index("Wave 1"))
    assert preflight_pos < first_wave_pos


def test_preflight_agent_call_carries_git_commit_agent_type_and_union_pathspec():
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="two waves")

    assert "preflight:commit-claimability" in script
    # union of both waves' pathspecs present in the preflight prompt.
    assert "coordinator_core/ops/dispatch_emit/spine_read.py" in script
    assert "coordinator_core/ops/dispatch_emit/wave_map.py" in script
    assert "do not stage or commit" in script.lower()


def test_preflight_call_carries_active_model_sonnet():
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="two waves")
    body_start = script.index("async function run")
    preflight_block_start = script.index("Preflight: commit claimability", body_start)
    preflight_block_end = script.index("Wave 1", body_start)
    preflight_block = script[preflight_block_start:preflight_block_end]
    assert "model: 'sonnet'" in preflight_block


# ---------------------------------------------------------------------------
# model: 'sonnet' on every agent() call (AC11) — WARN-tier, not caught by AC5
# ---------------------------------------------------------------------------


def test_every_agent_call_carries_active_model_sonnet():
    waves = [
        [
            _wave_row("C1", ["coordinator_core/ops/dispatch_emit/spine_read.py"]),
            _wave_row("C2", ["coordinator_core/ops/dispatch_emit/wave_map.py"]),
        ],
        [_wave_row("C3", ["coordinator_core/ops/dispatch_emit/pathspec.py"])],
    ]
    script = compose_script(waves, name="wf", description="model check")

    agent_call_count = len(_AGENT_CALL_RE.findall(script))
    model_count = script.count("model: 'sonnet'")

    assert agent_call_count >= 4  # 2 wave-1 + 1 wave-2 + 2 commit + 1 test
    assert model_count == agent_call_count, (
        "every agent() call site must carry an active model: 'sonnet' — "
        f"found {agent_call_count} agent() calls but {model_count} model: "
        "'sonnet' opts entries"
    )
    assert "// model:" not in script  # never a commented placeholder


def test_run_checks_reports_no_model_default_warn():
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="warn check")
    findings = run_checks(script)
    model_warns = [f for f in findings if f.code == "agent-model-default"]
    assert model_warns == []


# ---------------------------------------------------------------------------
# AC5 — round trip through run_checks, zero ERROR findings
# ---------------------------------------------------------------------------


def test_composed_script_passes_run_checks_with_zero_errors():
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="round trip")

    findings = run_checks(script)
    errors = [f for f in findings if f.severity is Severity.ERROR]
    assert errors == [], f"unexpected ERROR findings: {errors}"


def test_assert_zero_errors_does_not_raise_on_a_conformant_script():
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="round trip")
    assert_zero_errors(script)  # must not raise


def test_assert_zero_errors_raises_on_a_broken_meta_block():
    broken_script = "async function run(ctx) {}\n"  # no meta block at all
    with pytest.raises(ValueError, match="ERROR findings"):
        assert_zero_errors(broken_script)


def test_multi_row_wave_script_also_passes_run_checks():
    waves = [
        [
            _wave_row("C1", ["coordinator_core/ops/dispatch_emit/spine_read.py"]),
            _wave_row("C2", ["coordinator_core/ops/dispatch_emit/wave_map.py"]),
        ]
    ]
    script = compose_script(waves, name="wf", description="parallel round trip")
    errors = [f for f in run_checks(script) if f.severity is Severity.ERROR]
    assert errors == []


# ---------------------------------------------------------------------------
# Propagated refusals from pathspec.py (this module adds no derivation)
# ---------------------------------------------------------------------------


def test_compose_script_propagates_no_writes_declared_from_commit_pathspec():
    waves = [[_wave_row("C1", UNDECLARED, surface="dispatch_emit")]]
    with pytest.raises(NoWritesDeclaredError):
        compose_script(waves, name="wf", description="undeclared")


def test_compose_script_propagates_no_test_target_from_terminal_scope():
    waves = [[_wave_row("C1", ["coordinator_core/subagent_sandbox/CONTRACT.md"])]]
    with pytest.raises(NoTestTargetError):
        compose_script(waves, name="wf", description="doc only")


# ---------------------------------------------------------------------------
# emit_script — full plan-file -> script pipeline
# ---------------------------------------------------------------------------


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


def test_emit_script_reads_a_plan_file_and_composes_a_conformant_script(tmp_path):
    plan_path = tmp_path / "fixture-plan.md"
    plan_path.write_text(_FIXTURE_PLAN, encoding="utf-8")

    script = emit_script(plan_path)

    assert_zero_errors(script)  # AC5, against a real read_spine/build_waves pipeline
    assert "fixture-plan" in script  # default name derived from the plan's stem
    phase_titles = _extract_phase_titles(script)
    assert phase_titles[-1] == "Scoped test run"
    assert phase_titles[-2].startswith("Commit wave")


def test_emit_script_honors_explicit_name_and_description(tmp_path):
    plan_path = tmp_path / "fixture-plan.md"
    plan_path.write_text(_FIXTURE_PLAN, encoding="utf-8")

    script = emit_script(plan_path, name="custom-name", description="custom description")

    assert "name: 'custom-name'" in script
    assert "description: 'custom description'" in script


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
