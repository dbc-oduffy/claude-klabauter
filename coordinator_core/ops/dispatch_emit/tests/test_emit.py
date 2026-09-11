"""
Tests for coordinator_core.ops.dispatch_emit.emit.

Spec backlink: pln-the-emitter-turns-a-plan-spine-d08dda § C4.
"""

from __future__ import annotations

import re
import subprocess as _subprocess
import textwrap

import pytest

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit.emit import (
    MalformedAgentOverrideError,
    MixedAgentTypeRowError,
    NoWavesError,
    ReviewRosterFragmentError,
    assert_zero_errors,
    compose_script,
    derive_review_tier,
    emit_script,
)
from coordinator_core.ops.dispatch_emit.pathspec import NoWritesDeclaredError, commit_pathspec
from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED, read_spine
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow, build_waves
from coordinator_core.ops.workflow_scaffold import _js_string_literal

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
# compose_script — top-level body, never an uninvoked wrapper (BREAK-CLASS)
# ---------------------------------------------------------------------------


def test_composed_script_never_wraps_body_in_an_uninvoked_run_function():
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="two waves")

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
    script = compose_script(waves, name="wf", description="two waves")

    meta_end = script.index("};\n") + len("};\n")
    remainder = script[meta_end:].lstrip()
    assert remainder.startswith("const _incompleteChunks = [];")
    after_decl = remainder[len("const _incompleteChunks = [];") :].lstrip()
    assert after_decl.startswith("phase(")


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


def test_plan_body_write_row_derives_enricher_agent_type():
    waves = [
        [
            _wave_row("C1", ["docs/plans/2026-08-13-example.md"]),
            _wave_row("C2", ["coordinator_core/ops/dispatch_emit/spine_read.py"]),
        ]
    ]
    script = compose_script(waves, name="wf", description="plan body row")
    assert "agentType: 'coordinator:enricher'" in script
    assert script.count("agentType: 'coordinator:executor'") == 1


def test_ordinary_code_row_still_derives_executor_agent_type():
    waves = [[_wave_row("C1", ["coordinator_core/ops/dispatch_emit/spine_read.py"])]]
    script = compose_script(waves, name="wf", description="code row")
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
        compose_script(waves, name="wf", description="mixed row")


def test_problem_set_write_row_derives_enricher_agent_type():
    waves = [
        [
            _wave_row("C1", ["docs/problems/2026-08-13-example.md"]),
            _wave_row("C2", ["coordinator_core/ops/dispatch_emit/spine_read.py"]),
        ]
    ]
    script = compose_script(waves, name="wf", description="problem-set row")
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
        compose_script(waves, name="wf", description="mixed problem-set row")


def test_undeclared_writes_row_propagates_no_writes_declared_before_agent_type_matters():
    # UNDECLARED never reaches agentType derivation in a real run: pathspec's
    # commit_pathspec refuses it first (NoWritesDeclaredError). Regression
    # guard for that ordering -- see _row_agent_type's UNDECLARED docstring.
    waves = [[_wave_row("C1", UNDECLARED)]]
    with pytest.raises(NoWritesDeclaredError):
        compose_script(waves, name="wf", description="undeclared row")


# ---------------------------------------------------------------------------
# agent_type / agent_model spine overrides
# (state/sizings/2026-09-05-a-plan-row-can-name-the-agent-that-runs.yaml)
# ---------------------------------------------------------------------------


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
        compose_script(waves, name="wf", description="malformed agent_type")


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
        compose_script(waves, name="wf", description="malformed agent_model")


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
        compose_script(waves, name="wf", description="mixed row, explicit agent_type")


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
        compose_script(waves, name="wf", description="unmodellable agent_type alone")


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
    script = compose_script(waves, name="wf", description="unmodellable agent_type with model")
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
    script = compose_script(waves, name="wf", description="agent_model escaping")
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
    script = compose_script(waves, name="wf", description="spine-sourced override")

    assert "agentType: 'coordinator:workflow-maker'" in script
    assert "model: 'opus'" in script


def test_spine_text_with_neither_agent_key_emits_byte_identically(tmp_path):
    """A spine declaring neither key must still emit byte-identically to the
    pre-existing hand-built ``WaveRow`` path -- the negative-spec twin of
    the positive case above, and an actual byte-for-byte comparison rather
    than only substring assertions (the name's original unmet promise;
    Review: overengineering-reviewer -- the prior ``_plan_text`` helper's
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
        build_waves(read_spine(plan_path)), name="wf", description="no overrides"
    )
    script_from_hand_built = compose_script(
        [[_wave_row("C1", ["coordinator_core/ops/dispatch_emit/spine_read.py"])]],
        name="wf",
        description="no overrides",
    )

    assert script_from_spine == script_from_hand_built
    assert "agentType: 'coordinator:workflow-maker'" not in script_from_spine
    assert "agentType: 'coordinator:executor'" in script_from_spine
    assert "model: 'opus'" not in script_from_spine


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


def test_preflight_call_carries_the_commit_agents_charter_model():
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="two waves")
    body_start = script.index("};\n") + len("};\n")  # end of the meta block
    preflight_block_start = script.index("Preflight: commit claimability", body_start)
    preflight_block_end = script.index("Wave 1", body_start)
    preflight_block = script[preflight_block_start:preflight_block_end]
    assert "model: 'haiku'" in preflight_block


def test_preflight_call_binds_its_result_and_gates_the_run():
    # Before this gate, `await agent(...)` discarded the preflight verdict
    # and every later phase followed unconditionally, even a BLOCKED report.
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")
    assert "const preflightResult = await agent(" in script
    assert "if (/^[*_]{0,2}PREFLIGHT-BLOCKED" in script
    assert "return { halted:" in script


def test_preflight_blocked_token_match_is_anchored_not_substring():
    import re

    from coordinator_core.ops.dispatch_emit.emit import _preflight_halt_gate

    gate = _preflight_halt_gate("preflightResult", "Preflight: commit claimability")
    match = re.search(r"if \((/.*/m)\.test\(String\(preflightResult", gate)
    assert match is not None
    pattern = match.group(1)[1:-2]  # strip leading "/" and trailing "/m"

    # A quotation of the prompt's own instruction text -- carrying the
    # literal token mid-sentence, never at line-start -- must NOT match.
    quoting_prompt_back = (
        "Every path is claimable. (The instructions said to end my report "
        "with the line 'PREFLIGHT-BLOCKED <reason>' if any path were "
        "refused, but none was, so I am not doing that.)"
    )
    assert re.search(pattern, quoting_prompt_back, re.MULTILINE) is None

    # A genuine BLOCKED verdict, token anchored at line start, must match.
    genuine_blocked = (
        "Checked every path.\nPREFLIGHT-BLOCKED some/path.py refused by claim conflict"
    )
    assert re.search(pattern, genuine_blocked, re.MULTILINE) is not None


def _clear_gate_pattern():
    """The CLEAR arm's regex, lifted out of the emitted JS."""
    import re

    from coordinator_core.ops.dispatch_emit.emit import _preflight_halt_gate

    gate = _preflight_halt_gate("preflightResult", "Preflight: commit claimability")
    match = re.search(r"if \(!(/.*/m)\.test\(String\(preflightResult", gate)
    assert match is not None, "no negated CLEAR gate in the emitted JS"
    return match.group(1)[1:-2]  # strip leading "/" and trailing "/m"


def test_preflight_silence_is_not_a_pass():
    """The pass verdict must be a POSITIVE token, never the absence of output.

    Until the CLEAR gate existed, the prompt told the agent not to emit a line
    when everything was claimable, so a clean preflight and a preflight that
    never ran were the same signal -- and `String(x ?? "")` collapsed a null
    result, a crashed agent, and a zero-tool-call agent onto it.
    """
    import re

    pattern = _clear_gate_pattern()
    # Each of these must FAIL the CLEAR pattern, so the negated gate halts.
    for report in (
        "",  # agent returned null -> String(null ?? "") === ""
        "agent returned null",
        "Every path is claimable.",  # fluent, plausible, carries no evidence
        "Checked all 12 paths. No claim conflicts, no ignore rules, no guards.",
    ):
        assert re.search(pattern, report, re.MULTILINE) is None, report


def test_preflight_clear_cannot_be_satisfied_by_quoting_the_prompt():
    """Same anti-fabrication device the commit gate uses: a real hex sha.

    The prompt carries the literal placeholder ``<sha>``, which is not hex
    however it is decorated, so echoing the instruction back cannot pass.
    """
    import re

    pattern = _clear_gate_pattern()
    quoting_prompt_back = (
        "All paths are claimable. The instructions said to end my report with "
        "the line 'PREFLIGHT-CLEAR <sha>' carrying the sha from git rev-parse "
        "HEAD, so: PREFLIGHT-CLEAR <sha>"
    )
    assert re.search(pattern, quoting_prompt_back, re.MULTILINE) is None


def test_preflight_clear_accepts_a_real_sha_bare_or_emphasised():
    import re

    pattern = _clear_gate_pattern()
    for report in (
        "Checked every path.\nPREFLIGHT-CLEAR f990938d67",
        "Checked every path.\nPREFLIGHT-CLEAR f990938d67ab12cd34ef5678901234567890abcd",
        "Checked every path.\n**PREFLIGHT-CLEAR f990938d67**",
    ):
        assert re.search(pattern, report, re.MULTILINE) is not None, report


