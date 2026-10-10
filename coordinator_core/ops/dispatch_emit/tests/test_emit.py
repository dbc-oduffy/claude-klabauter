"""
Tests for coordinator_core.ops.dispatch_emit.emit.

Spec backlink: pln-the-emitter-turns-a-plan-spine-d08dda § C4.
"""

from __future__ import annotations
from coordinator_core.ops.dispatch_emit.emit import NoReviewStageError
from .conftest import REVIEW_KW, execute_section, _V5_ROSTER_FRAGMENT, _V5_STAGE_SCHEMAS

import re
import subprocess as _subprocess
import textwrap

import pytest

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit.emit import (
    MalformedAgentOverrideError,
    MixedAgentTypeRowError,
    UnroutableWorkKindRowError,
    NoWavesError,
    ReviewRosterFragmentError,
    assert_zero_errors,
    compose_script,
    derive_review_tier,
    emit_script,
)
from coordinator_core.ops.dispatch_emit.pathspec import NoWritesDeclaredError, commit_pathspec
from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED, read_spine

from ._shared_expand import expand_shared
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow, build_waves
from coordinator_core.ops.workflow_scaffold import _js_string_literal

_AGENT_CALL_RE = re.compile(r"agent\s*\(")
_META_PHASE_LINE_RE = re.compile(r"phases\s*:\s*\[([^\]]*)\]")
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


def _wave_row(
    id_, writes, reads=None, surface="dispatch_emit", agent_type=None, agent_model=None
):
    return WaveRow(
        id=id_,
        title=f"title-{id_}",
        surface=surface,
        writes=writes,
        reads=reads or [],
        depends_on=[],
        agent_type=agent_type,
        agent_model=agent_model,
    )


def _two_wave_fixture():
    return [
        [_wave_row("C1", ["coordinator_core/ops/dispatch_emit/spine_read.py"])],
        [_wave_row("C2", ["coordinator_core/ops/dispatch_emit/wave_map.py"])],
    ]


def test_compose_script_refuses_on_empty_waves():
    with pytest.raises(NoWavesError):
        compose_script([], name="empty", description="empty spine", **REVIEW_KW)


def test_compose_script_refuses_before_touching_pathspec_derivation():
    with pytest.raises(NoWavesError) as excinfo:
        compose_script([], name="empty", description="empty spine", **REVIEW_KW)
    assert "zero waves" in str(excinfo.value)


# ---------------------------------------------------------------------------
# compose_script — top-level body, never an uninvoked wrapper (BREAK-CLASS)
# ---------------------------------------------------------------------------


def test_composed_script_never_wraps_body_in_an_uninvoked_run_function():
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="two waves", **REVIEW_KW)

    assert "function run(" not in script
    assert "function run (" not in script


def test_first_statement_after_meta_block_is_a_phase_call():
    """The invariant this guards is "never wrapped in a function nothing
    calls" (module docstring § Top-level body), not literally "the very
    first token is `phase(`" -- `const _incompleteChunks = [];` is a
    top-level `const`, declared once, ahead of the first phase purely
    because every later status-check block needs somewhere to push into.
    """
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="two waves", **REVIEW_KW)

    meta_end = script.index("};\n") + len("};\n")
    remainder = expand_shared(script[meta_end:]).lstrip()
    assert remainder.startswith("const _incompleteChunks = [];")
    phase_idx = remainder.index("phase(")
    rows_idx = remainder.index("const _rows = {};")
    assert phase_idx < rows_idx


def test_terminal_test_phase_is_last():
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="two waves", **REVIEW_KW)

    phase_titles = _extract_phase_titles(script)

    assert phase_titles[-1] == "Scoped test run"


def test_every_row_gets_one_execute_phase_no_commit_phase():
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="two waves", **REVIEW_KW)

    phase_titles = _extract_phase_titles(script)

    assert phase_titles == ["Execute", "Review prep", "Review wave", "Review integration", "Scoped test run"]


def test_wave_phase_carries_executor_agent_type():
    waves = [[_wave_row("C1", ["coordinator_core/ops/dispatch_emit/spine_read.py"])]]
    script = compose_script(waves, name="wf", description="one wave", **REVIEW_KW)
    assert "agentType: 'coordinator:executor'" in script


def test_git_commit_agent_appears_only_as_the_serialised_checkpoint_committer():
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="two waves", **REVIEW_KW)
    assert script.count("agentType: 'coordinator:git-commit-agent'") == 1
    assert "label: 'commit:wave-' + n" in script
    assert "Commit wave" not in script


def test_terminal_phase_carries_test_runner_agent_type():
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="two waves", **REVIEW_KW)
    assert "agentType: 'coordinator:test-runner'" in script


def test_multi_row_wave_never_uses_parallel_wrap():
    waves = [
        [
            _wave_row("C1", ["coordinator_core/ops/dispatch_emit/spine_read.py"]),
            _wave_row("C2", ["coordinator_core/ops/dispatch_emit/wave_map.py"]),
        ]
    ]
    script = compose_script(waves, name="wf", description="two rows", **REVIEW_KW)
    assert "parallel(" not in execute_section(script)
    assert script.count("agentType: 'coordinator:executor'") == 2


def test_plan_body_write_row_derives_enricher_agent_type():
    waves = [
        [
            _wave_row("C1", ["docs/plans/2026-08-13-example.md"]),
            _wave_row("C2", ["coordinator_core/ops/dispatch_emit/spine_read.py"]),
        ]
    ]
    script = compose_script(waves, name="wf", description="plan body row", **REVIEW_KW)
    assert "agentType: 'coordinator:enricher'" in script
    assert script.count("agentType: 'coordinator:executor'") == 1


def test_ordinary_code_row_still_derives_executor_agent_type():
    waves = [[_wave_row("C1", ["coordinator_core/ops/dispatch_emit/spine_read.py"])]]
    script = compose_script(waves, name="wf", description="code row", **REVIEW_KW)
    assert "agentType: 'coordinator:executor'" in script
    assert "agentType: 'coordinator:enricher'" not in script


def test_mixed_plan_body_and_code_row_raises_mixed_agent_type_error():
    waves = [
        [
            _wave_row(
                "C1",
                [
                    "docs/plans/2026-08-13-example.md",
                    "coordinator_core/ops/dispatch_emit/spine_read.py",
                ],
            )
        ]
    ]
    with pytest.raises(MixedAgentTypeRowError):
        compose_script(waves, name="wf", description="mixed row", **REVIEW_KW)


def test_problem_set_write_row_derives_enricher_agent_type():
    waves = [
        [
            _wave_row("C1", ["docs/problems/2026-08-13-example.md"]),
            _wave_row("C2", ["coordinator_core/ops/dispatch_emit/spine_read.py"]),
        ]
    ]
    script = compose_script(waves, name="wf", description="problem-set row", **REVIEW_KW)
    assert "agentType: 'coordinator:enricher'" in script
    assert script.count("agentType: 'coordinator:executor'") == 1


def test_mixed_problem_set_and_code_row_raises_mixed_agent_type_error():
    waves = [
        [
            _wave_row(
                "C1",
                [
                    "docs/problems/2026-08-13-example.md",
                    "coordinator_core/ops/dispatch_emit/spine_read.py",
                ],
            )
        ]
    ]
    with pytest.raises(MixedAgentTypeRowError):
        compose_script(waves, name="wf", description="mixed problem-set row", **REVIEW_KW)


def test_undeclared_writes_row_propagates_no_writes_declared_before_agent_type_matters():
    # UNDECLARED never reaches agentType derivation in a real run: pathspec's
    # commit_pathspec refuses it first (NoWritesDeclaredError). Regression
    # guard for that ordering -- see _row_agent_type's UNDECLARED docstring.
    waves = [[_wave_row("C1", UNDECLARED)]]
    with pytest.raises(NoWritesDeclaredError):
        compose_script(waves, name="wf", description="undeclared row", **REVIEW_KW)


def test_malformed_agent_type_raises_malformed_agent_override_error():
    waves = [
        [
            _wave_row(
                "C1",
                ["coordinator_core/ops/dispatch_emit/spine_read.py"],
                agent_type="bad type!",
            )
        ]
    ]
    with pytest.raises(MalformedAgentOverrideError):
        compose_script(waves, name="wf", description="malformed agent_type", **REVIEW_KW)


def test_malformed_agent_model_raises_malformed_agent_override_error():
    waves = [
        [
            _wave_row(
                "C1",
                ["coordinator_core/ops/dispatch_emit/spine_read.py"],
                agent_model="bad model!",
            )
        ]
    ]
    with pytest.raises(MalformedAgentOverrideError):
        compose_script(waves, name="wf", description="malformed agent_model", **REVIEW_KW)


def test_mixed_writes_row_still_raises_even_with_explicit_agent_type():
    waves = [
        [
            _wave_row(
                "C1",
                [
                    "docs/plans/2026-08-13-example.md",
                    "coordinator_core/ops/dispatch_emit/spine_read.py",
                ],
                agent_type="coordinator:workflow-maker",
            )
        ]
    ]
    with pytest.raises(MixedAgentTypeRowError):
        compose_script(waves, name="wf", description="mixed row, explicit agent_type", **REVIEW_KW)


def test_unmodellable_agent_type_alone_raises_malformed_agent_override_error():
    """An explicit agent_type absent from _AGENT_MODELS, with no agent_model
    supplied, refuses rather than silently downgrading to a default model
    (Ruling 2: the field that closes the silent-Sonnet-downgrade hole is the
    refusal itself, not agent_model alone)."""
    waves = [
        [
            _wave_row(
                "C1",
                ["coordinator_core/ops/dispatch_emit/spine_read.py"],
                agent_type="coordinator:workflow-maker",
            )
        ]
    ]
    with pytest.raises(MalformedAgentOverrideError):
        compose_script(waves, name="wf", description="unmodellable agent_type alone", **REVIEW_KW)


def test_unmodellable_agent_type_with_agent_model_does_not_raise():
    """The same agent_type as above does NOT refuse once agent_model
    accompanies it -- agent_model is the mechanism that satisfies the
    refusal, not a knob only the informed find."""
    waves = [
        [
            _wave_row(
                "C1",
                ["coordinator_core/ops/dispatch_emit/spine_read.py"],
                agent_type="coordinator:workflow-maker",
                agent_model="opus",
            )
        ]
    ]
    script = compose_script(waves, name="wf", description="unmodellable agent_type with model", **REVIEW_KW)
    assert "agentType: 'coordinator:workflow-maker'" in script
    assert "model: 'opus'" in script


