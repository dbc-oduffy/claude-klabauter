"""
coordinator_core.ops.tests.test_workflow_scaffold

Tests for the "workflow.scaffold" op (coordinator_core/ops/workflow_scaffold.py)
— pure-generation composer over the four HOUSE_PATTERNS templates
(coordinator_core.ops._workflow_patterns, C3's own module). Exercises the op
HANDLER directly (not dispatch_message — that wire-level smoke lives in
test_op_registration.py per the established pattern), covering:

  (a) one case per pattern: emitted text contains a pure-literal meta,
      matching phase() titles (both the phase()-call and agent-options
      `phase:` surfaces), and the chosen pattern's template text.
  (b) pattern omitted -> "pipeline-default" (DoE consult note 3).
  (c) unknown pattern -> descriptive ValueError.
  (d) missing name/description -> descriptive ValueError.
  (e) ROUND-TRIP drift guard (AC7): for EACH of the four patterns, scaffold
      output is written to a tmp file and fed straight into the REGISTERED
      workflow.validate op via coordinator_core.ipc.dispatch_message — this
      exercises the actual C2<->C3 runtime coupling, not just an import —
      asserting ok:true and zero ERROR findings.
  (f) AC7a hand-authored fixture corpus: a large, realistic shipped-shaped
      Workflow script (populated agent() prompt bodies, including a prompt
      that literally contains the strings "Date.now()" and "Math.random()")
      asserted ok:true via workflow.validate (false-positive guard, ties to
      F1/the scrubber), plus hand-mutated malformed variants (impure meta via
      a bare-identifier value; a REAL forbidden Date.now() in code position;
      a phase() title absent from meta.phases) asserted to produce the
      expected ERROR/WARN findings (false-negative guard). This fixture is
      synthesized in-repo (no real shipped .mjs corpus exists yet) — see the
      module docstring note and the plan's C3 deviations.

Spec backlink: pln-workflow-skeleton-stamper-maki-adab0d § C3
"""

from __future__ import annotations

import asyncio

import pytest

import coordinator_core.ops  # noqa: F401 — triggers register_op(...) for every op module
import coordinator_core.ipc as ipc
from coordinator_core.ops._workflow_patterns import HOUSE_PATTERNS
from coordinator_core.ops.workflow_scaffold import _workflow_scaffold
from coordinator_core.ops.workflow_validate import _workflow_validate


def _run(coro):
    if asyncio.iscoroutine(coro):
        return asyncio.new_event_loop().run_until_complete(coro)
    return coro


def _scaffold(**params):
    return _run(_workflow_scaffold(params))