def test_preflight_prompt_asks_for_the_sha_the_clear_gate_requires():
    """The gate is unsatisfiable unless the prompt names how to get a sha."""
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")
    assert "git rev-parse HEAD" in script
    assert "PREFLIGHT-CLEAR <sha>" in script


def test_preflight_blocked_still_wins_over_clear():
    """A BLOCKED report halts with the blocker reason, not the no-verdict one."""
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")
    blocked_gate = script.index("PREFLIGHT-BLOCKED[*_]")
    clear_gate = script.index("!/^[*_]{0,2}PREFLIGHT-CLEAR")
    assert blocked_gate < clear_gate


# ---------------------------------------------------------------------------
# Per-hunk divergence: a pathspec scopes files, not changes
# ---------------------------------------------------------------------------


def _provenance_block():
    from coordinator_core.ops.dispatch_emit.emit import _PROVENANCE_HEADING

    return _PROVENANCE_HEADING


def test_provenance_no_longer_claims_peer_work_cannot_reach_the_commit():
    """The old reassurance was true per-FILE and false per-HUNK, and it is
    exactly the sentence that would stop an agent from looking.

    `commit_paths` commits working-tree state for every path handed to it, so
    a peer's uncommitted edit inside a DECLARED path lands under this run's
    subject with every guard passing. Measured at `ef3bbb1663`.
    """
    block = _provenance_block()
    assert "cannot sweep peer-staged work into your commit" not in block
    assert "PER-FILE, NOT PER-HUNK" in block


def test_provenance_requires_reading_the_diff_not_only_the_reports():
    block = _provenance_block()
    assert "git diff --stat" in block
    assert "Read the diff, not just the reports" in block


def test_the_halt_discriminator_is_a_file_no_report_mentions():
    """Halting on any unreported hunk would fire on ordinary under-itemised
    executor reports; halting on none leaves the peer case silent."""
    block = _provenance_block()
    assert "NO report mentions at all is the peer case" in block
    # The non-halting arm is stated explicitly, or the agent will over-halt.
    assert "do not halt on those" in block


def test_the_remedy_is_stop_and_report_never_clean_the_path():
    """Reverting a peer's hunk is the destructive failure this must not invite."""
    block = _provenance_block()
    assert "Never revert, stash, or check out a hunk" in block


def test_verification_must_produce_an_artifact_it_cannot_fabricate():
    """The measured failure mode is an agent that reasons from pasted reports
    and produces output indistinguishable from one that checked -- and whose
    presentation is anti-correlated with having checked. Requiring verbatim
    stat output makes the two modes produce different artifacts."""
    block = _provenance_block()
    assert "DIFF OBSERVED:" in block
    assert "VERBATIM" in block
    assert "do not build a nicer artifact" in block
    assert "did not look" in block


def test_the_per_hunk_clause_reaches_the_emitted_script():
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")
    # Only waves carrying executor reports get the provenance block at all.
    if "Pathspec provenance" in script:
        assert "PER-FILE, NOT PER-HUNK" in script


# ---------------------------------------------------------------------------
# C4: structured reports narrow the commit phase, per-report, diff still governs
# ---------------------------------------------------------------------------


def test_provenance_reconciles_structured_reports_per_report_not_per_wave():
    """A mixed wave -- some structured reports, some prose -- must reconcile
    each report on its own rules, never treat any structured subset as
    licence to skip reconciling the rest of the wave."""
    block = _provenance_block()
    assert "PER-REPORT, NEVER PER-WAVE" in block
    assert "must never suppress reconciliation of the other two" in block


def test_provenance_structured_claim_never_replaces_the_diff_check():
    """A structured changed-path list may narrow what the commit agent
    EXPECTS to see in `git diff --stat`; it must never be read as licensing
    a reports-only derivation of the pathspec -- the exact posture the
    'read the diff, not just the reports' paragraph exists to kill."""
    block = _provenance_block()
    assert "NARROWS WHAT YOU EXPECT" in block
    assert "NEVER REPLACES THE" in block
    assert "NOT a substitute for reading the" in block


def test_provenance_reconciles_structured_reports_per_report_reaches_the_emitted_script():
    """Review: coordinator:code-reviewer, finding P2 -- the constant-level
    assertion above stays green even if a refactor stops threading this
    clause through to the emitted script; pin the reaching leg too, the
    same idiom `test_the_per_hunk_clause_reaches_the_emitted_script` uses."""
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")
    if "Pathspec provenance" in script:
        assert "PER-REPORT, NEVER PER-WAVE" in script


def test_provenance_structured_claim_never_replaces_the_diff_check_reaches_the_emitted_script():
    """Review: coordinator:code-reviewer, finding P2 -- same reaching-the-
    emitted-script leg for the diff-still-governs clause."""
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")
    if "Pathspec provenance" in script:
        assert "NARROWS WHAT YOU EXPECT" in script
        assert "NEVER REPLACES THE" in script


def test_provenance_partial_wave_clause_unmodified():
    """The `abf69cd326` partial-wave clause is additive-only: this chunk must
    not touch it."""
    block = _provenance_block()
    assert "A PARTIAL WAVE STILL COMMITS" in block
    assert (
        "Refusing the whole wave because one item of N is blocked is the "
        "failure mode, not the safe choice."
    ) in block


# ---------------------------------------------------------------------------
# Cached-preflight staleness detection
# ---------------------------------------------------------------------------


def test_preflight_sha_is_bound_for_later_phases():
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")
    assert "const preflightHeadSha = " in script
    # Parsed out of the agent's own report, not assumed.
    assert "PREFLIGHT-CLEAR[*_ ]+([0-9a-f]{7,40})" in script


def test_every_commit_phase_carries_the_staleness_check():
    """The check belongs at the commit agent because that is the last step
    before anything is written AND the only actor in the run that can run git.
    """
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")
    assert script.count("STALENESS CHECK") == 2  # one per commit phase
    assert script.count("${preflightHeadSha") == 2
    assert "git rev-parse HEAD" in script


def test_staleness_clause_interpolates_rather_than_printing_source():
    """`_escape_for_js_template_literal` neutralises `$` on purpose, so a
    `${...}` written into the prompt would reach the agent as literal text."""
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")
    assert "<<<PREFLIGHT_HEAD_SHA>>>" not in script  # placeholder consumed
    assert "\\${preflightHeadSha" not in script  # not escaped into source text


def test_escaper_still_neutralises_other_dollars_in_prompt_text():
    """The splice is one known token to one known binding, never a general
    unescaping -- otherwise prompt text could inject expressions."""
    from coordinator_core.ops.dispatch_emit.emit import (
        _escape_for_js_template_literal,
        _interpolate_preflight_sha,
    )

    hostile = "cost was ${process.env.SECRET} and ${alert(1)}"
    out = _interpolate_preflight_sha(_escape_for_js_template_literal(hostile))
    # A plain `"${x}" not in out` would PASS on unescaped output too, since
    # the escaped form `\${x}` contains it as a substring. Assert on what
    # actually matters: every `${` is preceded by a backslash, so none of them
    # is a live interpolation.
    unescaped = [
        m.start()
        for m in re.finditer(r"\$\{", out)
        if m.start() == 0 or out[m.start() - 1] != "\\"
    ]
    assert not unescaped, out


def test_a_mismatch_is_not_framed_as_a_refusal_reason():
    """A resumed run replaying a cached verdict is EXPECTED. Framing the
    mismatch as grounds to refuse would halt every legitimate resume."""
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")
    i = script.index("STALENESS CHECK")
    clause = script[i:i + 900]
    assert "is NOT by itself a reason to refuse" in clause
    assert "Re-verify claimability" in clause


# ---------------------------------------------------------------------------
# Terminal completion return
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Gitignored writes excluded from preflight/commit pathspecs
# ---------------------------------------------------------------------------

_NOWIN = {"creationflags": getattr(_subprocess, "CREATE_NO_WINDOW", 0)}


@pytest.mark.spawns_process
@pytest.mark.cadence
def _git(repo, *args, check=True):
    return _subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check, **_NOWIN
    )


def _gitignore_repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work/z")
    _git(repo, "config", "user.email", "t@local")
    _git(repo, "config", "user.name", "t")
    (repo / ".gitignore").write_text("registry/registry.db\n", encoding="utf-8")
    (repo / "registry").mkdir()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


@pytest.mark.spawns_process
def test_a_gitignored_write_is_excluded_from_the_preflight_pathspec(tmp_path):
    """Defect: a spine row declaring a gitignored `writes:` path (a derived
    store, e.g. `registry/registry.db`) went PREFLIGHT-BLOCKED before wave 1
    -- an ignored path can never be committed, so it must never reach the
    preflight claimability set at all.
    """
    repo = _gitignore_repo(tmp_path)
    waves = [[_wave_row("C1", ["registry/registry.db"])]]
    script = compose_script(waves, name="wf", description="ignored write", repo_root=repo)
    preflight_body = _preflight_body(script)
    assert "registry/registry.db" not in preflight_body