def test_agent_model_is_escaped_not_interpolated_raw():
    """agent_model is spliced into `model: '{...}'` the same way agent_type
    is spliced into `agentType:` -- through `_js_string_literal`, not a raw
    f-string -- so a value carrying a quote cannot deform the emitted
    script. The grammar regex disallows a literal quote outright, so this
    proves the escaping path is exercised for a value the grammar itself
    permits, not merely that malformed input is refused elsewhere
    (Ruling 3)."""
    waves = [
        [
            _wave_row(
                "C1",
                ["coordinator_core/ops/dispatch_emit/spine_read.py"],
                agent_model="opus-model",
            )
        ]
    ]
    script = compose_script(waves, name="wf", description="agent_model escaping", **REVIEW_KW)
    assert "model: 'opus-model'" in script


def test_spine_text_agent_type_reaches_emitted_call(tmp_path):
    """End-to-end: spine text -> read_spine -> build_waves -> compose_script.

    ``test_explicit_agent_type_overrides_write_target_derivation`` above
    covers ``WaveRow`` -> emit only; nothing exercised the leg the peer
    executor left inert -- ``EmitterRow`` carrying neither field, so a
    plan row's ``agent_type:``/``agent_model:`` never reached ``WaveRow``
    regardless of what ``compose_script`` did with it once it arrived
    (state/sizings/2026-09-05-a-plan-row-can-name-the-agent-that-runs.yaml).
    """
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(
        "# fixture plan\n\n## Tasks\n\n"
        "```yaml plan-tasks\n"
        "- id: C1\n"
        "  title: uses an override\n"
        "  surface: dispatch_emit\n"
        "  writes:\n"
        "    - coordinator_core/ops/dispatch_emit/spine_read.py\n"
        "  agent_type: coordinator:workflow-maker\n"
        "  agent_model: opus\n"
        "```\n",
        encoding="utf-8",
    )

    rows = read_spine(plan_path)
    waves = build_waves(rows)
    script = compose_script(waves, name="wf", description="spine-sourced override", **REVIEW_KW)

    assert "agentType: 'coordinator:workflow-maker'" in script
    assert "model: 'opus'" in script


def _plan_with_row(tmp_path, row_lines: str):
    """Minimal one-row spine on disk, for the un-routable-row legs below."""
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(
        "# fixture plan\n\n## Tasks\n\n```yaml plan-tasks\n" + row_lines + "```\n",
        encoding="utf-8",
    )
    return plan_path


def test_spine_text_change_kind_reaches_wave_row(tmp_path):
    """The inert-field leg, asserted BEFORE the refusal legs that depend on it.

    ``WaveRow``'s own docstring records that the two agent-override fields
    first shipped inert because this module's other tests hand-build a
    ``WaveRow``, which cannot see a break in ``read_spine`` -> ``build_waves``.
    ``change_kind`` is threaded for ``_row_agent_type``, so if it silently
    fails to arrive the refusal below never fires and the defect it closes
    reopens with every test still green. This asserts the field's arrival
    directly rather than inferring it from behaviour.
    """
    plan_path = _plan_with_row(
        tmp_path,
        "- id: C1\n"
        "  title: verifies something\n"
        "  change_kind: verification\n"
        "  surface: dispatch_emit\n"
        "  writes:\n"
        "    - coordinator_core/ops/dispatch_emit/spine_read.py\n",
    )

    rows = read_spine(plan_path)
    assert rows[0].change_kind == "verification"
    assert build_waves(rows)[0][0].change_kind == "verification"


def test_verification_row_writing_only_a_plan_body_raises_unroutable(tmp_path):
    """The measured case (wf_71d241c7-e62): execution-tier work whose only
    declared write is an immutable body. No agentType can serve it -- the
    executor is the plan-body guard's sole block target, the enricher's
    charter forbids running things -- so emit refuses instead of dispatching
    a wave that can only be spent."""
    plan_path = _plan_with_row(
        tmp_path,
        "- id: C1\n"
        "  title: Run the fast tier and close the plan out\n"
        "  change_kind: verification\n"
        "  surface: docs/plans/2026-08-13-example.md\n"
        "  writes:\n"
        "    - docs/plans/2026-08-13-example.md\n",
    )

    with pytest.raises(UnroutableWorkKindRowError) as excinfo:
        compose_script(
            build_waves(read_spine(plan_path)), name="wf", description="unroutable", **REVIEW_KW
        )
    assert "Split the row" in str(excinfo.value)


def test_an_explicit_agent_type_does_not_escape_the_unroutable_refusal(tmp_path):
    """No override reconciles an unroutable row, the same way none reconciles
    a mixed-writes one: the check sits above the override. An override names
    WHO runs a coherent row; an execution-tier row writing only an immutable
    body is incoherent for every possible runner, so honouring the override
    here only bought a dispatch that could end in a refusal.

    Measured over the corpus before this moved: 7 of 3914 spine rows carry an
    override, all 7 are this exact shape, and 0 are legitimate -- so the
    tightening converts no clean emission into a surprise refusal.
    """
    plan_path = _plan_with_row(
        tmp_path,
        "- id: C1\n"
        "  title: Run the fast tier and close the plan out\n"
        "  change_kind: verification\n"
        "  surface: docs/plans/2026-08-13-example.md\n"
        "  writes:\n"
        "    - docs/plans/2026-08-13-example.md\n"
        "  agent_type: coordinator:code-reviewer\n"
        "  agent_model: sonnet\n",
    )

    with pytest.raises(UnroutableWorkKindRowError) as excinfo:
        compose_script(
            build_waves(read_spine(plan_path)), name="wf", description="forced", **REVIEW_KW
        )
    assert "Split the row" in str(excinfo.value)


def test_verification_row_writing_an_ordinary_path_still_routes_executor(tmp_path):
    """Negative control: ``verification`` is only un-routable in combination
    with an immutable-body-only write set. On an ordinary path the executor
    both runs and writes, so nothing is refused."""
    plan_path = _plan_with_row(
        tmp_path,
        "- id: C1\n"
        "  title: verifies something\n"
        "  change_kind: verification\n"
        "  surface: dispatch_emit\n"
        "  writes:\n"
        "    - coordinator_core/ops/dispatch_emit/spine_read.py\n",
    )

    script = compose_script(
        build_waves(read_spine(plan_path)), name="wf", description="ordinary", **REVIEW_KW
    )
    assert "agentType: 'coordinator:executor'" in script


def test_doc_edit_row_writing_a_plan_body_still_routes_enricher(tmp_path):
    """The other negative control, and the one that matters most: the
    write-path derivation is CORRECT and must be left alone. An edit-shaped
    ``change_kind`` on an immutable body is ordinary enricher work."""
    plan_path = _plan_with_row(
        tmp_path,
        "- id: C1\n"
        "  title: folds a finding into the plan body\n"
        "  change_kind: doc-edit\n"
        "  surface: docs/plans/2026-08-13-example.md\n"
        "  writes:\n"
        "    - docs/plans/2026-08-13-example.md\n",
    )

    script = compose_script(
        build_waves(read_spine(plan_path)), name="wf", description="enricher", **REVIEW_KW
    )
    assert "agentType: 'coordinator:enricher'" in script


def test_spine_text_with_neither_agent_key_emits_byte_identically(tmp_path):
    """A spine declaring neither key must still emit byte-identically to the
    pre-existing hand-built ``WaveRow`` path -- the negative-spec twin of
    the positive case above, and an actual byte-for-byte comparison rather
    than only substring assertions (the name's original unmet promise;
    The prior ``_plan_text`` helper's
    ``with_agent_keys=True`` branch was dead code, never exercised)."""
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(
        "# fixture plan\n\n## Tasks\n\n"
        "```yaml plan-tasks\n"
        "- id: C1\n"
        "  title: title-C1\n"
        "  surface: dispatch_emit\n"
        "  writes:\n"
        "    - coordinator_core/ops/dispatch_emit/spine_read.py\n"
        "```\n",
        encoding="utf-8",
    )

    script_from_spine = compose_script(
        build_waves(read_spine(plan_path)), name="wf", description="no overrides", **REVIEW_KW
    )
    script_from_hand_built = compose_script(
        [[_wave_row("C1", ["coordinator_core/ops/dispatch_emit/spine_read.py"])]],
        name="wf",
        description="no overrides", **REVIEW_KW,
    )

    assert script_from_spine == script_from_hand_built
    assert "agentType: 'coordinator:workflow-maker'" not in script_from_spine
    assert "agentType: 'coordinator:executor'" in script_from_spine
    assert "model: 'opus'" not in execute_section(script_from_spine)


def test_single_row_wave_is_a_plain_await_agent():
    waves = [[_wave_row("C1", ["coordinator_core/ops/dispatch_emit/spine_read.py"])]]
    script = compose_script(waves, name="wf", description="serial wave", **REVIEW_KW)
    assert "await parallel(" not in execute_section(script)
    assert "await agent(" in script


def test_a_degraded_gitignore_filter_is_visible_in_the_emitted_script(tmp_path, monkeypatch, caplog):
    """Fail-open on git absence/timeout stays fail-open (reproducing pre-fix
    behaviour beats halting a whole run over a transient git hiccup), but
    the degradation must be MORE than a `logging.warning` nothing downstream
    reads -- the emitted script itself has to carry the fact that the
    ignore-filter did not run, so a PREFLIGHT-BLOCKED on a path that looks
    gitignored in the plan is self-explaining (Review: coordinator:code-
    reviewer, dispatch-emit slice, Finding 5)."""
    import logging

    from coordinator_core.git.run import GitResult

    def _fake_run_git(*args, **kwargs):
        return GitResult(
            returncode=127, timed_out=False, stdout="", stderr="", stdout_bytes=b""
        )

    monkeypatch.setattr("coordinator_core.git.run.run_git", _fake_run_git)

    with caplog.at_level(logging.WARNING):
        waves = [[_wave_row("C1", ["registry/registry.db"])]]
        script = compose_script(
            waves, name="wf", description="degraded", repo_root=tmp_path, **REVIEW_KW
        )

    assert "GITIGNORE FILTER DID NOT RUN" in script
    assert "could not run" in caplog.text