def _validate_text(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return _run(_workflow_validate({"script_path": str(p)}))


def _validate_via_dispatch(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "workflow.validate",
        "params": {"script_path": str(p)},
    }
    return _run(ipc.dispatch_message(msg))


# ---------------------------------------------------------------------------
# (a) one case per pattern
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pattern", sorted(HOUSE_PATTERNS.keys()))
def test_each_pattern_emits_conformant_shape(pattern):
    result = _scaffold(
        name="demo",
        description="a demo workflow",
        phases=[{"title": "Collect", "detail": "gather items"}, {"title": "Report", "detail": ""}],
        pattern=pattern,
    )
    script = result["script"]

    assert "export const meta = {" in script
    assert "name: 'demo'" in script
    assert "description: 'a demo workflow'" in script
    assert "phases: ['Collect', 'Report']" in script

    # phase()-call surface
    assert "phase('Collect');" in script
    assert "phase('Report');" in script

    # agent-options phase: surface, both matching meta.phases
    assert "phase: 'Collect'" in script
    assert "phase: 'Report'" in script

    # every agent() call carries an active model: 'sonnet'
    assert "model: 'sonnet'" in script

    # the chosen house pattern's template text is embedded (as a comment)
    assert HOUSE_PATTERNS[pattern] in script


@pytest.mark.parametrize("pattern", sorted(HOUSE_PATTERNS.keys()))
def test_phase_and_agent_calls_are_top_level_not_wrapped(pattern):
    """The harness Workflow contract executes the script BODY's top-level
    statements only and never calls a `run` export — see
    coordinator_core/ops/dispatch_emit/emit.py module docstring § Top-level
    body, never a defined-but-uninvoked wrapper. A `phase()`/`agent()` call
    emitted inside `async function run(ctx) { ... }` is dead code: the
    scaffolded script would spawn zero agents while reporting success."""
    result = _scaffold(
        name="demo",
        description="a demo workflow",
        phases=[{"title": "Collect", "detail": ""}],
        pattern=pattern,
    )
    script = result["script"]

    assert "async function run" not in script
    assert "function run(" not in script

    lines = [line.strip() for line in script.splitlines() if line.strip()]
    assert "phase('Collect');" in lines
    assert any(line.startswith("await agent(") for line in lines)


# ---------------------------------------------------------------------------
# (b) pattern omitted -> pipeline-default (DoE consult note 3)
# ---------------------------------------------------------------------------


def test_pattern_omitted_defaults_to_pipeline_default():
    result = _scaffold(name="demo", description="a demo workflow")
    assert HOUSE_PATTERNS["pipeline-default"] in result["script"]


def test_phases_omitted_still_phase_conformant():
    result = _scaffold(name="demo", description="a demo workflow")
    script = result["script"]
    assert "phase('Run');" in script
    assert "phases: ['Run']" in script


# ---------------------------------------------------------------------------
# (c) unknown pattern / (d) missing required params
# ---------------------------------------------------------------------------


def test_unknown_pattern_raises_value_error():
    with pytest.raises(ValueError, match="pattern"):
        _scaffold(name="demo", description="a demo workflow", pattern="not-a-real-pattern")


def test_missing_name_raises_value_error():
    with pytest.raises(ValueError, match="name"):
        _run(_workflow_scaffold({"description": "a demo workflow"}))


def test_missing_description_raises_value_error():
    with pytest.raises(ValueError, match="description"):
        _run(_workflow_scaffold({"name": "demo"}))


# ---------------------------------------------------------------------------
# (e) ROUND-TRIP drift guard (AC7) — via dispatch_message, the real C2<->C3
# runtime coupling, not just an import of the handler.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pattern", sorted(HOUSE_PATTERNS.keys()))
def test_round_trip_scaffold_output_validates_clean(tmp_path, pattern):
    scaffolded = _scaffold(
        name=f"round-trip-{pattern}",
        description="round-trip drift guard fixture",
        phases=[{"title": "Work", "detail": "do the thing"}],
        pattern=pattern,
    )

    d = _validate_via_dispatch(tmp_path, f"{pattern}.mjs", scaffolded["script"])

    assert "result" in d, f"dispatch_message must succeed; got error: {d.get('error')}"
    result = d["result"]
    assert result["ok"] is True, (
        f"pattern {pattern!r} scaffold output failed validation: {result['findings']}"
    )
    assert result["error_count"] == 0


# ---------------------------------------------------------------------------
# (f) AC7a — hand-authored fixture corpus (large, realistic, populated
# agent() prompt bodies) + hand-mutated malformed variants.
#
# NOTE (deviation, documented per the plan's AC7a instruction): no real
# shipped fleet Workflow .mjs corpus exists in-repo at C3-authoring time —
# this fixture is SYNTHESIZED to be representative of the shipped shape
# (multi-phase, parallel fan-out, populated multi-sentence agent() prompts),
# not copied from a real script. It intentionally reuses the C2 large-fixture
# scaffolding shape (test_workflow_validate.py's LARGE_REALISTIC_SCRIPT) but
# is authored independently here so C3's own test module does not import
# C2's test module as a fixture source.
# ---------------------------------------------------------------------------

LARGE_REALISTIC_FIXTURE = '''\
export const meta = {
  name: 'incident-triage-and-brief',
  description: 'Pulls open incidents from the tracker, triages each with an ' +
    'agent, and drafts a single morning brief for the on-call lead.',
  phases: ['fetch', 'triage', 'brief'],
};

const TRACKER_ENDPOINT = './state/incidents/open.json';
const SEVERITY_FLOOR = 'P2';

async function loadOpenIncidents(ctx) {
  const raw = await ctx.readFile(TRACKER_ENDPOINT);
  return JSON.parse(raw);
}

function aboveFloor(incident) {
  return incident.severity <= SEVERITY_FLOOR;
}

async function run(ctx) {
  phase('fetch');

  const incidents = (await loadOpenIncidents(ctx)).filter(aboveFloor);

  phase('triage');

  const triaged = await parallel(incidents.map((incident) => async () => {
    return await agent({
      prompt: `Triage incident ${incident.id}: "${incident.title}".
        Current severity: ${incident.severity}. Reported ${incident.daysOpen}
        days ago. Do NOT attempt to compute how long it has been open yourself
        (no Date.now() calls, no Math.random() sampling for a fake ETA) —
        daysOpen is already provided and authoritative. Recommend one of:
        escalate, keep-monitoring, close-as-resolved. Give a one-sentence
        reason, then the verdict alone on its own line.`,
      model: 'sonnet',
      phase: 'triage',
    });
  }));

  const verdictCounts = { escalate: 0, 'keep-monitoring': 0, 'close-as-resolved': 0 };
  for (const verdict of triaged) {
    const key = verdict.trim().split('\\n').pop();
    if (key in verdictCounts) {
      verdictCounts[key] += 1;
    }
  }

  phase('brief');

  const brief = await agent({
    prompt: `Draft a morning on-call brief. Incidents reviewed: ${incidents.length}.
      Verdict counts: ${JSON.stringify(verdictCounts)}. Two short paragraphs:
      what needs attention today, and what can wait. Under 150 words.`,
    model: 'sonnet',
    phase: 'brief',
  });

  return { incidentCount: incidents.length, verdictCounts, brief };
}

export default run;
'''


def test_large_realistic_fixture_is_ok_zero_error(tmp_path):
    result = _validate_text(tmp_path, "large-realistic.mjs", LARGE_REALISTIC_FIXTURE)
    assert result["ok"] is True
    assert result["error_count"] == 0
    codes = {f["code"] for f in result["findings"]}
    # the prompt body literally contains "Date.now()" and "Math.random()" as
    # TEXT (inside a template literal) — F1 false-positive guard: neither
    # forbidden-global code must fire.
    assert "forbidden-global-date-now" not in codes
    assert "forbidden-global-math-random" not in codes
    assert "agent-model-default" not in codes


def test_fixture_prompt_body_contains_forbidden_global_literal_text():
    # Sanity check on the fixture itself: prove the false-positive guard
    # above is actually exercising the F1 masking path, not a fixture that
    # happens not to mention these tokens at all.
    assert "Date.now()" in LARGE_REALISTIC_FIXTURE
    assert "Math.random()" in LARGE_REALISTIC_FIXTURE


def test_large_fixture_mutated_impure_meta_bare_identifier_is_error(tmp_path):
    mutated = LARGE_REALISTIC_FIXTURE.replace(
        "const SEVERITY_FLOOR = 'P2';",
        "const SEVERITY_FLOOR = 'P2';\nconst computedName = deriveName();",
    ).replace(
        "  name: 'incident-triage-and-brief',",
        "  name: computedName,",
    )
    result = _validate_text(tmp_path, "mutated-impure-meta.mjs", mutated)
    assert result["ok"] is False
    codes = {f["code"] for f in result["findings"]}
    assert "meta-impure-bare-identifier" in codes


def test_large_fixture_mutated_real_forbidden_global_in_code_is_error(tmp_path):
    mutated = LARGE_REALISTIC_FIXTURE.replace(
        "  const verdictCounts = { escalate: 0, 'keep-monitoring': 0, 'close-as-resolved': 0 };",
        "  const triageStartedAt = Date.now();\n"
        "  const verdictCounts = { escalate: 0, 'keep-monitoring': 0, 'close-as-resolved': 0 };",
    )
    result = _validate_text(tmp_path, "mutated-real-forbidden-global.mjs", mutated)
    assert result["ok"] is False
    codes = {f["code"] for f in result["findings"]}
    assert "forbidden-global-date-now" in codes


def test_large_fixture_mutated_phase_title_absent_from_meta_is_warn(tmp_path):
    mutated = LARGE_REALISTIC_FIXTURE.replace(
        "phase('brief');\n\n  const brief",
        "phase('wrap-up');\n\n  const brief",
    )
    result = _validate_text(tmp_path, "mutated-phase-mismatch.mjs", mutated)
    assert result["ok"] is True  # WARN only, never fails
    codes = {f["code"] for f in result["findings"]}
    assert "phase-call-title-mismatch" in codes
    finding = next(f for f in result["findings"] if f["code"] == "phase-call-title-mismatch")
    assert finding["severity"] == "WARN"