@pytest.mark.spawns_process
def test_a_wave_whose_only_write_is_gitignored_gets_no_commit_phase(tmp_path):
    """Same treatment as an all-`writes: []` wave: nothing committable, so
    no commit phase is emitted for it."""
    repo = _gitignore_repo(tmp_path)
    waves = [[_wave_row("C1", ["registry/registry.db"])]]
    script = compose_script(waves, name="wf", description="ignored write", repo_root=repo)
    assert "commit phase omitted" in script
    assert "gitignored" in script
    # Nothing after the wave's own body `phase('Wave 1: C1')` call names the
    # commit agent -- the preflight phase (before it) legitimately does.
    wave_body = script[script.index("phase('Wave 1: C1')") :]
    assert "coordinator:git-commit-agent" not in wave_body


@pytest.mark.spawns_process
def test_a_gitignored_write_alongside_a_real_one_still_commits_the_real_path(tmp_path):
    repo = _gitignore_repo(tmp_path)
    waves = [[_wave_row("C1", ["registry/registry.db", "a.py"])]]
    script = compose_script(waves, name="wf", description="mixed write", repo_root=repo)
    preflight_body = _preflight_body(script)
    assert "registry/registry.db" not in preflight_body
    assert "a.py" in preflight_body
    assert "commit phase omitted" not in script


def _preflight_body(script: str) -> str:
    """The preflight phase's own `agent()` call text -- the segment between
    its result binding and the next phase's body `phase(...)` call. The
    phase TITLE `Preflight: commit claimability` also appears earlier, in
    `meta.phases`, so slicing from there (rather than from the result
    binding) would include the unrelated meta-block literal too."""
    start = script.index("const preflightResult")
    end = script.index("phase('Wave 1", start)
    return script[start:end]


def test_completed_run_returns_a_positive_record_not_undefined():
    """A finished run and a run that fell off the end must not look alike.

    The body used to end on `await agent(...)`, so a completed run returned
    `undefined` -- indistinguishable from a script that never reached its
    last phase. Same shape as the preflight's absent-output pass verdict.
    """
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")
    assert "completed: _incompleteChunks.length === 0" in script
    assert script.rstrip().endswith("};")


def test_completion_record_names_the_chunks_the_recovery_triple_greps_for():
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")
    tail = script[script.index("completed: _incompleteChunks.length === 0"):]
    assert "chunks: [" in tail
    assert "waves: 2" in tail
    # Every chunk in the fixture is named, because chunk ids are the join key
    # `git log --grep '<chunk-id>:'` uses.
    for wave in _two_wave_fixture():
        for row in wave:
            assert f"'{row.id}'" in tail, row.id


def test_completion_return_is_last_so_no_phase_follows_it():
    """An early completion return would silently skip the phases after it."""
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")
    completion = script.index("completed: _incompleteChunks.length === 0")
    assert "phase(" not in script[completion:]
    assert "await agent(" not in script[completion:]


def test_a_non_done_chunk_report_flips_completed_false_and_names_the_chunk():
    """Defect: a workflow reported `completed: true` while a chunk's own
    agent report carried `Status: PARTIAL`. Runtime behaviour is not
    executable from a compose-time test (there is no JS runtime here), so
    this pins the STRUCTURE the fix composes: a status-check block per
    batch feeding a shared `_incompleteChunks` array that the terminal
    return's `completed` reads, never a hard-coded `true`.
    """
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")
    assert "const _incompleteChunks = [];" in script
    # Every fixture wave is single-row, so each gets the single-row push
    # form, keyed to that row's own results binding -- never a single global
    # check blind to which chunk actually reported.
    for wave in _two_wave_fixture():
        for row in wave:
            assert f"_incompleteChunks.push('{row.id}')" in script, row.id
    assert "incomplete_chunks: _incompleteChunks" in script


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


def test_multi_row_wave_status_check_indexes_by_row_order():
    """A parallel wave's results binding is an array in row-dispatch order;
    the status check must index it per row, never blind-push every row id
    on any match (which would misattribute a sibling's PARTIAL)."""
    waves = [[_wave_row("C1", ["a.py"]), _wave_row("C2", ["b.py"])]]
    script = compose_script(waves, name="wf", description="one parallel wave")
    assert "['C1', 'C2'].forEach((id, i) => {" in script
    assert "wave1Results[i]" in script


# ---------------------------------------------------------------------------
# model: 'sonnet' on every agent() call (AC11) — WARN-tier, not caught by AC5
# ---------------------------------------------------------------------------