def test_a_healthy_gitignore_filter_carries_no_degraded_narration(tmp_path):
    """The negative half: an ordinary run (no paths to filter, so the early
    return never touches git) must not emit the degraded narration."""
    waves = [[_wave_row("C1", ["a.py"])]]
    script = compose_script(waves, name="wf", description="healthy", repo_root=tmp_path, **REVIEW_KW)
    assert "GITIGNORE FILTER DID NOT RUN" not in script


def test_completed_run_returns_a_positive_record_not_undefined():
    """A finished run and a run that fell off the end must not look alike.

    The body used to end on `await agent(...)`, so a completed run returned
    `undefined` -- indistinguishable from a script that never reached its
    last phase. Same shape as the preflight's absent-output pass verdict.
    Replaced by ``wake_digest.completion_return_js``'s wake-digest shape
    (§ Design D1, task C13) -- ``completed`` now reads off ``outcome``.
    """
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves", **REVIEW_KW)
    assert "outcome: (_halted ? 'halted' :" in script
    assert script.rstrip().endswith("};")


def test_completion_record_names_the_chunks_the_recovery_triple_greps_for():
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves", **REVIEW_KW)
    tail = script[script.index("return {"):]
    assert "chunks: [" in tail
    for wave in _two_wave_fixture():
        for row in wave:
            assert f'"{row.id}"' in tail, row.id


def test_completion_return_is_last_so_no_phase_follows_it():
    """An early completion return would silently skip the phases after it."""
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves", **REVIEW_KW)
    # The one sanctioned early return: a clean prep over no product file
    # ends the run as a no-op (execute_review), since nothing follows it
    # that has anything to act on.
    noop = "return { halted: 'no-op'"
    assert script.count(noop) <= 1
    # The other: a prep that produced no slices (and is not verify-only) halts
    # with a result, since the wave has nothing to review.
    no_slices = "return { halted: 'review prep returned no slices'"
    assert script.count(no_slices) <= 1
    script = script.replace(noop, "", 1).replace(no_slices, "", 1)
    completion = script.index("return {")
    assert "phase(" not in script[completion:]
    assert "await agent(" not in script[completion:]


def test_a_landed_row_vetoes_the_no_op_halt():
    """A checkpointed run once halted no-op on prep's count of 0 and shipped
    its rows unreviewed: the halt must also require that no row landed."""
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves", **REVIEW_KW)
    guard = script[: script.index("return { halted: 'no-op'")].rsplit("if (", 1)[1]
    assert "!Object.keys(_landed).length" in guard
    assert "const _landed = {};" in script


def test_a_finished_row_with_no_commit_vetoes_the_no_op_halt():
    """A verification row whose only write is gitignored finishes with no
    commit, so _landed stays empty; the halt must still see it finished, or
    the run skips its test phase and terminal judge."""
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves", **REVIEW_KW)
    guard = script[: script.index("return { halted: 'no-op'")].rsplit("if (", 1)[1]
    assert "!_finished.size" in guard
    assert "const _finished = new Set();" in script
    assert "if (!incomplete) _finished.add(id);" in script


def test_a_non_done_chunk_report_flips_completed_false_and_names_the_chunk():
    """Defect: a workflow reported `completed: true` while a chunk's own
    agent report carried `Status: PARTIAL`. Runtime behaviour is not
    executable from a compose-time test (there is no JS runtime here), so
    this pins the STRUCTURE the fix composes: a status-check block per
    batch feeding a shared `_incompleteChunks` array that the terminal
    return's `completed` reads, never a hard-coded `true`.
    """
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves", **REVIEW_KW)
    assert "const _incompleteChunks = [];" in script
    assert "_incompleteChunks.push(id)" in script
    assert "incomplete_chunks: [...new Set([..._incompleteChunks, ..._notStarted])]" in script


@pytest.mark.parametrize(
    "reply, incomplete",
    [
        ("PARTIAL: tasks/emit/C2.md", True),
        ("BLOCKED: tasks/emit/C2.md", True),
        ("DONE: tasks/emit/C2.md", False),
        ("DONE: tasks/emit/C2.md -- the PREFLIGHT-BLOCKED check passed", False),
        ("DONE: tasks/emit/C2.md, no PARTIAL chunks", False),
        ("report written\n<exit-status>PARTIAL</exit-status>", True),
    ],
)
def test_the_status_check_reads_the_contracts_status_position(reply, incomplete):
    """The emitted regex has no JS runtime here, but its body is also a valid
    Python pattern, so it is exercised against the JSON-stringified reply the
    emitted script actually tests."""
    import json
    import re

    from coordinator_core.ops.dispatch_emit.emit import _NON_DONE_STATUS_JS_RE

    body = _NON_DONE_STATUS_JS_RE[1 : _NON_DONE_STATUS_JS_RE.rindex("/")]
    assert bool(re.search(body, json.dumps(reply))) is incomplete


def test_multi_row_wave_status_checked_per_row_via_its_own_promise():
    """Each row is its own memoised promise (§ Design D4), so status
    classification is per-row inside the shared ``_runRow`` helper, never
    an array indexed by dispatch position (there is no shared results
    array to misindex any more)."""
    waves = [[_wave_row("C1", ["a.py"]), _wave_row("C2", ["b.py"])]]
    script = compose_script(waves, name="wf", description="two rows", **REVIEW_KW)
    assert "_rows['C1'] = _runRow('C1', []," in script
    assert "_rows['C2'] = _runRow('C2', []," in script


def test_test_runner_calls_carry_charter_haiku_while_rows_stay_sonnet():
    """A call-site ``model:`` OVERRIDES the named agent definition's own
    frontmatter, so the emitted tier must track each agentType's charter
    rather than one constant. ``test-runner`` is haiku by charter; a
    blanket sonnet billed a Sonnet for mechanical test invocation.
    Negative spec for `_model_opt`.
    """
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="tier check", **REVIEW_KW)

    def opts_for(label):
        idx = script.index(label)
        return script[idx:script.index("})", idx)]

    test_label_idx = script.find("agentType: 'coordinator:test-runner'")
    assert test_label_idx != -1, "fixture must compose the terminal test phase"
    assert "model: 'haiku'" in script[test_label_idx:script.index("})", test_label_idx)]

    assert "model: 'sonnet'" in opts_for("work:"), (
        "executor rows keep their charter sonnet - only the mechanical "
        "agents drop to haiku"
    )


def test_every_agent_call_carries_an_active_model():
    waves = [
        [
            _wave_row("C1", ["coordinator_core/ops/dispatch_emit/spine_read.py"]),
            _wave_row("C2", ["coordinator_core/ops/dispatch_emit/wave_map.py"]),
        ],
        [_wave_row("C3", ["coordinator_core/ops/dispatch_emit/pathspec.py"])],
    ]
    script = compose_script(waves, name="wf", description="model check", **REVIEW_KW)

    agent_call_count = len(_AGENT_CALL_RE.findall(script))
    model_count = script.count("model: '")

    assert agent_call_count >= 4
    assert model_count == agent_call_count, (
        "every agent() call site must carry an active model: - "
        f"found {agent_call_count} agent() calls but {model_count} "
        "model: opts entries"
    )
    assert "// model:" not in script


def test_run_checks_reports_no_model_default_warn():
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="warn check", **REVIEW_KW)
    findings = run_checks(script)
    model_warns = [f for f in findings if f.code == "agent-model-default"]
    assert model_warns == []


def test_composed_script_passes_run_checks_with_zero_errors():
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="round trip", **REVIEW_KW)

    findings = run_checks(script)
    errors = [f for f in findings if f.severity is Severity.ERROR]
    assert errors == [], f"unexpected ERROR findings: {errors}"


def test_assert_zero_errors_does_not_raise_on_a_conformant_script():
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="round trip", **REVIEW_KW)
    assert_zero_errors(script)


def test_assert_zero_errors_raises_on_a_broken_meta_block():
    broken_script = "async function run(ctx) {}\n"
    with pytest.raises(ValueError, match="ERROR findings"):
        assert_zero_errors(broken_script)


def test_multi_row_wave_script_also_passes_run_checks():
    waves = [
        [
            _wave_row("C1", ["coordinator_core/ops/dispatch_emit/spine_read.py"]),
            _wave_row("C2", ["coordinator_core/ops/dispatch_emit/wave_map.py"]),
        ]
    ]
    script = compose_script(waves, name="wf", description="parallel round trip", **REVIEW_KW)
    errors = [f for f in run_checks(script) if f.severity is Severity.ERROR]
    assert errors == []


def test_compose_script_propagates_no_writes_declared_from_commit_pathspec():
    waves = [[_wave_row("C1", UNDECLARED, surface="dispatch_emit")]]
    with pytest.raises(NoWritesDeclaredError):
        compose_script(waves, name="wf", description="undeclared", **REVIEW_KW)


def test_all_empty_writes_wave_emits_with_no_marker():
    from coordinator_core.ops.dispatch_emit.commit_request import parse_marker

    waves = [[_wave_row("C1", []), _wave_row("C2", [])]]
    script = compose_script(waves, name="wf", description="all-empty wave", **REVIEW_KW)

    assert _AGENT_CALL_RE.search(script) is not None
    assert parse_marker(script) is None


def test_all_empty_writes_wave_still_dispatches_its_agent_calls():
    waves = [[_wave_row("C1", [])]]
    script = compose_script(waves, name="wf", description="all-empty wave", **REVIEW_KW)
    assert "C1" in script


def test_mixed_writes_wave_is_unaffected_by_the_all_empty_branch():
    from coordinator_core.ops.dispatch_emit.commit_request import parse_marker

    waves = [
        [
            _wave_row("C1", []),
            _wave_row("C2", ["coordinator_core/ops/dispatch_emit/wave_map.py"]),
        ]
    ]
    script = compose_script(waves, name="wf", description="mixed wave", **REVIEW_KW)
    request = parse_marker(script)
    assert request is not None
    assert [c.id for c in request.chunks] == ["C1", "C2"]
    assert request.chunks[0].paths == ()


