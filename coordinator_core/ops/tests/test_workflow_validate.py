"""
coordinator_core.ops.tests.test_workflow_validate

Tests for the "workflow.validate" op (coordinator_core/ops/workflow_validate.py)
— the authoritative RPC lint against the shared pattern-contract SSOT
(coordinator_core.ops._workflow_contract, landed in C1). Exercises the op
HANDLER directly (not dispatch_message — that wire-level smoke lives in
test_op_registration.py per its established pattern), covering:

  (a) footgun cases (AC5): impure meta -> ERROR; each forbidden global
      (Math.random / Date.now / argless new Date()) -> ERROR, each actionable;
      a conformant script -> ok:true, zero ERROR; the F1 false-positive
      fixture (agent() prompt body literally containing "Date.now()") ->
      ok:true, zero ERROR.
  (b) heuristic + advisory cases (AC6, all WARN not ERROR): parallel-
      transform-parallel -> WARN; a legitimate barrier does NOT hard-fail;
      phase()-title mismatch -> WARN; agent-options phase: mismatch -> WARN.
  (c) missing script_path -> descriptive ValueError.
  (d) path-guard containment: script_path outside the derived/explicit
      containment root raises PathEscapeError, not a silent read.
  (e) AC7a hand-authored fixture corpus: a large, realistic shipped-shaped
      Workflow script (populated agent() prompt bodies) asserted ok:true
      (false-positive guard), plus hand-mutated malformed variants of it
      asserted to produce the expected ERROR/WARN findings (false-negative
      guard).

Spec backlink: docs/plans/2026-07-12-workflow-skeleton-stamper-claude-klabauter-engine.md § C2
"""

from __future__ import annotations

import asyncio

import pytest

from coordinator_core.cartography._guard import PathEscapeError
from coordinator_core.ops.workflow_validate import _workflow_validate


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _validate(script_path, target_root=None):
    params = {"script_path": str(script_path)}
    if target_root is not None:
        params["target_root"] = str(target_root)
    return _run(_workflow_validate(params))


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# (a) footgun cases — AC5
# ---------------------------------------------------------------------------

_CONFORMANT_SCRIPT = """\
export const meta = {
  name: 'demo-workflow',
  description: 'a conformant demo workflow',
  phases: ['collect', 'report'],
};

async function run(ctx) {
  phase('collect');
  const items = await parallel(ctx.targets, async (t) => {
    return await agent({ prompt: `process ${t}`, model: 'sonnet' });
  });

  phase('report');
  return items;
}
"""


def test_impure_meta_is_error(tmp_path):
    script = _write(
        tmp_path,
        "impure.mjs",
        """\
export const meta = {
  name: 'bad',
  description: `desc ${1 + 1}`,
};
""",
    )
    result = _validate(script)
    assert result["ok"] is False
    assert result["error_count"] >= 1
    codes = {f["code"] for f in result["findings"]}
    assert "meta-impure-interpolation" in codes or "meta-impure-template-literal" in codes
    for f in result["findings"]:
        if f["severity"] == "ERROR":
            assert len(f["message"]) > 20  # actionable, not a bare "invalid"


@pytest.mark.parametrize(
    "snippet,expected_code",
    [
        ("const t = Math.random();", "forbidden-global-math-random"),
        ("const t = Date.now();", "forbidden-global-date-now"),
        ("const d = new Date();", "forbidden-global-new-date"),
    ],
)
def test_forbidden_globals_are_error_and_actionable(tmp_path, snippet, expected_code):
    script = _write(
        tmp_path,
        "forbidden.mjs",
        f"""\
export const meta = {{
  name: 'bad',
  description: 'uses a forbidden global',
}};

async function run(ctx) {{
  {snippet}
}}
""",
    )
    result = _validate(script)
    assert result["ok"] is False
    codes = {f["code"] for f in result["findings"]}
    assert expected_code in codes
    finding = next(f for f in result["findings"] if f["code"] == expected_code)
    assert finding["severity"] == "ERROR"
    assert len(finding["message"]) > 20


def test_conformant_script_is_ok_zero_error(tmp_path):
    script = _write(tmp_path, "conformant.mjs", _CONFORMANT_SCRIPT)
    result = _validate(script)
    assert result["ok"] is True
    assert result["error_count"] == 0


def test_f1_date_now_literal_in_prompt_body_is_not_error(tmp_path):
    # F1 false-positive fixture: a conformant script whose agent() prompt
    # BODY contains the literal text "Date.now()" — must validate clean.
    script = _write(
        tmp_path,
        "f1.mjs",
        """\
export const meta = {
  name: 'f1-fixture',
  description: 'prompt body mentions a forbidden global as literal text',
  phases: ['work'],
};

async function run(ctx) {
  phase('work');
  return await agent({
    prompt: `Explain why Date.now() is forbidden in Workflow scripts and
             how Math.random() also throws at runtime.`,
    model: 'sonnet',
  });
}
""",
    )
    result = _validate(script)
    assert result["ok"] is True
    assert result["error_count"] == 0