def test_commit_and_test_calls_carry_charter_haiku_while_waves_stay_sonnet():
    """A call-site ``model:`` OVERRIDES the named agent definition's own
    frontmatter, so the emitted tier must track each agentType's charter
    rather than one constant. ``git-commit-agent`` and ``test-runner`` are
    haiku by charter; a blanket sonnet billed a Sonnet for mechanical staging
    and test invocation. Negative spec for `_model_opt`.
    """
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="tier check")

    def opts_for(label):
        idx = script.index(label)
        return script[idx:script.index("})", idx)]

    assert "model: 'haiku'" in opts_for("commit:wave-1")
    assert "model: 'sonnet'" not in opts_for("commit:wave-1")

    test_label_idx = script.find("agentType: 'coordinator:test-runner'")
    assert test_label_idx != -1, "fixture must compose the terminal test phase"
    assert "model: 'haiku'" in script[test_label_idx:script.index("})", test_label_idx)]

    assert "model: 'sonnet'" in opts_for("work:"), (
        "executor waves keep their charter sonnet - only the mechanical "
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
    script = compose_script(waves, name="wf", description="model check")

    agent_call_count = len(_AGENT_CALL_RE.findall(script))
    model_count = script.count("model: '")

    assert agent_call_count >= 4  # 2 wave-1 + 1 wave-2 + 2 commit + 1 test
    assert model_count == agent_call_count, (
        "every agent() call site must carry an active model: - "
        f"found {agent_call_count} agent() calls but {model_count} "
        "model: opts entries"
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


# ---------------------------------------------------------------------------
# All-empty-writes wave: no commit phase, not a whole-plan refusal
# (state/bug-backlog/2026-09-11-an-all-empty-writes-wave-sinks-the-whole-emit.yaml)
# ---------------------------------------------------------------------------


def test_all_empty_writes_wave_emits_with_no_commit_phase():
    waves = [[_wave_row("C1", []), _wave_row("C2", [])]]
    script = compose_script(waves, name="wf", description="all-empty wave")

    # No refusal: the wave's agent calls still emit.
    assert _AGENT_CALL_RE.search(script) is not None
    # No commit phase title for this wave.
    titles = _extract_phase_titles(script)
    assert not any("Commit" in title for title in titles)
    # The omission is stated in the emitted script, not silently absent.
    assert "commit phase omitted" in script
    assert "writes: []" in script


def test_all_empty_writes_wave_contributes_nothing_to_the_preflight_pathspec():
    waves = [[_wave_row("C1", []), _wave_row("C2", [])]]
    script = compose_script(waves, name="wf", description="all-empty wave")

    # An all-empty wave contributes zero paths to the preflight union, so
    # the preflight prompt's rendered pathspec is the empty list.
    assert "every path in []" in script


def test_all_empty_writes_wave_still_dispatches_its_agent_calls():
    waves = [[_wave_row("C1", [])]]
    script = compose_script(waves, name="wf", description="all-empty wave")
    assert "C1" in script


def test_mixed_writes_wave_is_unaffected_by_the_all_empty_branch():
    # Staff review finding P0-1: SOME rows `writes: []` alongside a real
    # contributor still warns-and-continues via commit_pathspec, unchanged --
    # it must not be folded into the new all-empty branch, so a commit phase
    # still emits for this wave.
    waves = [
        [
            _wave_row("C1", []),
            _wave_row("C2", ["coordinator_core/ops/dispatch_emit/wave_map.py"]),
        ]
    ]
    script = compose_script(waves, name="wf", description="mixed wave")
    titles = _extract_phase_titles(script)
    assert any("Commit" in title for title in titles)
    assert "commit phase omitted" not in script


def test_all_undeclared_wave_still_raises_not_folded_into_all_empty_branch():
    # Refusal 1 (every row UNDECLARED) must keep raising -- it is not the
    # same shape as every row explicitly declaring `writes: []`.
    waves = [[_wave_row("C1", UNDECLARED, surface="dispatch_emit")]]
    with pytest.raises(NoWritesDeclaredError):
        compose_script(waves, name="wf", description="all-undeclared")


def test_commit_pathspec_directly_still_refuses_an_all_empty_wave():
    # commit_pathspec itself is unchanged -- it keeps refusing an all-empty
    # wave; only compose_script now avoids asking it the question.
    waves = [_wave_row("C1", []), _wave_row("C2", [])]
    with pytest.raises(NoWritesDeclaredError):
        commit_pathspec(waves)


def test_compose_script_no_longer_propagates_no_test_target_but_degrades_loudly():
    # NoTestTargetError used to veto the whole emit. It now degrades: absent
    # a falsifier, compose_script composes rung 3 -- a loud narration naming
    # the unmapped path -- instead of raising. See empty-terminal-test-scope-
    # degrades-not-vetoes.
    waves = [[_wave_row("C1", ["coordinator_core/ops/dispatch_emit/nonexistent_module.py"])]]
    script = compose_script(waves, name="wf", description="uncovered module")
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
        waves, name="wf", description="uncovered module", falsifier=falsifier
    )
    assert "Scoped test run" in script
    assert "coordinator:test-runner" in script
    assert "run the migration and check the flag" in script
    assert "the flag reads enabled" in script
    assert "the flag reads disabled" in script
    assert "No terminal test phase" not in script


def test_compose_script_omits_the_terminal_phase_for_a_prose_only_spine():
    # The composer, not the scope derivation, is where the original gap
    # lived: the terminal test phase was appended unconditionally, so a wave
    # with no runnable target was unrepresentable and the derivation had to
    # refuse in order to defend that invariant.
    waves = [[_wave_row("C1", ["coordinator_core/subagent_sandbox/CONTRACT.md"])]]
    script = compose_script(waves, name="wf", description="doc only")
    assert "Scoped test run" not in script
    assert "coordinator:test-runner" not in script


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
    # Absence must be reported as absence. A silently missing phase is
    # indistinguishable from a phase that ran and passed, which is the exact
    # false-green the original refusal was protecting against.
    waves = [[_wave_row("C1", ["coordinator_core/subagent_sandbox/CONTRACT.md"])]]
    script = compose_script(waves, name="wf", description="doc only")
    assert "No terminal test phase" in script
    assert "declared, not a pass" in script
    # A log() line, never a phase or an agent dispatch.
    assert "log(" in script


def test_compose_script_still_composes_the_terminal_phase_for_a_mixed_spine():
    # A wave with any resolved target keeps the real phase -- the omission is
    # never a "mostly docs" judgment.
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
    script = compose_script(waves, name="wf", description="mixed")
    assert "Scoped test run" in script
    assert "No terminal test phase" not in script


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


# ---------------------------------------------------------------------------
# (a) Review phases -- tier derivation + fixture roster fragment composition
# ---------------------------------------------------------------------------


_FIXTURE_ROSTER_FRAGMENT = {
    "schema": "review-roster-fragment",
    "tiers": {
        "lightweight": ["coordinator:code-reviewer"],
        "standard": ["coordinator:code-reviewer", "coordinator:integrator"],
        "full": [
            "coordinator:code-reviewer",
            "coordinator:integrator",
            "coordinator:staff-reviewer",
        ],
    },
}


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
    # (Review: code-reviewer S4-dispatch-emit, P2 finding 1 -- `../` is not
    # normalized by `Path.__truediv__`; a citation traversing outside
    # `repo_root` must be rejected, not silently resolved.)
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
    # (Review: code-reviewer S4-dispatch-emit, P2 finding 1 -- an absolute
    # `cited` makes `root / cited` discard `root` entirely per pathlib
    # semantics; it must not be silently followed outside `repo_root`.)
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


def test_compose_script_composes_no_review_phase_when_tier_or_fragment_absent():
    waves = _two_wave_fixture()

    script_neither = compose_script(waves, name="wf", description="no review")
    assert "review:" not in script_neither

    script_tier_only = compose_script(
        waves, name="wf", description="tier only", review_tier="standard"
    )
    assert "review:" not in script_tier_only

    script_fragment_only = compose_script(
        waves,
        name="wf",
        description="fragment only",
        review_roster_fragment=_FIXTURE_ROSTER_FRAGMENT,
    )
    assert "review:" not in script_fragment_only


def test_compose_script_composes_a_single_reviewer_review_phase():
    waves = _two_wave_fixture()
    script = compose_script(
        waves,
        name="wf",
        description="lightweight review",
        review_tier="lightweight",
        review_roster_fragment=_FIXTURE_ROSTER_FRAGMENT,
    )

    phase_titles = _extract_phase_titles(script)
    assert "Review" in phase_titles
    assert "review:coordinator:code-reviewer" in script
    assert "await parallel(" not in script.split("Review")[-1]  # single reviewer, serial call


def test_compose_script_composes_a_parallel_review_phase_for_multiple_reviewers():
    waves = _two_wave_fixture()
    script = compose_script(
        waves,
        name="wf",
        description="full review",
        review_tier="full",
        review_roster_fragment=_FIXTURE_ROSTER_FRAGMENT,
    )

    assert "review:coordinator:code-reviewer" in script
    assert "review:coordinator:integrator" in script
    assert "review:coordinator:staff-reviewer" in script
    assert script.count("agentType: 'coordinator:staff-reviewer'") == 1


def test_compose_script_composes_a_staged_gate_fragment_without_suppressing_the_test_phase():
    """A staged (schema_version >= 2) fragment used to be refused outright —
    that stopgap (8d7d057f) comes out with this chunk: `compose_script` now
    routes a staged fragment through `review_mint.roster.parse_stages` /
    `review_mint.compose.compose` like any other, and a `gate: true` stage's
    verdict must never suppress the terminal test phase for this
    post-execution caller (GATE POLICY, C4's body)."""
    waves = _two_wave_fixture()
    staged = {
        "schema": "review-roster-fragment",
        "schema_version": 3,
        "blocking_verdicts": {"coordinator:prior-art-checker": "BLOCKED-SURFACE-TO-PM"},
        "tiers": {
            "standard": {
                "stages": [
                    {"gate": True, "agents": ["coordinator:prior-art-checker"]},
                    {"agents": ["coordinator:code-reviewer", "coordinator:staff-eng"]},
                ]
            }
        },
    }

    script = compose_script(
        waves,
        name="wf",
        description="staged fragment",
        review_tier="standard",
        review_roster_fragment=staged,
    )

    phase_titles = _extract_phase_titles(script)
    assert "Scoped test run" in phase_titles
    assert phase_titles.index("Scoped test run") == len(phase_titles) - 1
    assert "review:coordinator:prior-art-checker" in script
    assert "review:coordinator:code-reviewer" in script
    # A gate stage's structured schema is present, but no early-return branch
    # is ever spliced for this post-execution caller.
    #
    # This assertion has now been narrowed TWICE by the same mechanism, so it
    # is stated structurally rather than as a third proxy. `"return" not in
    # script` broke when the commit phase grew `return { halted: ... }`
    # (example-retrieval-repo-em memo, 2026-08-20). Its replacement -- every return line
    # not mentioning "commit" -- broke when the script grew a terminal
    # `return { completed: true, ... }` (2026-08-31), which mentions neither
    # commits nor reviews. Each proxy failed the same way: it encoded "is not
    # a review return" as "does not look like the returns I already knew
    # about", so every NEW legitimate return read as a violation.
    #
    # Stated positively instead: a review-gate return would sit inside a
    # review phase's own block. Slice the script from the first review phase
    # to the phase that follows the last one, and assert no return there.
    assert "sidecar_path" in script
    lines = script.splitlines()
    review_span = [
        i for i, line in enumerate(lines)
        if line.strip().startswith("phase(") and "review" in line.lower()
    ]
    assert review_span, "no review phase in a script composed with a review roster"
    # Extend to the next non-review phase() line, or to the end of the script.
    end = len(lines)
    for i in range(review_span[-1] + 1, len(lines)):
        if lines[i].strip().startswith("phase(") and "review" not in lines[i].lower():
            end = i
            break
    review_returns = [
        line for line in lines[review_span[0]:end] if "return" in line
    ]
    assert not review_returns, f"review stage spliced an early return: {review_returns}"


def test_review_calls_carry_no_model_key_so_the_agent_definition_pins_the_tier():
    """A reviewer call site declares its tier via ``agentType``, and the agent
    definition it names pins the model (the personas are ``model: opus`` by
    charter). ``opts.model`` OVERRIDES that frontmatter, so emitting
    ``model: 'sonnet'`` alongside ``agentType`` would silently run a persona
    below its own charter — a Sonnet review wearing an Opus reviewer's name.
    Negative spec for `_review_phase_calls`."""
    waves = _two_wave_fixture()
    script = compose_script(
        waves,
        name="wf",
        description="full review",
        review_tier="full",
        review_roster_fragment=_FIXTURE_ROSTER_FRAGMENT,
    )

    review_segment = script.split("phase('Review');")[-1].split("phase(")[0]
    assert "agentType:" in review_segment, "fixture must actually compose reviewer calls"
    assert "model:" not in review_segment, (
        "reviewer calls must not carry model: — it overrides the agent "
        "definition's own pinned tier"
    )


def test_non_reviewer_calls_still_carry_model_so_coverage_is_partial_not_zero():
    """Dropping ``model:`` from reviewer calls must not push an emitted script
    to ZERO modeled call sites: the Workflow model guard DENIES at zero and only
    advises on partial coverage. Every executor/commit/test call still carries
    it."""
    waves = _two_wave_fixture()
    script = compose_script(
        waves,
        name="wf",
        description="full review",
        review_tier="full",
        review_roster_fragment=_FIXTURE_ROSTER_FRAGMENT,
    )

    assert "model: '" in script, (
        "an emitted script with zero modeled call sites would be DENIED by the "
        "Workflow model guard"
    )


def test_compose_script_review_phase_precedes_terminal_test_phase():
    waves = _two_wave_fixture()
    script = compose_script(
        waves,
        name="wf",
        description="ordering",
        review_tier="standard",
        review_roster_fragment=_FIXTURE_ROSTER_FRAGMENT,
    )

    phase_titles = _extract_phase_titles(script)
    review_index = phase_titles.index("Review")
    assert phase_titles[review_index + 1] == "Scoped test run"


def test_reviewers_for_tier_raises_on_missing_tiers_key():
    waves = _two_wave_fixture()
    with pytest.raises(ReviewRosterFragmentError):
        compose_script(
            waves,
            name="wf",
            description="malformed fragment",
            review_tier="standard",
            review_roster_fragment={"schema": "review-roster-fragment"},
        )


def test_reviewers_for_tier_raises_on_unknown_tier_key():
    waves = _two_wave_fixture()
    with pytest.raises(ReviewRosterFragmentError):
        compose_script(
            waves,
            name="wf",
            description="unknown tier",
            review_tier="not-a-real-tier",
            review_roster_fragment=_FIXTURE_ROSTER_FRAGMENT,
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


def test_emit_script_composes_a_review_phase_when_a_fragment_is_supplied(tmp_path):
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
        plan_path, repo_root=tmp_path, review_roster_fragment=_FIXTURE_ROSTER_FRAGMENT
    )

    phase_titles = _extract_phase_titles(script)
    assert "Review" in phase_titles  # M -> standard tier, fragment supplied
    assert "review:coordinator:integrator" in script


# ---------------------------------------------------------------------------
# (b) Commit-phase placement keyed to wave size (n>10 executors-per-wave)
# ---------------------------------------------------------------------------


_REAL_TEST_MAPPED_PATHS = [
    "coordinator_core/ops/dispatch_emit/spine_read.py",
    "coordinator_core/ops/dispatch_emit/wave_map.py",
]


def _large_wave(n: int, prefix: str = "C"):
    # Each row's `writes:` cycles between two real, co-located-test-mapped
    # repo paths -- write-overlap is a `wave_map.build_waves` concern this
    # fixture bypasses by handing `compose_script` an already-built wave
    # directly, so a shared write path across rows is fine here; only the
    # row ids need to be distinct (asserted via each row's `work:<id>`
    # label).
    return [
        _wave_row(f"{prefix}{i}", [_REAL_TEST_MAPPED_PATHS[i % 2]])
        for i in range(1, n + 1)
    ]


def test_wave_at_threshold_gets_exactly_one_commit_phase():
    waves = [_large_wave(10)]
    script = compose_script(waves, name="wf", description="at threshold")

    phase_titles = _extract_phase_titles(script)
    commit_titles = [t for t in phase_titles if t.startswith("Commit wave")]
    assert commit_titles == ["Commit wave 1"]


def test_wave_over_threshold_splits_into_batches_each_with_its_own_commit_phase():
    waves = [_large_wave(12)]
    script = compose_script(waves, name="wf", description="over threshold")

    phase_titles = _extract_phase_titles(script)
    commit_titles = [t for t in phase_titles if t.startswith("Commit wave")]
    wave_titles = [t for t in phase_titles if t.startswith("Wave ")]

    assert commit_titles == [
        "Commit wave 1 (batch 1/2)",
        "Commit wave 1 (batch 2/2)",
    ]
    assert len(wave_titles) == 2
    assert phase_titles.index(wave_titles[0]) < phase_titles.index(commit_titles[0])
    assert phase_titles.index(commit_titles[0]) < phase_titles.index(wave_titles[1])
    assert phase_titles.index(wave_titles[1]) < phase_titles.index(commit_titles[1])


def _phase_body_slice(script: str, phase_title: str, next_phase_title: str) -> str:
    """Return the script body between two ``phase(...)`` calls, exclusive of
    the second — i.e. exactly the code emitted for ``phase_title``'s own
    phase, not anything belonging to a later phase.

    Locating by the ``phase('<title>')`` call text (not by index into
    ``_extract_phase_titles``) so a title's OWN dispatch block is what's
    checked, rather than merely whether a row id string appears anywhere in
    the whole script -- the substring-anywhere shape this replaces could not
    fail even when a batch's title wrongly enumerated the whole wave (see
    Review: code-reviewer S4-dispatch-emit, P2 finding 2).
    """
    start_marker = f"phase('{phase_title}');"
    end_marker = f"phase('{next_phase_title}');"
    start = script.index(start_marker)
    end = script.index(end_marker, start)
    return script[start:end]


def test_wave_over_threshold_batches_carry_disjoint_rows_in_order():
    waves = [_large_wave(12)]
    script = compose_script(waves, name="wf", description="disjoint batches")

    phase_titles = _extract_phase_titles(script)
    wave_titles = [t for t in phase_titles if t.startswith("Wave ")]
    commit_titles = [t for t in phase_titles if t.startswith("Commit wave")]
    assert wave_titles == ["Wave 1: C1, C2, C3, C4, C5, C6, C7, C8, C9, C10 (batch 1/2)", "Wave 1: C11, C12 (batch 2/2)"]

    assert "await parallel([" in script

    batch_1_slice = _phase_body_slice(script, wave_titles[0], commit_titles[0])
    batch_2_slice = _phase_body_slice(script, wave_titles[1], commit_titles[1])

    for i in range(1, 11):
        assert f"work:C{i}'" in batch_1_slice
        assert f"work:C{i}'" not in batch_2_slice
    for i in range(11, 13):
        assert f"work:C{i}'" in batch_2_slice
        assert f"work:C{i}'" not in batch_1_slice


def test_wave_over_threshold_script_passes_run_checks():
    waves = [_large_wave(12)]
    script = compose_script(waves, name="wf", description="round trip")

    errors = [f for f in run_checks(script) if f.severity is Severity.ERROR]
    assert errors == []


# ---------------------------------------------------------------------------
# compose_script — commit prompt carries the wave's captured executor
# results as pathspec provenance (git-commit-agent.md § Pathspec provenance)
# ---------------------------------------------------------------------------


def test_compose_script_binds_each_waves_executor_results_and_threads_them():
    """Each wave's `agent()`/`parallel()` call must bind a results variable,
    and the immediately-following commit phase must reference that same
    variable via `JSON.stringify` -- never a wave-scoped commit prompt that
    states only the pathspec (git-commit-agent.md refuses that shape)."""
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="two waves")

    assert "const wave1Results = await agent(" in script
    assert "const wave2Results = await agent(" in script
    assert "JSON.stringify(wave1Results, null, 2)" in script
    assert "JSON.stringify(wave2Results, null, 2)" in script