def test_compose_script_threads_expected_branch_into_the_marker():
    from coordinator_core.ops.dispatch_emit.commit_request import parse_marker

    waves = [[_wave_row("C1", ["coordinator_core/ops/dispatch_emit/wave_map.py"])]]
    with_branch = compose_script(waves, name="wf", description="d", expected_branch="feat-x", **REVIEW_KW)
    without = compose_script(waves, name="wf", description="d", **REVIEW_KW)
    assert parse_marker(with_branch).expected_branch == "feat-x"
    assert parse_marker(without).expected_branch is None


def _emit_with_head(tmp_path, head_text):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text(head_text, encoding="utf-8")
    plan = _absent_target_plan(tmp_path, ("C1", "code-edit", ["pkg/x.py"]))
    return emit_script(plan, repo_root=tmp_path, **REVIEW_KW)


def test_emit_script_captures_the_head_branch_into_the_marker(tmp_path):
    from coordinator_core.ops.dispatch_emit.commit_request import parse_marker

    script = _emit_with_head(tmp_path, "ref: refs/heads/feat-x\n")
    assert parse_marker(script).expected_branch == "feat-x"


def test_emit_script_maps_a_detached_head_to_no_expected_branch(tmp_path):
    from coordinator_core.ops.dispatch_emit.commit_request import parse_marker

    script = _emit_with_head(tmp_path, "0123456789abcdef0123456789abcdef01234567\n")
    assert parse_marker(script).expected_branch is None


def test_all_undeclared_wave_still_raises_not_folded_into_all_empty_branch():
    # Refusal 1 (every row UNDECLARED) must keep raising -- it is not the
    # same shape as every row explicitly declaring `writes: []`.
    waves = [[_wave_row("C1", UNDECLARED, surface="dispatch_emit")]]
    with pytest.raises(NoWritesDeclaredError):
        compose_script(waves, name="wf", description="all-undeclared", **REVIEW_KW)


def test_commit_pathspec_directly_still_refuses_an_all_empty_wave():
    waves = [_wave_row("C1", []), _wave_row("C2", [])]
    with pytest.raises(NoWritesDeclaredError):
        commit_pathspec(waves)


def test_compose_script_no_longer_propagates_no_test_target_but_degrades_loudly():
    waves = [[_wave_row("C1", ["coordinator_core/ops/dispatch_emit/nonexistent_module.py"])]]
    script = compose_script(waves, name="wf", description="uncovered module", **REVIEW_KW)
    assert "No terminal test phase" in script
    assert "coordinator_core/ops/dispatch_emit/nonexistent_module.py" in script
    assert "no prime_exit_criterion.falsifier" in script


def test_compose_script_falls_back_to_the_plans_falsifier_when_no_test_target_resolves():
    waves = [[_wave_row("C1", ["coordinator_core/ops/dispatch_emit/nonexistent_module.py"])]]
    falsifier = {
        "how": "run the migration and check the flag",
        "expected_when_true": "the flag reads enabled",
        "baseline_output": "the flag reads disabled",
    }
    script = compose_script(
        waves, name="wf", description="uncovered module", falsifier=falsifier, **REVIEW_KW
    )
    assert "Scoped test run" in script
    assert "coordinator:test-runner" in script
    assert "run the migration and check the flag" in script
    assert "the flag reads enabled" in script
    assert "the flag reads disabled" in script
    assert "No terminal test phase" not in script


def test_criterion_status_guards_a_null_falsifier_result_on_the_halted_path():
    """When a falsifier stage is present, the terminal `return`'s
    `criterion.status` must guard `_falsifierResult` the same way
    `tests.status` guards its own stage-result var (`_testResult ? ... :
    'not_run'`) -- on a halted run `_falsifierResult` stays `null`
    (STOP-RULE-FIRED skips the block that assigns it), so an unguarded
    `_falsifierResult.status` throws while assembling the wake digest."""
    waves = [[_wave_row("C1", ["coordinator_core/ops/dispatch_emit/nonexistent_module.py"])]]
    falsifier = {
        "how": "run the migration",
        "expected_when_true": "succeeds",
        "baseline_output": "fails today",
    }
    script = compose_script(
        waves, name="wf", description="halted with falsifier", falsifier=falsifier, **REVIEW_KW
    )
    assert "criterion: { status: (_falsifierResult ? (_falsifierResult.differs_from_baseline === true ? 'met' : " in script
    assert "'not_met' : _falsifierResult.status))) : 'not_run')" in script


def test_compose_script_omits_the_terminal_phase_for_a_prose_only_spine():
    waves = [[_wave_row("C1", ["coordinator_core/subagent_sandbox/CONTRACT.md"])]]
    script = compose_script(waves, name="wf", description="doc only", **REVIEW_KW)
    assert "Scoped test run" not in script
    assert "_rows['C1'] = _runRow('C1', [], null," in script


def test_prime_exit_criterion_falsifier_reads_how_and_expected_when_true():
    plan_text = textwrap.dedent(
        """\
        ---
        title: x
        prime_exit_criterion:
          statement: the migration is live
          derived_from: state/sizings/x.yaml
          falsifier:
            how: run the migration script
            baseline_output: fails today
            expected_when_true: succeeds
        ---
        # Plan
        """
    )
    result = emit._prime_exit_criterion_falsifier(plan_text)
    assert result == {
        "how": "run the migration script",
        "expected_when_true": "succeeds",
        "baseline_output": "fails today",
    }


def test_prime_exit_criterion_falsifier_is_none_when_absent_or_incomplete():
    assert emit._prime_exit_criterion_falsifier("no frontmatter here") is None
    assert (
        emit._prime_exit_criterion_falsifier(
            "---\ntitle: x\nprime_exit_criterion:\n  statement: s\n---\n"
        )
        is None
    )
    assert (
        emit._prime_exit_criterion_falsifier(
            "---\ntitle: x\nprime_exit_criterion:\n  falsifier:\n    how: x\n---\n"
        )
        is None
    )


def test_a_prose_only_spine_declares_the_absent_test_run_on_the_emitted_script():
    waves = [[_wave_row("C1", ["coordinator_core/subagent_sandbox/CONTRACT.md"])]]
    script = compose_script(waves, name="wf", description="doc only", **REVIEW_KW)
    assert "No terminal test phase" in script
    assert "declared, not a pass" in script
    assert "log(" in script


def test_compose_script_still_composes_the_terminal_phase_for_a_mixed_spine():
    waves = [
        [
            _wave_row(
                "C1",
                [
                    "coordinator_core/subagent_sandbox/CONTRACT.md",
                    "coordinator_core/ops/dispatch_emit/spine_read.py",
                ],
            )
        ]
    ]
    script = compose_script(waves, name="wf", description="mixed", **REVIEW_KW)
    assert "Scoped test run" in script
    assert "No terminal test phase" not in script


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

    script = emit_script(plan_path, **REVIEW_KW)

    assert_zero_errors(script)
    assert "fixture-plan" in script
    phase_titles = _extract_phase_titles(script)
    assert phase_titles[-1] == "Scoped test run"
    assert phase_titles[0] == "Execute"


def test_emit_script_honors_explicit_name_and_description(tmp_path):
    plan_path = tmp_path / "fixture-plan.md"
    plan_path.write_text(_FIXTURE_PLAN, encoding="utf-8")

    script = emit_script(plan_path, name="custom-name", description="custom description", **REVIEW_KW)

    assert "name: 'custom-name'" in script
    assert "description: 'custom description'" in script


def test_emit_script_preamble_reaches_every_executor_prompt_once(tmp_path):
    """#89 K2: a run-wide posture block is rendered ONCE, as a `_shared`
    const, and reaches every executor row's prompt via that const's
    reference -- never inlined per row (module docstring's SharedBlocks
    "declared once" discipline)."""
    plan_path = tmp_path / "fixture-plan.md"
    plan_path.write_text(_FIXTURE_PLAN, encoding="utf-8")

    preamble = "RUN POSTURE: this is a resumed run; do not re-plan."
    script = emit_script(plan_path, preamble=preamble, **REVIEW_KW)

    assert_zero_errors(script)
    assert script.count(preamble) == 1
    assert script.count("agent(") >= 2

    baseline = emit_script(plan_path, **REVIEW_KW)
    assert preamble not in baseline


def test_plan_standing_rules_reach_every_executor_prompt_once(tmp_path):
    plan_path = tmp_path / "fixture-plan.md"
    rule = "never run UBT without the EM's slot"
    text = _FIXTURE_PLAN.replace("---\n", f"---\nstanding_rules:\n  - \"{rule}\"\n", 1)
    assert text != _FIXTURE_PLAN
    plan_path.write_text(text, encoding="utf-8")

    script = emit_script(plan_path, preamble="RUN POSTURE: resumed run.", **REVIEW_KW)

    assert_zero_errors(script)
    assert script.count(rule) == 1
    assert script.index("RUN POSTURE") < script.index(rule)



def _write_plan_with_sizing(tmp_path, tshirt: str):
    sizing_dir = tmp_path / "state" / "sizings"
    sizing_dir.mkdir(parents=True)
    sizing_path = sizing_dir / "example.yaml"
    sizing_path.write_text(
        f"schema: sizing-object\nestimate:\n  tshirt: {tshirt}\n  provisional: false\n",
        encoding="utf-8",
    )

    plan_path = tmp_path / "example-plan.md"
    plan_path.write_text(
        "---\n"
        "title: \"Example\"\n"
        "sizing_object: \"state/sizings/example.yaml\"\n"
        "---\n\n# Example\n",
        encoding="utf-8",
    )
    return plan_path


@pytest.mark.parametrize(
    "tshirt,expected_tier",
    [
        ("XS", "lightweight"),
        ("S", "lightweight"),
        ("M", "standard"),
        ("L", "standard"),
        ("XL", "full"),
        ("XXL", "full"),
    ],
)
def test_derive_review_tier_maps_every_tshirt_notch(tmp_path, tshirt, expected_tier):
    plan_path = _write_plan_with_sizing(tmp_path, tshirt)
    assert derive_review_tier(plan_path, repo_root=tmp_path) == expected_tier


def test_derive_review_tier_returns_none_when_sizing_object_absent(tmp_path):
    plan_path = tmp_path / "no-sizing-plan.md"
    plan_path.write_text(
        "---\ntitle: \"No sizing\"\nsizing_object: null\n---\n\n# No sizing\n",
        encoding="utf-8",
    )
    assert derive_review_tier(plan_path, repo_root=tmp_path) is None