# ---------------------------------------------------------------------------
# (b) heuristic + advisory cases — AC6, always WARN not ERROR
# ---------------------------------------------------------------------------


def test_parallel_transform_parallel_is_warn(tmp_path):
    script = _write(
        tmp_path,
        "barrier.mjs",
        """\
export const meta = {
  name: 'barrier-shape',
  description: 'two barriers with a plain transform between them',
};

async function run(ctx) {
  const first = await parallel(ctx.items, async (x) => x * 2);
  const mapped = first.map((x) => x + 1);
  const second = await parallel(mapped, async (x) => x - 1);
  return second;
}
""",
    )
    result = _validate(script)
    assert result["ok"] is True  # WARN does not fail
    codes = {f["code"] for f in result["findings"]}
    assert "barrier-vs-pipeline" in codes
    finding = next(f for f in result["findings"] if f["code"] == "barrier-vs-pipeline")
    assert finding["severity"] == "WARN"


def test_legitimate_agent_mediated_barrier_does_not_hard_fail(tmp_path):
    script = _write(
        tmp_path,
        "legit-barrier.mjs",
        """\
export const meta = {
  name: 'legit-barrier',
  description: 'a real dedup barrier between two parallel stages',
};

async function run(ctx) {
  const first = await parallel(ctx.items, async (x) => {
    return await agent({ prompt: `scan ${x}`, model: 'sonnet' });
  });
  const deduped = [...new Set(first)];
  const second = await parallel(deduped, async (x) => {
    return await agent({ prompt: `verify ${x}`, model: 'sonnet' });
  });
  return second;
}
""",
    )
    result = _validate(script)
    assert result["ok"] is True
    codes = {f["code"] for f in result["findings"]}
    assert "barrier-vs-pipeline" not in codes


def test_phase_call_title_mismatch_is_warn(tmp_path):
    script = _write(
        tmp_path,
        "phase-mismatch.mjs",
        """\
export const meta = {
  name: 'phase-mismatch',
  description: 'a phase title not listed in meta.phases',
  phases: ['collect'],
};

async function run(ctx) {
  phase('collect');
  phase('unlisted-phase');
  return ctx.items;
}
""",
    )
    result = _validate(script)
    assert result["ok"] is True
    codes = {f["code"] for f in result["findings"]}
    assert "phase-call-title-mismatch" in codes
    finding = next(f for f in result["findings"] if f["code"] == "phase-call-title-mismatch")
    assert finding["severity"] == "WARN"


def test_agent_options_phase_mismatch_is_warn(tmp_path):
    script = _write(
        tmp_path,
        "agent-phase-mismatch.mjs",
        """\
export const meta = {
  name: 'agent-phase-mismatch',
  description: 'agent-options phase: not listed in meta.phases',
  phases: ['collect'],
};

async function run(ctx) {
  phase('collect');
  return await agent({
    prompt: 'do work',
    model: 'sonnet',
    phase: 'unlisted-agent-phase',
  });
}
""",
    )
    result = _validate(script)
    assert result["ok"] is True
    codes = {f["code"] for f in result["findings"]}
    assert "agent-options-phase-title-mismatch" in codes
    finding = next(
        f for f in result["findings"] if f["code"] == "agent-options-phase-title-mismatch"
    )
    assert finding["severity"] == "WARN"


# ---------------------------------------------------------------------------
# (c) missing script_path
# ---------------------------------------------------------------------------


def test_missing_script_path_raises_value_error():
    with pytest.raises(ValueError, match="script_path"):
        _run(_workflow_validate({}))


# ---------------------------------------------------------------------------
# (d) path-guard containment
# ---------------------------------------------------------------------------


def test_script_path_outside_target_root_raises_path_escape_error(tmp_path):
    inside_dir = tmp_path / "inside"
    inside_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    escaping_script = _write(outside_dir, "escape.mjs", _CONFORMANT_SCRIPT)

    with pytest.raises(PathEscapeError):
        _validate(escaping_script, target_root=inside_dir)


def test_script_path_derives_containment_root_from_parent_when_omitted(tmp_path):
    script = _write(tmp_path, "derived-root.mjs", _CONFORMANT_SCRIPT)
    result = _validate(script)  # no target_root passed
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# (e) AC7a — hand-authored fixture corpus: a large, realistic shipped-shaped
# script with populated agent() prompt bodies (false-positive guard), plus
# hand-mutated malformed variants (false-negative guard). This same large
# fixture doubles as the throwaway budget benchmark's input (see AC3 —
# benchmarked separately, not asserted on here).
# ---------------------------------------------------------------------------