def test_compose_script_commit_prompt_states_provenance_and_passes_run_checks():
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="two waves")

    assert "Pathspec provenance" in script
    assert "touched-files set" in script

    errors = [f for f in run_checks(script) if f.severity is Severity.ERROR]
    assert errors == []


def test_compose_script_commit_prompt_names_every_measured_false_refusal():
    """The provenance block must keep naming all four non-divergences.

    Each corresponds to a halt measured while executing
    `pln-the-discriminators-that-alread-1545b5` (2026-08-27), where the
    committer read a true fact as a reason to refuse and cost a
    `resumeFromRunId`. Asserting the emitted SCRIPT rather than the module
    constant is deliberate: what reaches the agent is the only thing that
    changes its behaviour, and a refactor that stops threading the block
    through would leave a constant-level assertion green."""
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="two waves")

    assert "ONE-DIRECTIONAL BY DEFAULT" in script
    assert "state/subagent-share/**" in script
    assert "SHARED TREE" in script
    assert "UNCHANGED DECLARED PATHS" in script
    assert "A PARTIAL WAVE STILL COMMITS" in script
    assert "ALREADY COMMITTED" in script
    assert "THE CALL RETURNS THE SHA" in script
    assert "ON THE CALL IS A WRONG KEYWORD, NOT AN ABSENT ROUTE" in script
    assert "IS A MISSING" in script and "blob_fallback" in script
    assert "hash_worktree_blobs_via_spawn" in script
    # The refusing set is a property of the TARGET repo's .gitattributes,
    # not of file extension: an emitted script cannot know its target tree,
    # so a file-kind list here would tell a committer dispatched into a
    # blanket-`* text=auto` repo that the case it is about to hit does not
    # happen (doe-claude-9f, 2026-08-30, measured both ways).
    assert "property of the TARGET REPO" in script
    assert "check-attr text eol" in script
    assert "UNCONDITIONALLY" in script

    errors = [f for f in run_checks(script) if f.severity is Severity.ERROR]
    assert errors == []


def test_compose_script_commit_prompt_licenses_a_partial_wave():
    """A wave with a non-DONE item must still commit the DONE items' work.

    Measured on mise run `20260911T111541-8087eee2`: a five-item wave
    returned four DONE and one BLOCKED, and the commit phase committed
    nothing -- reasoning, from this prompt, that five ids in the subject
    could not all land when one item was blocked and that committing four
    would report false delivery. It is a coherent read of what the prompt
    said: the subject rule demanded every dispatched id, and the provenance
    block told it to refuse pathspec entries no report corroborates, which
    is exactly the blocked item's paths. Four executors' output then sat
    uncommitted on a shared checkout while HEAD moved twice underneath it,
    and the run halted before the next wave.

    The rule it should have applied is /mise-en-place § Partial wave
    landing. The fix is prompt-side by necessity: the pathspec is derived
    at EMIT time, before any executor has run, so the union is the only
    thing the emitter can hand over. Narrowing it against the reports is
    the committing agent's job, and this is where it is told so.
    """
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="two waves")

    assert "A PARTIAL WAVE STILL COMMITS" in script
    assert "Refuse only if NO item is DONE" in script
    # The blocked item's id leaving the subject is the half the agent got
    # wrong; asserting only the heading above would leave that green.
    assert "its id drops out of the subject alongside" in script
    assert "registering fewer" in script
    assert "is the failure mode, not the safe choice" in script

    errors = [f for f in run_checks(script) if f.severity is Severity.ERROR]
    assert errors == []