def test_derive_review_tier_returns_none_when_citation_does_not_resolve(tmp_path):
    plan_path = tmp_path / "dangling-plan.md"
    plan_path.write_text(
        "---\ntitle: \"Dangling\"\nsizing_object: \"state/sizings/missing.yaml\"\n---\n\n# Dangling\n",
        encoding="utf-8",
    )
    assert derive_review_tier(plan_path, repo_root=tmp_path) is None


def test_derive_review_tier_resolves_a_citation_whose_sizing_was_archived(tmp_path):
    """A terminal sizing moves to `archive/sizings/<month>/` and its citation
    is never rewritten; the tier must still derive, or the emitted workflow
    silently composes no review phase at all."""
    archive_dir = tmp_path / "archive" / "sizings" / "2026-08"
    archive_dir.mkdir(parents=True)
    (archive_dir / "example.yaml").write_text(
        "schema: sizing-object\nestimate:\n  tshirt: XL\n  provisional: false\n",
        encoding="utf-8",
    )

    plan_path = tmp_path / "archived-sizing-plan.md"
    plan_path.write_text(
        '---\ntitle: "Archived"\n'
        'sizing_object: "state/sizings/example.yaml"\n'
        '---\n\n# Archived\n',
        encoding="utf-8",
    )
    assert derive_review_tier(plan_path, repo_root=tmp_path) == "full"


def test_derive_review_tier_returns_none_when_archive_match_is_ambiguous(tmp_path):
    """Two same-basename archived records mean the resolver cannot say which
    one the plan meant — it refuses rather than picking one."""
    for month in ("2026-07", "2026-08"):
        month_dir = tmp_path / "archive" / "sizings" / month
        month_dir.mkdir(parents=True)
        (month_dir / "example.yaml").write_text(
            "schema: sizing-object\nestimate:\n  tshirt: XL\n  provisional: false\n",
            encoding="utf-8",
        )

    plan_path = tmp_path / "ambiguous-sizing-plan.md"
    plan_path.write_text(
        '---\ntitle: "Ambiguous"\n'
        'sizing_object: "state/sizings/example.yaml"\n'
        '---\n\n# Ambiguous\n',
        encoding="utf-8",
    )
    assert derive_review_tier(plan_path, repo_root=tmp_path) is None


def test_derive_review_tier_prefers_the_live_sizing_over_an_archived_namesake(tmp_path):
    live_dir = tmp_path / "state" / "sizings"
    live_dir.mkdir(parents=True)
    (live_dir / "example.yaml").write_text(
        "schema: sizing-object\nestimate:\n  tshirt: XS\n  provisional: false\n",
        encoding="utf-8",
    )
    archive_dir = tmp_path / "archive" / "sizings" / "2026-08"
    archive_dir.mkdir(parents=True)
    (archive_dir / "example.yaml").write_text(
        "schema: sizing-object\nestimate:\n  tshirt: XXL\n  provisional: false\n",
        encoding="utf-8",
    )

    plan_path = tmp_path / "live-wins-plan.md"
    plan_path.write_text(
        '---\ntitle: "Live wins"\n'
        'sizing_object: "state/sizings/example.yaml"\n'
        '---\n\n# Live wins\n',
        encoding="utf-8",
    )
    assert derive_review_tier(plan_path, repo_root=tmp_path) == "lightweight"


def test_derive_review_tier_returns_none_when_citation_traverses_outside_root(tmp_path):
    outside_dir = tmp_path.parent / f"{tmp_path.name}-outside-sizing"
    outside_dir.mkdir(exist_ok=True)
    (outside_dir / "escape.yaml").write_text(
        "schema: sizing-object\nestimate:\n  tshirt: M\n  provisional: false\n",
        encoding="utf-8",
    )

    plan_path = tmp_path / "traversal-plan.md"
    plan_path.write_text(
        "---\ntitle: \"Traversal\"\n"
        f"sizing_object: \"../{outside_dir.name}/escape.yaml\"\n"
        "---\n\n# Traversal\n",
        encoding="utf-8",
    )
    assert derive_review_tier(plan_path, repo_root=tmp_path) is None


def test_derive_review_tier_returns_none_when_citation_is_absolute_path(tmp_path):
    outside_dir = tmp_path.parent / f"{tmp_path.name}-absolute-sizing"
    outside_dir.mkdir(exist_ok=True)
    absolute_sizing_path = outside_dir / "absolute.yaml"
    absolute_sizing_path.write_text(
        "schema: sizing-object\nestimate:\n  tshirt: M\n  provisional: false\n",
        encoding="utf-8",
    )

    plan_path = tmp_path / "absolute-plan.md"
    plan_path.write_text(
        "---\ntitle: \"Absolute\"\n"
        f"sizing_object: \"{absolute_sizing_path.as_posix()}\"\n"
        "---\n\n# Absolute\n",
        encoding="utf-8",
    )
    assert derive_review_tier(plan_path, repo_root=tmp_path) is None


def test_derive_review_tier_raises_on_unmapped_tshirt(tmp_path):
    plan_path = _write_plan_with_sizing(tmp_path, "not-a-real-notch")
    with pytest.raises(ValueError):
        derive_review_tier(plan_path, repo_root=tmp_path)


def test_compose_script_refuses_a_missing_or_pre_v5_review_roster():
    waves = _two_wave_fixture()
    v4_fragment = {"schema": "review-roster-fragment", "schema_version": 4, "tiers": {}}

    with pytest.raises(NoReviewStageError):
        compose_script(waves, name="wf", description="no review")
    with pytest.raises(NoReviewStageError):
        compose_script(
            waves, name="wf", description="fragment only", review_roster_fragment=_V5_ROSTER_FRAGMENT
        )
    with pytest.raises(NoReviewStageError):
        compose_script(
            waves, name="wf", description="schemas only", review_stage_schemas=_V5_STAGE_SCHEMAS
        )
    with pytest.raises(NoReviewStageError, match="schema_version 4"):
        compose_script(
            waves,
            name="wf",
            description="v4 fragment",
            review_roster_fragment=v4_fragment,
            review_stage_schemas=_V5_STAGE_SCHEMAS,
        )


def test_compose_script_composes_the_v5_execute_review_wave():
    waves = _two_wave_fixture()
    script = compose_script(
        waves,
        name="wf",
        description="v5 review",
        review_roster_fragment=_V5_ROSTER_FRAGMENT,
        review_stage_schemas=_V5_STAGE_SCHEMAS,
    )

    phase_titles = _extract_phase_titles(script)
    assert "Review prep" in phase_titles
    assert "Review wave" in phase_titles
    assert "Review integration" in phase_titles
    assert "_reviewPrep = await" in script
    assert "const _reviewPrep" not in script
    assert "review:coordinator:code-reviewer" in script
    assert "review:coordinator:integrator" in script


def test_compose_script_review_wave_is_guarded_by_halted_and_precedes_the_test_phase():
    waves = _two_wave_fixture()
    script = compose_script(
        waves,
        name="wf",
        description="ordering",
        review_roster_fragment=_V5_ROSTER_FRAGMENT,
        review_stage_schemas=_V5_STAGE_SCHEMAS,
    )

    assert "if (!_halted) {" in script
    phase_titles = _extract_phase_titles(script)
    integration_index = phase_titles.index("Review integration")
    assert phase_titles[integration_index + 1] == "Scoped test run"


def test_compose_script_registers_declared_paths_as_review_targets(tmp_path):
    """wf_f2892741-12c regression: the v5 integration stage runs as
    `coordinator:code-reviewer`, whose Edit is sandbox-denied outside its
    own sidecar unless its files are registered review targets
    (`block_confined_agent_write.py` M1). Composing the script is the one
    EM-side, in-process point that knows the run's declared paths, so it
    must register them here -- not leave the integration agent to discover
    mid-run that nothing was registered for it."""
    waves = _two_wave_fixture()
    compose_script(
        waves,
        name="wf",
        description="v5 review registers targets",
        review_roster_fragment=_V5_ROSTER_FRAGMENT,
        review_stage_schemas=_V5_STAGE_SCHEMAS,
        repo_root=tmp_path,
        session_id="11111111-1111-1111-1111-111111111111",
    )
    targets_file = (
        tmp_path
        / ".git"
        / "coordinator-sessions"
        / "11111111-1111-1111-1111-111111111111"
        / "review-targets.txt"
    )
    assert targets_file.is_file(), (
        "compose_script composed a v5 review wave but registered no "
        "review targets for the session -- the integration stage's Edit "
        "will be sandbox-denied"
    )
    registered = {line.strip() for line in targets_file.read_text().splitlines() if line.strip()}
    assert registered, "review-targets.txt was created but registered no paths"


def test_parse_execute_review_raises_on_malformed_fragment():
    waves = _two_wave_fixture()
    with pytest.raises(ReviewRosterFragmentError):
        compose_script(
            waves,
            name="wf",
            description="malformed fragment",
            review_roster_fragment={"schema": "review-roster-fragment", "schema_version": 5},
            review_stage_schemas=_V5_STAGE_SCHEMAS,
        )


def _mirror_repo_test_targets(tmp_path):
    """Mirror JUST enough of the real repo layout under ``tmp_path`` for
    ``pathspec.terminal_test_scope``'s stem-named-test check to resolve
    the two write paths ``_FIXTURE_PLAN`` declares (``spine_read.py``,
    ``wave_map.py``) -- so a ``repo_root=tmp_path`` sizing-citation test can
    exercise ``emit_script`` end to end without touching the real repo tree.
    """
    dispatch_emit_dir = tmp_path / "coordinator_core" / "ops" / "dispatch_emit"
    tests_dir = dispatch_emit_dir / "tests"
    tests_dir.mkdir(parents=True)
    for stem in ("spine_read", "wave_map"):
        (dispatch_emit_dir / f"{stem}.py").write_text("", encoding="utf-8")
        (tests_dir / f"test_{stem}.py").write_text("", encoding="utf-8")