LARGE_REALISTIC_SCRIPT = '''\
export const meta = {
  name: 'fleet-sweep-and-report',
  description: 'Scans every registered repo for stale branches, dispatches ' +
    'a review agent per candidate, and rolls the findings into a single report.',
  phases: ['discover', 'review', 'report'],
};

const REPO_LIST_PATH = './state/repo-registry.json';
const STALE_DAYS_THRESHOLD = 30;

async function loadRepoRegistry(ctx) {
  const raw = await ctx.readFile(REPO_LIST_PATH);
  return JSON.parse(raw);
}

function isCandidate(branch) {
  return branch.aheadBy === 0 && branch.daysSinceCommit > STALE_DAYS_THRESHOLD;
}

async function run(ctx) {
  phase('discover');

  const registry = await loadRepoRegistry(ctx);
  const repos = registry.repos ?? [];

  const branchLists = await parallel(repos, async (repo) => {
    return await agent({
      prompt: `You are auditing the repo at ${repo.path} for stale branches.
        List every local branch that has had no commits in the last ${STALE_DAYS_THRESHOLD}
        days and is not the default branch. For each one report: branch name,
        last commit sha, last commit author, and days since last commit.
        Do not delete or modify anything — this is a read-only audit pass.
        Output a JSON array of objects with keys: name, sha, author, daysSinceCommit, aheadBy.`,
      model: 'sonnet',
      phase: 'discover',
      options: { repoPath: repo.path },
    });
  });

  const allBranches = branchLists.flat();
  const candidates = allBranches.filter(isCandidate);

  phase('review');

  const reviews = await parallel(candidates, async (branch) => {
    return await agent({
      prompt: `Review branch ${branch.name} (last commit ${branch.sha} by
        ${branch.author}, ${branch.daysSinceCommit} days stale) and recommend
        one of: keep, archive, delete. Consider whether the branch name suggests
        an abandoned experiment vs a paused feature. Explain your reasoning in
        two sentences, then give the verdict as a single word on its own line.
        Note: do not attempt to compute a timestamp yourself (e.g. via
        Date.now()) — daysSinceCommit is already provided.`,
      model: 'sonnet',
      phase: 'review',
    });
  });

  const verdictCounts = { keep: 0, archive: 0, delete: 0 };
  for (const review of reviews) {
    const verdict = review.trim().split('\\n').pop().toLowerCase();
    if (verdict in verdictCounts) {
      verdictCounts[verdict] += 1;
    }
  }

  phase('report');

  const summary = await agent({
    prompt: `Summarize this stale-branch sweep for a PM audience. Candidates
      reviewed: ${candidates.length}. Verdict counts: ${JSON.stringify(verdictCounts)}.
      Write three short paragraphs: what was found, what is recommended, and
      any risk callouts. Keep it under 200 words total.`,
    model: 'sonnet',
    phase: 'report',
  });

  return {
    repoCount: repos.length,
    branchCount: allBranches.length,
    candidateCount: candidates.length,
    verdictCounts,
    summary,
  };
}

export default run;
'''


def test_large_realistic_fixture_is_ok_zero_error(tmp_path):
    script = _write(tmp_path, "large-realistic.mjs", LARGE_REALISTIC_SCRIPT)
    result = _validate(script)
    assert result["ok"] is True
    assert result["error_count"] == 0
    # Every agent() call in this fixture carries an explicit model: — no
    # model-default WARN noise expected from a well-formed corpus entry.
    codes = {f["code"] for f in result["findings"]}
    assert "agent-model-default" not in codes


def test_large_fixture_mutated_impure_meta_is_error(tmp_path):
    mutated = LARGE_REALISTIC_SCRIPT.replace(
        "description: 'Scans every registered repo for stale branches, dispatches ' +\n"
        "    'a review agent per candidate, and rolls the findings into a single report.',",
        "description: describeSweep(),",
    )
    script = _write(tmp_path, "mutated-impure-meta.mjs", mutated)
    result = _validate(script)
    assert result["ok"] is False
    codes = {f["code"] for f in result["findings"]}
    assert "meta-impure-call" in codes


def test_large_fixture_mutated_forbidden_global_is_error(tmp_path):
    mutated = LARGE_REALISTIC_SCRIPT.replace(
        "const verdictCounts = { keep: 0, archive: 0, delete: 0 };",
        "const sweepStartedAt = Date.now();\n"
        "  const verdictCounts = { keep: 0, archive: 0, delete: 0 };",
    )
    script = _write(tmp_path, "mutated-forbidden-global.mjs", mutated)
    result = _validate(script)
    assert result["ok"] is False
    codes = {f["code"] for f in result["findings"]}
    assert "forbidden-global-date-now" in codes


def test_large_fixture_mutated_phase_mismatch_is_warn_not_error(tmp_path):
    mutated = LARGE_REALISTIC_SCRIPT.replace(
        "phase('report');\n\n  const summary",
        "phase('finalize');\n\n  const summary",
    )
    script = _write(tmp_path, "mutated-phase-mismatch.mjs", mutated)
    result = _validate(script)
    assert result["ok"] is True  # WARN only, never fails
    codes = {f["code"] for f in result["findings"]}
    assert "phase-call-title-mismatch" in codes
    finding = next(f for f in result["findings"] if f["code"] == "phase-call-title-mismatch")
    assert finding["severity"] == "WARN"