# ---------------------------------------------------------------------------
# compose_script — the bookkeeping-prefix carve-out is a positive allowlist,
# not a blanket one-directional pass (doe-claude-em memo 2026-08-30 (iii);
# improvement-queue c6acabf9a600)
# ---------------------------------------------------------------------------


def test_bookkeeping_prefix_render_is_derived_from_the_allowlist():
    """The prompt must name the same prefixes the allowlist holds.

    Hand-writing the prefix into the prompt string is how the two drift:
    a later prefix addition would silence the halt in nobody's prompt.
    """
    assert emit._BOOKKEEPING_PREFIXES == (
        ".coordinator-local/subagent-share/",
        "state/subagent-share/",
    )
    for prefix in emit._BOOKKEEPING_PREFIXES:
        assert f"`{prefix}**`" in emit._BOOKKEEPING_PREFIX_RENDER
    assert emit._BOOKKEEPING_PREFIX_RENDER in emit._PROVENANCE_HEADING


def test_commit_prompt_halts_on_a_reported_write_outside_the_allowlist():
    """The eight paths stranded on `2026-08-30-who-pushes-and-when.md` were
    chunk work under already-declared surfaces, not sidecars. The prompt has
    to make that case a STOP while leaving the bookkeeping prefix silent --
    a blanket flip re-buys the four measured false halts instead."""
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="two waves")

    assert "DISPATCH-LAYER BOOKKEEPING" in script
    assert "Any OTHER reported-written path absent from the pathspec IS a" in script
    assert "you must STOP" in script
    # The committer must not resolve the divergence itself: widening is the
    # spine's call, and a self-widened pathspec would commit work the spine
    # never declared.
    assert "Do NOT widen the pathspec yourself" in script
    assert "Emit no success token." in script

    errors = [f for f in run_checks(script) if f.severity is Severity.ERROR]
    assert errors == []


def test_commit_prompt_halt_message_points_the_operator_at_the_spine():
    """A halt that names the paths but not the remedy is a halt an operator
    resolves by overriding. The message has to say where the widening
    belongs."""
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="two waves")

    assert "naming every such path verbatim" in script
    assert "widen the spine row and restamp, or confirm they are bookkeeping" in script


def test_the_four_measured_false_halts_stay_silent_under_the_new_clause():
    """Guards the memo's caution directly: none of the four halts measured on
    `pln-the-discriminators-that-alread-1545b5` may be re-bought.

    Sidecars are allowlisted; peer-staged paths and unchanged-declared paths
    are not reported-written by this wave's executors at all, so the STOP
    clause cannot reach them -- and each keeps its own naming clause."""
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="two waves")

    assert "state/subagent-share/**" in script
    assert "do not refuse over it" in script
    assert "SHARED TREE" in script
    assert "UNCHANGED DECLARED PATHS" in script
    # The STOP is keyed on what the REPORTS name, never on the index or on a
    # declared path that did not change.
    assert "reported-written path absent from the pathspec" in script


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


# ---------------------------------------------------------------------------
# Commit-phase halt gate (example-retrieval-repo-em cross-repo memo, 2026-08-20)
# ---------------------------------------------------------------------------


def test_commit_phase_binds_its_result_and_gates_the_next_wave():
    # Before this gate, `await agent(...)` discarded the commit agent's result
    # and the next wave's phase followed unconditionally.
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")
    assert "const commitWave1Results = await agent(" in script
    assert "const commitWave2Results = await agent(" in script
    # 2 commit-phase gates + 2 preflight gates. The preflight contributes TWO
    # because a preflight has two ways to fail: it reported a blocker, or it
    # reported nothing at all. The second was added 2026-08-31 -- until then
    # the phase's pass verdict was the ABSENCE of output, so a null result, a
    # crashed agent and a zero-tool-call agent all passed it. See
    # test_preflight_silence_is_not_a_pass.
    assert script.count("return { halted:") == 4


def test_commit_gate_halts_on_null_and_on_a_tokenless_report():
    # `null` is the engine's own value for an agent that died or was skipped;
    # a returning-but-tokenless report is a refusal that still produced prose.
    # Neither proves a commit landed, so both must fail the same way.
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")
    assert "if (!commitWave1Results || " in script
    assert "test(String(commitWave1Results)))" in script

    gate = _emitted_gate(script)
    assert not gate.search("the commit agent declined; nothing landed\n")
    assert not gate.search("")


def _emitted_gate(script: str):
    """Compile the gate exactly as emitted, so these tests pin BEHAVIOUR.

    Asserting the regex source verbatim turns every one of these into a
    spelling test: widening the token line to tolerate markdown emphasis
    (2026-08-30) broke assertions that had no opinion about what the gate
    actually accepts. What must not regress is which reports pass.
    """
    # The locator names COMMIT-LANDED explicitly. It used to take the FIRST
    # negated anchored regex in the script, which was unambiguous only while
    # the commit gate was the sole negated gate. The preflight CLEAR gate
    # (2026-08-31) is also negated and is emitted EARLIER, so a positional
    # locator retargets onto it and these tests quietly begin asserting
    # things about the wrong gate -- passing or failing for reasons that have
    # nothing to do with commits.
    emitted = re.search(r"!/(\^[^/]*COMMIT-LANDED[^/]*)/m\.test", script)
    assert emitted, "commit gate is no longer an anchored regex test"
    return re.compile(emitted.group(1).replace("\\ ", " "), re.M)


def test_commit_gate_accepts_the_token_line_wrapped_in_markdown_emphasis():
    """A bolded token line is a report that COMMITTED. Measured, not supposed.

    Census of 378 commit-agent outcomes across 653 workflow journals
    (2026-08-30, state/audits/2026-08-30-what-the-commit-halts-actually-were.md):
    of 18 halts, four were reports reading `**COMMIT-LANDED <sha>**`. All four
    shas are in this repo's history -- 5e4a76ea70, ca1ccc6019, 5eb6df2ece,
    ae9607e410 -- so the gate killed four runs AFTER their work had landed.
    At 22% that is the largest single halt class, and each one cost the
    operator a resume or a re-emit for nothing: the "workflow after workflow
    against one plan" the PM reported.

    Agents write reports in markdown and emphasise the line they were told
    matters. The gate is the artifact that has to tolerate that; a prompt
    asking them not to would be "the operator remembers" wearing a subagent.
    """
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")
    gate = _emitted_gate(script)

    # The measured shapes, verbatim from the journals.
    assert gate.search("**COMMIT-LANDED 5e4a76ea706dd35cc1045f22708f20d64b0d9a91**")
    assert gate.search("**COMMIT-LANDED ca1ccc601968ecc57398a523bf13b24ebca6f98e**")
    assert gate.search(
        "prose above\n**COMMIT-LANDED ae9607e4104eab49951e22153b2cd78ecbba2108**\n"
    )
    # And the undecorated form the contract has always named.
    assert gate.search("COMMIT-LANDED 5eb6df2ece10cac6f0af23b6f5ceef820eaad17c\n")


def test_markdown_emphasis_does_not_reopen_the_quoting_fail_open():
    """Widening for emphasis must not re-admit a refusal that quotes the token.

    What closed the 2026-08-21 fail-open is the hex-sha requirement, not the
    absence of asterisks -- so a refusal stays refused however it decorates
    the instruction it is quoting back. This is the assertion that makes the
    widening safe rather than merely convenient.
    """
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")
    gate = _emitted_gate(script)

    assert not gate.search("**COMMIT-LANDED <sha>**")
    assert not gate.search("__COMMIT-LANDED <sha>__")
    assert not gate.search(
        "Attempted per the brief, which said to end the report with the line "
        "'**COMMIT-LANDED <sha>**'. Guard denied; did not land.\n"
    )
    # A sha mid-sentence is still not a token line, decorated or not.
    assert not gate.search(
        "I would have written **COMMIT-LANDED ca1ccc601968ecc57398a523bf13b24ebca6f98e** "
        "had the guard allowed it.\n"
    )


def test_commit_gate_is_not_defeated_by_a_refusal_that_quotes_the_token():
    """Regression: the gate was `.includes('COMMIT-LANDED')`, and it FAILED OPEN.

    Measured 2026-08-21 during a real plan execution. Subagents may not commit
    at all (caller-identity enforced), so the commit agent refused -- and its
    refusal quoted the instruction it had been given, "end your report with the
    line 'COMMIT-LANDED <sha>'", back at the gate. The substring was present,
    the gate passed, and the next wave ran over an uncommitted one.

    The token is in the prompt every commit agent is holding while it writes,
    so a substring test cannot distinguish emitting it from repeating it. This
    pins the discriminator, not the spelling: an anchored whole-line match with
    a real sha.
    """
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")
    gate = _emitted_gate(script)

    # The verbatim shape of the refusal that defeated the old gate.
    quoting_refusal = (
        "The commit gate denied this -- I am a subagent and `git commit` is EM-only.\n"
        "Attempted per the brief, which said to end the report with the line "
        "'COMMIT-LANDED <sha>'. Guard denied; did not land.\n"
    )
    assert not gate.search(quoting_refusal)

    # A genuine landing still passes, or the gate is merely broken the other way.
    assert gate.search("committed the wave\nCOMMIT-LANDED a805587fd8d0\n")
    # And a placeholder on its own line is still not a sha.
    assert not gate.search("COMMIT-LANDED <sha>\n")