def test_emit_script_composes_a_review_phase_when_a_v5_fragment_and_schemas_are_supplied(
    tmp_path,
):
    _mirror_repo_test_targets(tmp_path)
    plan_path = tmp_path / "fixture-plan.md"
    sizing_dir = tmp_path / "state" / "sizings"
    sizing_dir.mkdir(parents=True)
    (sizing_dir / "fixture.yaml").write_text(
        "schema: sizing-object\nestimate:\n  tshirt: M\n  provisional: false\n",
        encoding="utf-8",
    )
    plan_path.write_text(_FIXTURE_PLAN, encoding="utf-8")

    script = emit_script(
        plan_path,
        repo_root=tmp_path,
        review_roster_fragment=_V5_ROSTER_FRAGMENT,
        review_stage_schemas=_V5_STAGE_SCHEMAS,
    )

    phase_titles = _extract_phase_titles(script)
    assert "Review integration" in phase_titles
    assert "review:coordinator:integrator" in script


_REAL_TEST_MAPPED_PATHS = [
    "coordinator_core/ops/dispatch_emit/spine_read.py",
    "coordinator_core/ops/dispatch_emit/wave_map.py",
]


def _large_wave(n: int, prefix: str = "C"):
    return [
        _wave_row(f"{prefix}{i}", [_REAL_TEST_MAPPED_PATHS[i % 2]])
        for i in range(1, n + 1)
    ]


def test_wave_at_threshold_emits_no_marker_split():
    waves = [_large_wave(10)]
    script = compose_script(waves, name="wf", description="at threshold", **REVIEW_KW)

    phase_titles = _extract_phase_titles(script)
    assert phase_titles == ["Execute", "Review prep", "Review wave", "Review integration", "Scoped test run"]


def test_wave_over_threshold_still_a_single_execute_phase():
    waves = [_large_wave(12)]
    script = compose_script(waves, name="wf", description="over threshold", **REVIEW_KW)

    phase_titles = _extract_phase_titles(script)
    assert phase_titles == ["Execute", "Review prep", "Review wave", "Review integration", "Scoped test run"]
    for i in range(1, 13):
        assert f"work:C{i}'" in script


def test_wave_over_threshold_script_passes_run_checks():
    waves = [_large_wave(12)]
    script = compose_script(waves, name="wf", description="round trip", **REVIEW_KW)

    errors = [f for f in run_checks(script) if f.severity is Severity.ERROR]
    assert errors == []


def _one_wave_fixture_with_writes(writes):
    return [[_wave_row("C1", writes)]]


def test_emitted_row_prompt_carries_the_footprint_constraint_over_writes_plus_report():
    """The footprint constraint must be spliced into the emitted SCRIPT text
    (never asserted against the module constant alone -- see
    `test_compose_script_commit_prompt_names_every_measured_false_refusal`'s
    docstring for why a constant-level assertion would stay green through a
    refactor that stopped threading the text into the emitted prompt)."""
    waves = _one_wave_fixture_with_writes(["coordinator_core/ops/dispatch_emit/emit.py"])
    script = compose_script(waves, name="wf", description="one wave", plan_path="docs/plans/example.md", **REVIEW_KW)

    assert "You MUST NOT create or modify any file outside this footprint" in script
    assert "coordinator_core/ops/dispatch_emit/emit.py" in script
    assert ".coordinator-local/subagent-share/dispatch-reports/example/C1.md" in script


def test_emitted_row_prompt_carries_the_self_verify_constraint_naming_emitted_authority():
    """The self-verify clause must name the EMITTED commit/verification
    authority (this run's terminal scoped commit + its per-row `verify:`
    calls) -- never the hand-dispatch "the EM" text, which is false on this
    path, and never a retired per-wave commit phase."""
    waves = _one_wave_fixture_with_writes(["coordinator_core/ops/dispatch_emit/emit.py"])
    script = compose_script(waves, name="wf", description="one wave", plan_path="docs/plans/example.md", **REVIEW_KW)

    assert "leave your changes uncommitted and unstaged" in script
    assert "terminal scoped commit" in script
    assert "dispatch.terminal_commit" in script
    assert "Only the EM commits, once per wave" not in script


def test_emitted_row_prompt_carries_the_done_summary_constraint_with_reply_and_porcelain():
    """The done-summary constraint's structured-reply rule and its
    porcelain changed-path clause must both reach the emitted script,
    scoped to THIS row's own footprint (writes + report path)."""
    waves = _one_wave_fixture_with_writes(["coordinator_core/ops/dispatch_emit/emit.py"])
    script = compose_script(waves, name="wf", description="one wave", plan_path="docs/plans/example.md", **REVIEW_KW)

    expected_report_path = ".coordinator-local/subagent-share/dispatch-reports/example/C1.md"
    assert f"Reply EXACTLY `<STATUS>: {expected_report_path}`" in script
    assert "git status --porcelain -- " in script
    assert "coordinator_core/ops/dispatch_emit/emit.py" in script
    assert expected_report_path in script
    assert "| cut -c4-`" in script


def test_emitted_row_prompt_omits_footprint_constraint_when_writes_undeclared():
    """An UNDECLARED `writes:` is a legal epistemic-premise-gated state,
    distinct from `writes: []` -- the footprint constraint must not render
    at all (never as "you may write nothing"), while the report path still
    threads through the self-verify/porcelain clauses.

    Composed directly through `_row_prompt`/`_row_return_contract`, not
    `compose_script`: a wave whose only row is UNDECLARED never reaches a
    commit phase at all (`pathspec.commit_pathspec` refuses
    `NoWritesDeclaredError` first) -- this is the row-prompt-rendering
    behaviour in isolation, the same UNDECLARED-row shape the module
    docstring calls "a safe placeholder never actually dispatched"."""
    row = _wave_row("C1", UNDECLARED)
    prompt = emit._row_prompt(row, "docs/plans/example.md")

    assert "You MUST NOT create or modify any file outside this footprint" not in prompt
    assert "you may write nothing" not in prompt
    expected_report_path = ".coordinator-local/subagent-share/dispatch-reports/example/C1.md"
    assert expected_report_path in prompt


def test_dispatch_report_path_uses_plan_stem_and_row_id_never_mise_done():
    """The report path is `.coordinator-local/subagent-share/dispatch-
    reports/<plan stem>/<row id>.md`, NOT `tasks/mise-done/` -- see the C3
    row body's three-reason argument."""
    assert (
        emit._dispatch_report_path("docs/plans/example.md", "C7")
        == ".coordinator-local/subagent-share/dispatch-reports/example/C7.md"
    )
    assert "tasks/mise-done" not in emit._dispatch_report_path("docs/plans/example.md", "C7")


def test_dispatch_report_path_is_inside_the_bookkeeping_allowlist():
    """C3 must be safe to land alone: the report path this module renders
    into every row prompt lands under `_DISPATCH_REPORT_DIR`."""
    report_path = emit._dispatch_report_path("docs/plans/example.md", "C1")
    assert report_path.startswith(emit._DISPATCH_REPORT_DIR)


def test_dispatch_report_path_refuses_a_row_id_containing_path_separators():
    """
    -- a row id spliced raw with `../` would pass the allowlist's
    `str.startswith` check while resolving outside `_BOOKKEEPING_PREFIXES`
    on disk. Must refuse loud, never silently sanitize."""
    import pytest

    for bad_id in ("../escape", "a/b", "a\\b", "..", "."):
        with pytest.raises(ValueError):
            emit._dispatch_report_path("docs/plans/example.md", bad_id)


def test_dispatch_report_path_accepts_ordinary_row_ids():
    """The allowlist rewrite
    must still accept every ordinary row-id shape a plan spine writes today."""
    for good_id in ("C1", "c-1", "C_1.a"):
        report_path = emit._dispatch_report_path("docs/plans/example.md", good_id)
        assert report_path == f".coordinator-local/subagent-share/dispatch-reports/example/{good_id}.md"


def test_dispatch_report_path_refuses_windows_hazardous_row_ids():
    """
    `spine_read` validates row-id presence, type and uniqueness only, never
    character shape, so a row id shaped like a drive letter, a leading
    `~`, a trailing dot/space, a control character, or a Windows reserved
    device name (bare or with an extension) reaches this function
    untouched and must be refused here."""
    import pytest

    bad_ids = [
        "C:",
        "~foo",
        "foo.",
        "foo ",
        "foo\x00bar",
        "foo\x01bar",
        "CON",
        "con",
        "CON.md",
        "COM1",
        "LPT1.md",
    ]
    for bad_id in bad_ids:
        with pytest.raises(ValueError):
            emit._dispatch_report_path("docs/plans/example.md", bad_id)


def test_emitted_script_names_the_rows_own_dispatch_report_path():
    """End-to-end: compose a script for a wave whose row writes nothing
    else, and confirm its own dispatch report path reaches the emitted
    script (the terminal commit, not this module, judges divergence now)."""
    waves = _one_wave_fixture_with_writes(["coordinator_core/ops/dispatch_emit/emit.py"])
    script = compose_script(waves, name="wf", description="one wave", plan_path="docs/plans/example.md", **REVIEW_KW)

    report_path = emit._dispatch_report_path("docs/plans/example.md", "C1")
    assert report_path in script


def test_row_prompt_return_contract_is_escaped_via_js_string_literal_not_template_literal():
    """Row prompts (including the return-contract text spliced into them)
    go through `workflow_scaffold._js_string_literal` at the
    `_wave_agent_calls` splice points -- never
    `_escape_for_js_template_literal`, which is reserved for the commit
    phase's own runtime-interpolating prompt. A row prompt containing a
    backtick or `${...}`-shaped substring (as the return contract's
    porcelain clause can, via its literal backticked command) must survive
    as a single-quoted JS string literal, not a template literal."""
    waves = _one_wave_fixture_with_writes(["coordinator_core/ops/dispatch_emit/emit.py"])
    script = compose_script(waves, name="wf", description="one wave", plan_path="docs/plans/example.md", **REVIEW_KW)

    row_prompt = emit._row_prompt(
        _wave_row("C1", ["coordinator_core/ops/dispatch_emit/emit.py"]),
        "docs/plans/example.md",
    )
    literal = _js_string_literal(row_prompt)
    assert literal in expand_shared(script)
    assert f"`{row_prompt}`" not in script


def test_emitted_row_prompt_tells_an_executor_how_to_declare_a_fired_stop_rule():
    """Asserted against the emitted SCRIPT, not the builder's return value,
    for the reason
    `test_emitted_row_prompt_carries_the_footprint_constraint_over_writes_plus_report`
    gives. Why the token exists: `emit._stop_rule_halt_gate`."""
    waves = _one_wave_fixture_with_writes(["coordinator_core/ops/dispatch_emit/emit.py"])
    script = compose_script(
        waves, name="wf", description="one wave", plan_path="docs/plans/example.md", **REVIEW_KW
    )

    assert emit._STOP_RULE_TOKEN in script
    assert "stopping IS the work the row asked for" in script
    assert "status (DONE | BLOCKED | PARTIAL)" in script