def test_commit_prompt_requires_the_landed_token_only_on_success():
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")
    assert "COMMIT-LANDED <sha>" in script
    # The prompt must say NOT to emit it on a refusal -- a token the agent
    # emits unconditionally is not a gate.
    assert "do NOT emit that line" in script


def test_commit_prompt_no_longer_promises_a_subsequent_wave():
    """(a) The withheld-paths brief must not promise anything picks the
    path back up later -- nothing does (wf_8dc1b0ed-32b: D4's withheld path
    was never handled, because no later wave's pathspec is scoped to it).
    """
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")
    assert "subsequent wave" not in script
    assert "will be handled" not in script
    # The replacement instruction: withheld paths are reported, and the run
    # halts -- no promise that anything later resolves them.
    assert "Nothing downstream carries a withheld path forward" in script
    assert "do not imply, predict, or promise that a later wave" in script


def test_commit_gate_halts_on_a_partial_verdict_before_the_next_wave():
    """(b) A COMMIT-PARTIAL verdict must halt the run, the same as a
    tokenless report -- before the next wave's agent calls, not after.
    """
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")
    gate = _emitted_gate(script)

    # A partial verdict never satisfies the COMMIT-LANDED gate...
    assert not gate.search(
        "committed D2 and D8\nCOMMIT-PARTIAL 5e4a76ea706dd35cc1045f22708f20d64b0d9a91"
        " withheld: chunks/D4.py\n"
    )
    # ...and the halt fires ahead of the next wave's phase in the emitted
    # script order.
    commit1_idx = script.index("const commitWave1Results = await agent(")
    gate1_halt_idx = script.index("return { halted:", commit1_idx)
    wave2_phase_idx = script.index("Wave 2", gate1_halt_idx + 1)
    assert gate1_halt_idx < wave2_phase_idx

    # The halt message names withheld paths when the agent supplied them.
    assert "landed only PART of its handed pathspec" in script
    assert "partialMatch" in script
    assert "withheld" in script


def test_commit_gate_still_continues_on_a_fully_committed_wave():
    """(c) The existing fully-landed path is unchanged: COMMIT-LANDED <sha>
    still satisfies the gate and the run proceeds.
    """
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")
    gate = _emitted_gate(script)

    assert gate.search("committed the wave\nCOMMIT-LANDED a805587fd8d0\n")


def test_a_declared_but_untouched_path_is_landed_not_partial():
    """A declared path with nothing dirty against HEAD (the memo-outbox
    case: `memo.send` writes into the gitignored
    `.coordinator-local/memo-outbox/`, so a `writes:` entry for it never
    appears in this repo's tree) is NOT withheld -- there is nothing to
    commit for it. The prompt must say so explicitly, and a report naming
    that path alongside COMMIT-LANDED must still satisfy the gate rather
    than being read as a reason to hold out for COMMIT-PARTIAL.
    """
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")

    # The prompt states the discriminator plainly.
    assert "WITHHELD MEANS DIRTY-AND-UNCOMMITTED, NOT MERELY DECLARED" in script
    assert "memo-outbox" in script
    assert "must never trigger the partial verdict" in script

    gate = _emitted_gate(script)
    # A committer that landed every dirty path and reports a declared,
    # untouched memo-outbox path as clean still emits COMMIT-LANDED, and the
    # gate passes -- the run continues, no COMMIT-PARTIAL involved.
    assert gate.search(
        "committed chunks/D2.py and chunks/D8.py; "
        "state/memo-outbox/sent/D2-notify.md was declared but never appeared "
        "in the worktree (memo.send route, gitignored) -- clean, nothing to "
        "commit\nCOMMIT-LANDED a805587fd8d0\n"
    )


def test_declared_and_still_dirty_after_landing_halts_the_wave():
    """Defect (example-retrieval-repo-4a, commit b550e655): a C3 executor reported
    `registry/materialize.ts` changed, the wave's declared pathspec named
    it, and the commit agent's report still read COMMIT-LANDED while the
    file stayed dirty. HEAD then failed to import, and the residue halted
    the NEXT plan's commit wave as unaccounted -- this wave should have
    halted at its own boundary instead.

    The gate itself has no independent git access (pure JS orchestration),
    so the fix is the mandated post-commit `git status --porcelain` check
    in the static prompt (`_commit_agent_call`) directing the agent to
    treat a still-dirty declared path as withheld and fall through to
    COMMIT-PARTIAL -- this pins both halves: the prompt states the
    requirement, and the emitted gate still halts when a commit agent
    that followed it reports the resulting COMMIT-PARTIAL.
    """
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")

    assert "POST-COMMIT VERIFICATION IS MANDATORY, NOT OPTIONAL" in script
    assert "git status --porcelain --" in script
    assert "registry/materialize.ts" in script
    assert "b550e655" in script

    gate = _emitted_gate(script)
    assert not gate.search(
        "ran git status --porcelain after commit_paths returned; "
        "chunks/D4.py was still dirty despite being declared and reported "
        "landed\n"
        "COMMIT-PARTIAL a805587fd8d0 withheld: chunks/D4.py\n"
    )


def test_commit_gate_land_nothing_halt_is_unchanged():
    """(d) The pre-existing halt-on-nothing-landed behaviour is unchanged:
    a null result and a tokenless report both still fail the gate.
    """
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")
    gate = _emitted_gate(script)

    assert not gate.search("the commit agent declined; nothing landed\n")
    assert not gate.search("")
    assert "did not land a commit" in script


def test_commit_gate_names_resume_path_not_just_the_failure():
    # A halt the operator cannot act on is a stall. The reason must say how to
    # continue after clearing the cause.
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")
    assert "resumeFromRunId" in script


def test_commit_gate_halt_text_forbids_the_re_emit_and_names_the_resume_call():
    """The halt used to instruct the exact move that loses work.

    Measured by doe-claude-em (2026-08-30): a commit-phase halt ends the whole
    emitted run, and a re-emit is the reachable recovery for an EM who no
    longer holds the run id. `spine_read`'s closed-disposition exclusion then
    correctly drops the chunks that DID land, and the narrowed one-wave `.mjs`
    on disk is indistinguishable from one always meant to be partial -- the
    "workflow after workflow against one plan" the PM saw. Nothing refuses and
    nothing warns, so the halt STRING is the only surface that can put the
    correct move in front of the EM at the moment they need it.

    Pinned here rather than at the reason constant because only what reaches
    the emitted script changes an operator's behaviour.
    """
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")

    assert "RECOVERY IS RESUME, NEVER RE-EMIT" in script
    assert "A-SECOND-EMIT-AFTER-A-PARTIAL-RUN-NARROWS-SILENTLY" in script
    # The literal call, not just the parameter name -- an EM reading `resume
    # via resumeFromRunId` still has to work out what to type.
    assert "Workflow({scriptPath, resumeFromRunId})" in script
    # Resume serves the longest UNCHANGED prefix from cache, so relaunching
    # this script untouched replays the refusal and re-halts. A recovery text
    # that omits the edit sends the operator round the same loop once more.
    assert "an unchanged call" in script and "served from cache" in script
    # And the same-session-only limit, or the instruction is a dead end for
    # exactly the EM who most needs it.
    assert "same-session-only" in script

    errors = [f for f in run_checks(script) if f.severity is Severity.ERROR]
    assert errors == []


def test_every_commit_phase_gets_its_own_uniquely_named_gate():
    # A reused const name is a redeclaration error in the emitted script; a
    # shared one would also let wave 2's gate read wave 1's result.
    waves = [
        [_wave_row("C1", ["coordinator_core/ops/dispatch_emit/spine_read.py"])],
        [_wave_row("C2", ["coordinator_core/ops/dispatch_emit/wave_map.py"])],
        [_wave_row("C3", ["coordinator_core/ops/dispatch_emit/pathspec.py"])],
    ]
    script = compose_script(waves, name="wf", description="three waves")
    bound = re.findall(r"const (commit\w+) = await agent\(", script)
    assert len(bound) == 3
    assert len(set(bound)) == 3, f"duplicate commit result bindings: {bound}"
    for var in bound:
        assert f"if (!{var} ||" in script


def test_commit_gate_precedes_the_next_wave_phase():
    # The whole point is ordering: the gate is worthless if it lands after the
    # next wave's executors have already written.
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")
    first_gate = script.index("if (!commitWave1Results ||")
    second_wave_commit = script.index("const commitWave2Results")
    assert first_gate < second_wave_commit


def test_gated_script_still_passes_the_workflow_contract_checker():
    script = compose_script(_two_wave_fixture(), name="wf", description="two waves")
    assert_zero_errors(script)