def test_withdrawn_and_void_rows_get_their_own_tokens_that_never_halt():
    """A row withdrawn by its own gate, and a conditional row that never armed,
    decided nothing: each declares a token distinct from the halt token, and
    the emitted halt matcher fires on neither."""
    waves = _one_wave_fixture_with_writes(["coordinator_core/ops/dispatch_emit/emit.py"])
    script = compose_script(
        waves, name="wf", description="one wave", plan_path="docs/plans/example.md", **REVIEW_KW
    )

    assert emit._WITHDRAWN_TOKEN in script
    assert emit._VOID_TOKEN in script
    assert len({emit._STOP_RULE_TOKEN, emit._WITHDRAWN_TOKEN, emit._VOID_TOKEN}) == 3

    rx = re.compile(emit._STOP_RULE_JS_RE.strip("/"))
    for token in (emit._WITHDRAWN_TOKEN, emit._VOID_TOKEN):
        assert not rx.search(f"DONE: nothing changed\n{token}: the gate withdrew this row")
        assert not rx.search(f"{token}: arm-A-only under an arm-B measurement")
    helper = emit._run_row_helper_js()
    assert emit._WITHDRAWN_TOKEN not in helper
    assert emit._VOID_TOKEN not in helper


def test_the_stop_rule_gate_is_declared_inside_the_shared_run_row_helper():
    """Placement is the whole design (§ Design D4) -- unified into the ONE
    shared `_runRow` helper, not a per-wave halt gate any more."""
    waves = _one_wave_fixture_with_writes(["coordinator_core/ops/dispatch_emit/emit.py"])
    script = compose_script(
        waves, name="wf", description="one wave", plan_path="docs/plans/example.md", **REVIEW_KW
    )

    assert "STOP RULE" in script
    assert f"{emit._STOP_RULE_TOKEN}[*_]" in script


def test_a_writeless_row_still_carries_the_stop_rule_gate():
    """A row with no writes must not become the hole a stop rule falls
    through -- ``_runRow`` classifies every row identically regardless of
    write-capability."""
    waves = [[_wave_row("C1", [])]]
    script = compose_script(
        waves, name="wf", description="one wave", plan_path="docs/plans/example.md", **REVIEW_KW
    )

    assert f"{emit._STOP_RULE_TOKEN}[*_]" in script


def test_the_stop_rule_pattern_matches_a_declaration_and_not_a_bare_mention():
    r"""The emitted matcher is line-anchored for `_preflight_halt_gate`'s
    reason: the prompt itself carries the token text, so a substring test
    would fail OPEN on an agent quoting its own instructions mid-sentence.

    Evaluated here with Python's `re` rather than a JS runtime -- this repo
    runs no Node for its own work (CLAUDE.md § Runtime conventions). The
    pattern uses only syntax the two engines agree on (alternation, a
    bounded `[*_]{0,2}` repeat, `\s`, `\S`), and the emitted text itself is
    pinned by the placement test above.
    """
    rx = re.compile(emit._STOP_RULE_JS_RE.strip("/"))

    assert rx.search(f'{emit._STOP_RULE_TOKEN}: "if the shape needs a new rule" — it does')
    assert rx.search(f'DONE: report.md\n{emit._STOP_RULE_TOKEN}: the rule fired')
    assert not rx.search(
        f"I read the instruction about {emit._STOP_RULE_TOKEN}: and no rule fired"
    )
    assert not rx.search(f"{emit._STOP_RULE_TOKEN}:")


def test_a_solitary_writes_empty_row_rides_the_marker_as_a_pathless_chunk():
    """A `change_kind: verification` row still dispatches; it wrote nothing, so
    its chunk carries no path and the terminal commit closes the row no-change."""
    from coordinator_core.ops.dispatch_emit.commit_request import parse_marker

    waves = [
        [_wave_row("C1", [])],
        [_wave_row("C2", ["coordinator_core/ops/dispatch_emit/emit.py"])],
    ]
    script = compose_script(waves, name="wf", description="verdict then write", **REVIEW_KW)

    assert "_rows['C1'] = _runRow('C1', []," in script, "the verdict row must still dispatch"
    request = parse_marker(script)
    assert request is not None
    assert [c.id for c in request.chunks] == ["C1", "C2"]


def test_the_verdict_rows_paths_are_absent_from_the_marker():
    """A row that writes nothing carries no path in the terminal-
    commit-request marker."""
    from coordinator_core.ops.dispatch_emit.commit_request import parse_marker

    waves = [
        [_wave_row("C1", [])],
        [_wave_row("C2", ["docs/wiki/dispatch-emit.md"])],
    ]
    script = compose_script(waves, name="wf", description="verdict then write", **REVIEW_KW)
    request = parse_marker(script)
    assert request is not None
    assert [c.id for c in request.chunks] == ["C1", "C2"]
    assert list(request.chunks[1].paths) == ["docs/wiki/dispatch-emit.md"]


def test_compose_script_widens_the_marker_pathspec_with_the_stem_test_candidate():
    """state/bug-backlog/2026-08-26-emitted-wave-commit-legs-are-handed-a-wr-
    c0f443ac1fdb.yaml: a row's `writes:` names only the production module, but
    the ACs require the executor to also write the test covering it, so the
    terminal-commit-request marker's pathspec (widened the same way the
    executor's own verify-scope derivation is) must admit the stem-derived
    test candidate up front."""
    from coordinator_core.ops.dispatch_emit.commit_request import parse_marker

    waves = [[_wave_row("C1", ["coordinator_core/ops/brand_new_thing.py"])]]
    script = compose_script(waves, name="wf", description="one wave", **REVIEW_KW)

    request = parse_marker(script)
    assert request is not None
    assert "coordinator_core/ops/brand_new_thing.py" in request.chunks[0].paths


def test_a_done_with_concerns_reply_answers_its_brief():
    """An executor ending `DONE_WITH_CONCERNS: <path>` in backticks did its
    work; reading it as an unanswered brief halted a run as a dispatch defect."""
    import json
    import re

    pattern = emit._ANY_STATUS_JS_RE[1:-1].replace("\\/", "/")
    reply = json.dumps("Work landed.\n`DONE_WITH_CONCERNS: .coordinator-local/r.md`")
    assert re.search(pattern, reply)


def test_a_done_with_concerns_reply_with_closed_backtick_answers_its_brief():
    """The more common markdown convention closes the
    inline-code span right before the colon (`` `DONE_WITH_CONCERNS`: <path> ``);
    the trailing class must admit a backtick too, or this reproduces the exact
    defect the open-backtick case above was fixed for."""
    import json
    import re

    pattern = emit._ANY_STATUS_JS_RE[1:-1].replace("\\/", "/")
    reply = json.dumps("Work landed.\n`DONE_WITH_CONCERNS`: .coordinator-local/r.md")
    assert re.search(pattern, reply)


def _plan_with_row_body(body_line: str, *, external_gate: str = "", disposition: str = "") -> str:
    return (
        "---\ntitle: fixture\n---\n\n# Fixture\n\n## Tasks\n\n"
        "```yaml plan-tasks\n"
        "- id: C1\n"
        "  title: Do the thing\n"
        "  change_kind: doc-edit\n"
        "  surface: docs/reference/some-thing.md\n"
        "  writes:\n    - docs/reference/some-thing.md\n"
        "  queue_scope: project\n"
        f"  disposition: {disposition or 'open'}\n"
        f"{external_gate}"
        f"  body: |\n    {body_line}\n"
        "```\n"
    )


def test_blocked_prose_with_no_gate_raises_dispatch_gate_violation(tmp_path):
    plan_path = tmp_path / "fixture.md"
    plan_path.write_text(
        _plan_with_row_body("Do not start before confirming the directive is live."),
        encoding="utf-8",
    )
    with pytest.raises(emit.DispatchGateViolation) as excinfo:
        emit.emit_script(plan_path, repo_root=tmp_path, **REVIEW_KW)
    assert "Check B" in str(excinfo.value)
    assert "C1" in str(excinfo.value)


def test_blocked_prose_with_a_gate_does_not_raise(tmp_path):
    gate = (
        "  external_gate:\n"
        "    - owner_repo: coordinator-content-repo\n"
        "      condition: waiting on the directive\n"
        "      requires: landed-work\n"
        "      cleared: true\n"
    )
    plan_path = tmp_path / "fixture.md"
    plan_path.write_text(
        _plan_with_row_body(
            "Do not start before confirming the directive is live.", external_gate=gate
        ),
        encoding="utf-8",
    )
    emit.emit_script(plan_path, repo_root=tmp_path, **REVIEW_KW)


def test_already_happened_prose_with_open_disposition_raises(tmp_path):
    plan_path = tmp_path / "fixture.md"
    plan_path.write_text(
        _plan_with_row_body("The memo was sent already -- for traceability only."),
        encoding="utf-8",
    )
    with pytest.raises(emit.DispatchGateViolation) as excinfo:
        emit.emit_script(plan_path, repo_root=tmp_path, **REVIEW_KW)
    assert "Check B" in str(excinfo.value)


def test_already_happened_prose_with_coded_disposition_does_not_raise(tmp_path):
    """A row already marked `disposition: coded` is excluded from
    `read_spine`'s output before `check_unschedulable_rows` ever sees it, so
    the plan has zero dispatchable rows and `build_waves` refuses instead --
    Check B never fires for a row this engine has already excluded."""
    plan_path = tmp_path / "fixture.md"
    plan_path.write_text(
        _plan_with_row_body(
            "The memo was sent already -- for traceability only.", disposition="coded"
        ),
        encoding="utf-8",
    )
    with pytest.raises(NoWavesError):
        emit.emit_script(plan_path, repo_root=tmp_path, **REVIEW_KW)


def test_gate_discharge_claim_uncleared_raises(tmp_path):
    """DoE parity (2b3cd386e/4537df652): a gate whose condition prose
    declares discharge in shout-case while `cleared` stays unset is refused,
    even though the row body itself carries no contradicting prose."""
    gate = (
        "  external_gate:\n"
        "    - owner_repo: coordinator-content-repo\n"
        "      condition: GATE SATISFIED 2026-09-02\n"
        "      requires: landed-work\n"
    )
    plan_path = tmp_path / "fixture.md"
    plan_path.write_text(
        _plan_with_row_body("Ordinary body prose.", external_gate=gate),
        encoding="utf-8",
    )
    with pytest.raises(emit.DispatchGateViolation) as excinfo:
        emit.emit_script(plan_path, repo_root=tmp_path, **REVIEW_KW)
    assert "Check B" in str(excinfo.value)
    assert "gate" in str(excinfo.value).lower()


def test_gate_discharge_claim_cleared_does_not_raise(tmp_path):
    """The same discharge-claim prose on a gate that DOES carry
    `cleared: true` is not a Check B violation -- the prose and the field
    agree."""
    gate = (
        "  external_gate:\n"
        "    - owner_repo: coordinator-content-repo\n"
        "      condition: GATE SATISFIED 2026-09-02\n"
        "      requires: landed-work\n"
        "      cleared: true\n"
    )
    plan_path = tmp_path / "fixture.md"
    plan_path.write_text(
        _plan_with_row_body("Ordinary body prose.", external_gate=gate),
        encoding="utf-8",
    )
    emit.emit_script(plan_path, repo_root=tmp_path, **REVIEW_KW)


def test_ordinary_prose_does_not_raise(tmp_path):
    """Negative-spec: Check B is deliberately narrow. Ordinary future-
    conditional executor instruction prose ("report BLOCKED rather than...")
    must not fire -- the DoE pattern this class was tuned against."""
    plan_path = tmp_path / "fixture.md"
    plan_path.write_text(
        _plan_with_row_body(
            "If X is genuinely needed, report BLOCKED rather than adding one."
        ),
        encoding="utf-8",
    )
    emit.emit_script(plan_path, repo_root=tmp_path, **REVIEW_KW)


# ---------------------------------------------------------------------------
# Absent edit targets -- a row authored before its surface was deleted
# ---------------------------------------------------------------------------


def _absent_target_plan(tmp_path, *rows: tuple):
    """One spine row per ``(id, change_kind, writes, extra)``."""
    body = ""
    for row_id, kind, writes, *rest in rows:
        body += (
            f"- id: {row_id}\n  title: row {row_id}\n  change_kind: {kind}\n"
            "  surface: pkg\n  writes:\n"
            + "".join(f"    - {w}\n" for w in writes)
            + (rest[0] if rest else "")
        )
    return _plan_with_row(tmp_path, body)


def _emit_findings(plan_path, root):
    out: list = []
    script = emit_script(plan_path, repo_root=root, findings_out=out, **REVIEW_KW)
    return script, out


def test_edit_row_against_an_absent_path_warns_naming_row_path_and_kind(tmp_path):
    plan = _absent_target_plan(tmp_path, ("C1", "code-edit", ["pkg/deleted.py"]))

    _, findings = _emit_findings(plan, tmp_path)

    assert len(findings) == 1
    assert findings[0].severity is Severity.WARN
    assert findings[0].code == emit.ABSENT_EDIT_TARGET_CODE
    assert "C1: pkg/deleted.py" in findings[0].message


def test_many_absent_paths_aggregate_into_one_finding(tmp_path):
    rows = [(f"C{i}", "code-edit", [f"pkg/new_{i}.py"]) for i in range(1, 15)]
    plan = _absent_target_plan(tmp_path, *rows)

    _, findings = _emit_findings(plan, tmp_path)

    assert len(findings) == 1
    assert findings[0].message.startswith("14 ")
    assert "(+4 more)" in findings[0].message


def test_edit_row_against_a_present_path_does_not_warn(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "live.py").write_text("x = 1\n", encoding="utf-8")
    plan = _absent_target_plan(tmp_path, ("C1", "script-edit", ["pkg/live.py"]))

    assert _emit_findings(plan, tmp_path)[1] == []


def test_creating_kinds_do_not_warn_on_an_absent_path(tmp_path):
    plan = _absent_target_plan(
        tmp_path,
        ("C1", "wiki-new", ["pkg/new-page.md"]),
        ("C2", "test-edit", ["pkg/test_new.py"]),
    )

    assert _emit_findings(plan, tmp_path)[1] == []


def test_a_later_row_editing_what_an_earlier_row_creates_is_not_flagged(tmp_path):
    plan = _absent_target_plan(
        tmp_path,
        ("C1", "wiki-new", ["pkg/made.py"]),
        ("C2", "code-edit", ["pkg/made.py"], "  depends_on:\n    - chunk: C1\n      gate_kind: output-consumption-runtime\n"),
    )

    assert _emit_findings(plan, tmp_path)[1] == []


def test_absent_target_finding_never_refuses_the_emit(tmp_path):
    plan = _absent_target_plan(tmp_path, ("C1", "code-edit", ["pkg/deleted.py"]))

    script, findings = _emit_findings(plan, tmp_path)

    assert findings and script
    assert_zero_errors(script)


def test_no_repo_root_or_no_sink_is_a_no_op(tmp_path):
    plan = _absent_target_plan(tmp_path, ("C1", "code-edit", ["pkg/deleted.py"]))

    assert _emit_findings(plan, None)[1] == []
    assert emit_script(plan, repo_root=tmp_path, **REVIEW_KW)


def test_dispatch_emit_reply_carries_the_absent_target_warn(tmp_path):
    from coordinator_core.ops.dispatch_emit.op import _dispatch_emit

    plan = _absent_target_plan(tmp_path, ("C1", "code-edit", ["pkg/deleted.py"]))
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    reply = _dispatch_emit(
        {"plan_path": str(plan), "output_path": str(out_dir / "e.mjs"), "target_root": str(tmp_path)},
        repo_root=tmp_path,
    )

    hits = [f for f in reply["findings"] if f["code"] == emit.ABSENT_EDIT_TARGET_CODE]
    assert len(hits) == 1
    assert reply["ok"] is True
    assert reply["warn_count"] >= 1


# ---------------------------------------------------------------------------
# Import-window rows -- a new module and its existing importer in one row
# ---------------------------------------------------------------------------


def _import_window_findings(tmp_path, writes, importer_src, *, importer="pkg/user.py"):
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / importer).write_text(importer_src, encoding="utf-8")
    plan = _absent_target_plan(tmp_path, ("C1", "code-edit", writes))
    _, findings = _emit_findings(plan, tmp_path)
    return [f for f in findings if f.code == emit.IMPORT_WINDOW_CODE]


@pytest.mark.parametrize(
    "src",
    [
        "from pkg.fresh import thing\n",
        "import pkg.fresh\n",
        "from pkg import fresh\n",
        "from . import fresh\n",
        "from .fresh import thing\n",
        "def f():\n    from pkg.fresh.sub import thing\n",
        "from pkg import (\n    other,\n    fresh,\n)\n",
    ],
)
def test_row_writing_a_new_module_and_its_importer_warns(tmp_path, src):
    hits = _import_window_findings(tmp_path, ["pkg/fresh.py", "pkg/user.py"], src)

    assert len(hits) == 1
    assert hits[0].severity is Severity.WARN
    assert "C1: pkg/user.py imports new pkg/fresh.py" in hits[0].message
    assert "depends_on" in hits[0].message


def test_new_package_init_counts_as_the_module(tmp_path):
    hits = _import_window_findings(
        tmp_path, ["pkg/newpkg/__init__.py", "pkg/user.py"], "import pkg.newpkg\n"
    )

    assert len(hits) == 1


def test_importer_not_importing_the_new_module_does_not_warn(tmp_path):
    hits = _import_window_findings(
        tmp_path, ["pkg/fresh.py", "pkg/user.py"], "from pkg.other import x\nimport pkg.freshly\n"
    )

    assert hits == []


def test_existing_module_is_not_new(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "fresh.py").write_text("x = 1\n", encoding="utf-8")

    hits = _import_window_findings(
        tmp_path, ["pkg/fresh.py", "pkg/user.py"], "from pkg.fresh import x\n"
    )

    assert hits == []


def test_existing_test_module_importing_the_new_module_does_not_warn(tmp_path):
    hits = _import_window_findings(
        tmp_path,
        ["pkg/fresh.py", "pkg/test_user.py"],
        "from pkg.fresh import thing\n",
        importer="pkg/test_user.py",
    )

    assert hits == []


def test_new_module_and_importer_in_separate_rows_do_not_warn(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "user.py").write_text("from pkg.fresh import x\n", encoding="utf-8")
    plan = _absent_target_plan(
        tmp_path,
        ("C1", "code-edit", ["pkg/fresh.py"]),
        (
            "C2",
            "code-edit",
            ["pkg/user.py"],
            "  depends_on:\n    - chunk: C1\n      gate_kind: output-consumption-runtime\n",
        ),
    )

    _, findings = _emit_findings(plan, tmp_path)

    assert [f for f in findings if f.code == emit.IMPORT_WINDOW_CODE] == []


def test_unparseable_importer_is_skipped_and_the_warn_never_refuses(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "user.py").write_text("def broken(:\n", encoding="utf-8")
    plan = _absent_target_plan(tmp_path, ("C1", "code-edit", ["pkg/fresh.py", "pkg/user.py"]))

    script, findings = _emit_findings(plan, tmp_path)

    assert [f for f in findings if f.code == emit.IMPORT_WINDOW_CODE] == []
    assert_zero_errors(script)


def test_import_window_warn_is_a_no_op_without_a_repo_root(tmp_path):
    assert emit.find_import_window_rows([], None) == []


@pytest.mark.parametrize(
    "params, needle",
    [
        ("not-a-dict", "must be an object"),
        ({"plan_path": 7}, "plan_path must be a string"),
        ({"plan_path": "p.md", "target_root": ["x"]}, "target_root must be a string"),
        ({"queue": "dir", "profile": "p"}, "queue must be a list"),
        ({"plan_path": "p.md", "overrides": []}, "overrides must be an object"),
    ],
)
def test_dispatch_emit_refuses_malformed_request_without_raising(params, needle):
    from coordinator_core.ops.dispatch_emit.op import _dispatch_emit

    out = _dispatch_emit(params)
    assert isinstance(out, dict)
    assert needle in out["error"], out