def test_commit_prompt_tells_the_agent_a_claim_is_not_a_refusal_condition():
    """Measured 2026-08-31: a wave-2 commit agent declined ALL SEVEN paths of
    its pathspec citing a claim held by a session 13 hours idle with both
    recorded pids dead, which had touched exactly ONE of the seven. The
    sanctioned route then committed all seven without complaint -- because
    `commit_paths` performs no ownership or claim check at all. The prompt
    must name both the liveness and the per-path narrowing checks, or the
    same halt recurs on every shared-tree run.

    AMENDED 2026-08-31 (second incident, same run). This test previously
    pinned the sentence "A CLAIM IS NOT A REFUSAL CONDITION, AND THE ROUTE
    NEVER RAISES ONE", and that sentence was FALSE at the layer that
    actually stops the agent. `commit_paths` checks no claims, but a
    PreToolUse guard in front of it does, and refuses before the route is
    reached. Two further waves halted on exactly that -- one on a dead
    session's claim, one on an orphan record -- with the prompt telling
    each agent that the refusal it was holding could not exist. Pinning a
    reassurance that contradicts an observable denial is worse than pinning
    nothing, so the assertion now pins the two-layer truth plus the
    recovery verbs, which is what an agent needs to get unstuck alone.
    """
    from coordinator_core.ops.dispatch_emit import emit as _emit

    prompt = _emit._COMMIT_AGENT_CONTRACT if hasattr(_emit, "_COMMIT_AGENT_CONTRACT") else None
    if prompt is None:
        import inspect

        prompt = inspect.getsource(_emit)

    # The two layers, named as two -- this is the correction.
    assert "A CLAIM CAN REFUSE YOU, BUT ONLY A GUARD RAISES IT" in prompt
    assert "performs NO ownership or claim check" in prompt
    assert "PreToolUse guard sits IN FRONT of the" in prompt
    # The old sentence must not come back: it is the defect, not a phrasing.
    assert "THE ROUTE NEVER RAISES ONE" not in prompt
    # Liveness leg.
    assert "absent from the process table" in prompt
    # Per-path leg: one hit must not generalise across the pathspec.
    assert "touch-record.jsonl" in prompt
    assert "refuse ONLY the claimed paths and commit the" in prompt
    # The recovery, without which the agent can diagnose and still not move.
    assert "who-claims-path" in prompt
    assert "clear-claim-if-dead" in prompt
    # And why that verb is safe to hand a haiku agent unsupervised.
    assert "no-op against a LIVE holder" in prompt


# ---------------------------------------------------------------------------
# C3 -- the emitted row prompt carries the executor return contract, and
# that contract lands somewhere safe to commit on its own
# (docs/plans/2026-09-11-the-executor-return-contract-gets-one-de.md § C3)
# ---------------------------------------------------------------------------


def _one_wave_fixture_with_writes(writes):
    return [[_wave_row("C1", writes)]]


def test_emitted_row_prompt_carries_the_footprint_constraint_over_writes_plus_report():
    """The footprint constraint must be spliced into the emitted SCRIPT text
    (never asserted against the module constant alone -- see
    `test_compose_script_commit_prompt_names_every_measured_false_refusal`'s
    docstring for why a constant-level assertion would stay green through a
    refactor that stopped threading the text into the emitted prompt)."""
    waves = _one_wave_fixture_with_writes(["coordinator_core/ops/dispatch_emit/emit.py"])
    script = compose_script(waves, name="wf", description="one wave", plan_path="docs/plans/example.md")

    assert "You MUST NOT create or modify any file outside this footprint" in script
    assert "coordinator_core/ops/dispatch_emit/emit.py" in script
    assert ".coordinator-local/subagent-share/dispatch-reports/example/C1.md" in script


def test_emitted_row_prompt_carries_the_self_verify_constraint_naming_emitted_authority():
    """The self-verify clause must name the EMITTED commit/verification
    authority (the wave's own commit phase + the terminal test-runner
    phase) -- never the hand-dispatch "the EM" text, which is false on this
    path."""
    waves = _one_wave_fixture_with_writes(["coordinator_core/ops/dispatch_emit/emit.py"])
    script = compose_script(waves, name="wf", description="one wave", plan_path="docs/plans/example.md")

    assert "leave your changes uncommitted and unstaged" in script
    assert "coordinator:git-commit-agent` commit phase" in script
    assert "terminal `coordinator:test-runner` phase" in script
    # The hand-dispatch text this must never regress to.
    assert "Only the EM commits, once per wave" not in script


def test_emitted_row_prompt_commit_clause_names_only_the_commit_phase():
    """Slot (4) ("Only <X> commits, once per wave...") must name ONLY the
    commit phase -- never the terminal test-runner phase, which does not
    commit (Review: coordinator:code-reviewer, finding 1, EM-agreed
    break-class fix). Asserted against `compose_script` output, not the
    module constant, per the reviewer's brief."""
    waves = _one_wave_fixture_with_writes(["coordinator_core/ops/dispatch_emit/emit.py"])
    script = compose_script(waves, name="wf", description="one wave", plan_path="docs/plans/example.md")

    assert (
        "Only this wave\\'s `coordinator:git-commit-agent` commit phase "
        "commits, once per wave"
    ) in script
    assert (
        "Only this wave\\'s `coordinator:git-commit-agent` commit phase and "
        "the run\\'s terminal `coordinator:test-runner` phase commits"
    ) not in script


def test_emitted_row_prompt_carries_the_done_summary_constraint_with_reply_and_porcelain():
    """The done-summary constraint's structured-reply rule and its
    porcelain changed-path clause must both reach the emitted script,
    scoped to THIS row's own footprint (writes + report path)."""
    waves = _one_wave_fixture_with_writes(["coordinator_core/ops/dispatch_emit/emit.py"])
    script = compose_script(waves, name="wf", description="one wave", plan_path="docs/plans/example.md")

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
    into every row prompt must already fall under one of
    `_BOOKKEEPING_PREFIXES`, so the commit phase's own provenance heading
    never instructs a halt on a wave's own dispatch report."""
    report_path = emit._dispatch_report_path("docs/plans/example.md", "C1")
    assert any(report_path.startswith(prefix) for prefix in emit._BOOKKEEPING_PREFIXES)


def test_dispatch_report_path_refuses_a_row_id_containing_path_separators():
    """Review: coordinator:code-reviewer, finding P3, EM-overridden to APPLY
    -- a row id spliced raw with `../` would pass the allowlist's
    `str.startswith` check while resolving outside `_BOOKKEEPING_PREFIXES`
    on disk. Must refuse loud, never silently sanitize."""
    import pytest

    for bad_id in ("../escape", "a/b", "a\\b", "..", "."):
        with pytest.raises(ValueError):
            emit._dispatch_report_path("docs/plans/example.md", bad_id)


def test_dispatch_report_path_accepts_ordinary_row_ids():
    """Review: coordinator:code-reviewer, finding 1 -- the allowlist rewrite
    must still accept every ordinary row-id shape a plan spine writes today."""
    for good_id in ("C1", "c-1", "C_1.a"):
        report_path = emit._dispatch_report_path("docs/plans/example.md", good_id)
        assert report_path == f".coordinator-local/subagent-share/dispatch-reports/example/{good_id}.md"


def test_dispatch_report_path_refuses_windows_hazardous_row_ids():
    """Review: coordinator:code-reviewer, finding 1, EM-confirmed LIVE --
    `spine_read` validates row-id presence, type and uniqueness only, never
    character shape, so a row id shaped like a drive letter, a leading
    `~`, a trailing dot/space, a control character, or a Windows reserved
    device name (bare or with an extension) reaches this function
    untouched and must be refused here."""
    import pytest

    bad_ids = [
        "C:",  # bare drive-letter-shaped segment
        "~foo",  # leading ~
        "foo.",  # trailing dot -- Windows silently strips it
        "foo ",  # trailing space -- Windows silently strips it
        "foo\x00bar",  # NUL character
        "foo\x01bar",  # control character
        "CON",  # reserved device name
        "con",  # reserved device name, case-insensitive
        "CON.md",  # reserved device name with an extension
        "COM1",  # reserved device name
        "LPT1.md",  # reserved device name with an extension
    ]
    for bad_id in bad_ids:
        with pytest.raises(ValueError):
            emit._dispatch_report_path("docs/plans/example.md", bad_id)


def test_emitted_script_never_instructs_a_halt_on_the_dispatch_report_path():
    """End-to-end: compose a script for a wave whose row writes nothing
    else, and confirm the commit phase's rendered provenance heading would
    treat the row's OWN dispatch report as dispatch-layer bookkeeping (the
    allowlisted, silent branch) rather than as an unaccounted divergence
    that halts the run."""
    waves = _one_wave_fixture_with_writes(["coordinator_core/ops/dispatch_emit/emit.py"])
    script = compose_script(waves, name="wf", description="one wave", plan_path="docs/plans/example.md")

    report_path = emit._dispatch_report_path("docs/plans/example.md", "C1")
    assert report_path in script
    # The provenance heading's allowlist carve-out must co-occur, in this
    # SAME emitted script, with the report path it is meant to cover.
    # Prefix rendering itself is pinned once, above (test_emit.py:~1743);
    # `report_path.startswith(prefix)` is pinned once, in the test
    # immediately above this one -- neither is re-derived here.
    assert "DISPATCH-LAYER BOOKKEEPING" in script


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
    script = compose_script(waves, name="wf", description="one wave", plan_path="docs/plans/example.md")

    row_prompt = emit._row_prompt(
        _wave_row("C1", ["coordinator_core/ops/dispatch_emit/emit.py"]),
        "docs/plans/example.md",
    )
    literal = _js_string_literal(row_prompt)
    assert literal in script
    # Never spliced as a backtick template literal at the wave-agent-call
    # splice point.
    assert f"`{row_prompt}`" not in script
