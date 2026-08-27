"""
coordinator_core.workstream_complete.tests.test_receipt_path_absolute_budgets
-- AC12 / AC12b / AC12c (docs/plans/2026-08-27-the-review-gate-measures-the-
whole-session.md, C7): absolute ceilings on both legs the review-receipt
design touches, asserted in tests, never in prose.

Two legs, two instruments:

  - CLOSE leg (`workstream_complete.brief()`): whole PROCESS TREE, job-
    accounted via `coordinator_core.benchmarks.process_time.
    batched_process_time_ms`, run as a real subprocess so the job object
    actually sees a process tree to account -- < 400ms process time, and a
    Python-issued spawn census of exactly 0. `test_gate_path_spawn_budget.py`
    already bounds GIT spawns specifically; this file bounds the WHOLE
    process tree the job object can see, which is strictly wider and never
    substitutes for that file.

  - DISPATCH leg (`provision_report._provision`'s review-receipt splice):
    structural assertions first (no new imports over the non-reviewer path,
    no `commit_ledger.resolve_owner`/`baton_assemble` on it, zero subprocess
    spawns), plus a < 1ms marginal-cost NUMERIC BACKSTOP measured against the
    identical sidecar write with the receipt splice omitted. The structural
    legs are the actual gate; the number only catches an implementation that
    evades all three (chunk brief).

AC12c, satisfied by construction: every ceiling below is an ABSOLUTE literal
(400ms, 0 spawns, 1ms) baked into the assertion -- none of these tests reads
a prior measurement, a git ref, or a "before" sample and compares against
it. There is no relative/no-regression branch anywhere in this file to
audit away; the criterion is discharged by the absence of that shape, not
by a test that specifically exercises its absence.

Baselines this file re-measures (never quotes) against the plan's own
§ Performance plan figures (measured there at `b40cdfde1`): close leg
~296.9-365.6ms / 0 spawns; dispatch-leg marginal ~0.0005ms over a 0.1932ms
compose+write.

Spec backlink: state/dispatch-briefs/2026-08-27-the-review-gate-measures-
the-whole-session/C7.md

Negative-spec -- do NOT "fix" while reading this module:
    - Does not assert on `directives[]` content, judgment shapes, or
      anything `test_workstream_complete.py`/`test_review_receipt_gates_
      delivered_close.py` already own -- this file owns exactly the two
      absolute performance ceilings named above.
    - Does not compare against a stashed/checked-out prior revision, a git
      log figure, or any other "no worse than before" shape (AC12c).
    - Does not reintroduce `commit_ledger.resolve_owner`/`baton_assemble`
      "just to check the number" -- their absence is asserted structurally,
      never exercised.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.benchmarks.process_time import batched_process_time_ms
from coordinator_core.subagent_sandbox.engine import load_policy, resolve_git_root
from coordinator_core.subagent_sandbox.provision_report import (
    _build_doc_text,
    _is_delegate_reviewer,
    _provision,
    _splice_review_receipt,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

# ---------------------------------------------------------------------------
# Close leg -- brief()'s whole process tree, job-accounted.
# ---------------------------------------------------------------------------

_REPO_ROOT = str(Path(__file__).resolve().parents[3])

#: 400ms absolute ceiling (AC12), against a measured ~300-365ms baseline
#: (§ Performance plan) -- ~100ms headroom, still inside the 500ms
#: brightline. Never derived from a prior run of THIS file.
CLOSE_LEG_PROCESS_TIME_CEILING_MS: float = 400.0

#: `brief()` must issue zero Python-issued subprocess spawns beyond the
#: fixture-setup git calls, which the fixture builds ONCE, OUTSIDE the
#: timed driver (see `_build_close_leg_fixture` below) -- the driver itself
#: only ever calls `brief()`. Counted via job-object process count, which
#: sees EVERY spawned process, not only ones whose argv[0] is `git`
#: (`test_gate_path_spawn_budget.py`'s own instrument is argv-filtered to
#: `git` specifically; this one is not).
CLOSE_LEG_SPAWN_CENSUS_CEILING: int = 0

K_INVOCATIONS = 10

#: Driver reads its fixture's repo root from argv[1] -- the fixture is
#: built once, before timing starts, never inside the timed process (a
#: `git init`/`git commit` sequence run k times INSIDE the measured
#: process would inflate both the process-time and spawn-count figures
#: with fixture-setup cost this leg's brief() call never pays in
#: production).
_CLOSE_LEG_DRIVER = (
    "import sys\n"
    "from pathlib import Path\n"
    "import coordinator_core.workstream_complete as wsc\n"
    "d = Path(sys.argv[1])\n"
    "sid = 'c7-absolute-budget-sid'\n"
    "gate = wsc.SessionShapeGate(\n"
    "    sid=sid,\n"
    "    disposition='single-session',\n"
    "    consumed_handoff='',\n"
    "    diagnostics=[],\n"
    "    consumed_handoff_paths=(),\n"
    "    detection={},\n"
    ")\n"
    "wsc.compute_session_shape_gate = lambda root: gate\n"
    "wsc.brief(decisions={}, repo_root=d)\n"
)


def _build_close_leg_fixture(repo: Path) -> None:
    """A real git repo carrying the seeded session claim dir --
    `test_gate_path_spawn_budget.py`'s own fixture (and its module
    docstring) established that `brief()` call 1 (no `stage_paths`) reaches
    a true zero-git-spawn fast path only when a claim dir for the session
    id exists; a bare `tmp_path` with no `.git` at all falls through
    `resolve_session_start_time`'s 5-candidate `merge-base`/`log` ladder
    and spawns 6 git processes measuring THAT ladder's cost, not brief()'s
    real fast path. Built once here, never inside the timed driver."""
    ncw = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, creationflags=ncw)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        creationflags=ncw,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=repo, check=True, creationflags=ncw
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True, creationflags=ncw)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True, creationflags=ncw)
    claim_dir = repo / ".git" / "coordinator-sessions" / "c7-absolute-budget-sid"
    claim_dir.mkdir(parents=True)


def test_close_leg_process_tree_stays_under_400ms_with_zero_spawns(tmp_path: Path):
    """AC12: one live `brief()` call, run as a real subprocess so
    `batched_process_time_ms`'s job-object accounting has an actual process
    tree to measure -- process time under the absolute 400ms ceiling, and
    the job object's own process count (root interpreter + every process it
    spawns, not merely `git`-named ones) derives to exactly 0 spawns.

    The fixture repo is built ONCE, before any timing starts; the k
    repeated driver invocations below re-read the SAME repo (matches
    `batched_process_time_ms`'s own "re-runs the SAME argv k times" idiom
    elsewhere in this repo) -- `decisions={}` call 1 does not mutate it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _build_close_leg_fixture(repo)

    result = batched_process_time_ms(
        [sys.executable, "-c", _CLOSE_LEG_DRIVER, str(repo)], k=K_INVOCATIONS, cwd=_REPO_ROOT
    )
    assert result["rc"] == 0, (
        f"close-leg driver exited rc={result['rc']} -- a failing invocation "
        "cannot stand in for a valid process-time/spawn-count sample"
    )
    assert result["process_time_ms"] <= CLOSE_LEG_PROCESS_TIME_CEILING_MS, (
        f"brief()'s whole process tree regressed: {result['process_time_ms']}ms "
        f"exceeds the absolute {CLOSE_LEG_PROCESS_TIME_CEILING_MS}ms ceiling "
        f"(k={result['k']}, wall_ms={result['wall_ms']} for context only)."
    )

    spawn_count = max(0, round(result["procs_per_call"]) - 1)
    assert spawn_count <= CLOSE_LEG_SPAWN_CENSUS_CEILING, (
        f"brief() issued {spawn_count} Python-issued spawn(s) beyond the root "
        f"interpreter (procs_per_call={result['procs_per_call']!r}), exceeding "
        f"the absolute {CLOSE_LEG_SPAWN_CENSUS_CEILING} ceiling. This census "
        "is job-object process count, not an argv[0]=='git' filter, so it "
        "catches a non-git spawn `test_gate_path_spawn_budget.py` never sees."
    )


# ---------------------------------------------------------------------------
# Dispatch leg -- provision_report._provision's review-receipt splice.
# ---------------------------------------------------------------------------

REVIEWER_TYPE = "coordinator:code-reviewer"
NON_REVIEWER_ELIGIBLE_TYPE = "coordinator:executor"

_MODULES_BASELINE_ARGV = [
    sys.executable,
    "-c",
    (
        "import json, sys\n"
        "before = set(sys.modules)\n"
        "from coordinator_core.subagent_sandbox.provision_report import (\n"
        "    _build_doc_text, _is_delegate_reviewer,\n"
        ")\n"
        "after_import = set(sys.modules)\n"
        "doc = _build_doc_text('coordinator:executor', '2026-01-01T00:00:00Z', None, lead_session_id='sid')\n"
        "_is_delegate_reviewer('coordinator:executor', '')\n"
        "print(json.dumps(sorted(set(sys.modules) - before)))\n"
    ),
]

_MODULES_RECEIPT_ARGV = [
    sys.executable,
    "-c",
    (
        "import json, sys\n"
        "before = set(sys.modules)\n"
        "from coordinator_core.subagent_sandbox.provision_report import (\n"
        "    _build_doc_text, _is_delegate_reviewer, _splice_review_receipt,\n"
        ")\n"
        "doc = _build_doc_text('coordinator:code-reviewer', '2026-01-01T00:00:00Z', None, lead_session_id='sid')\n"
        "if _is_delegate_reviewer('coordinator:code-reviewer', ''):\n"
        "    doc = _splice_review_receipt(doc, 'sid', 'agent-1', 'coordinator:code-reviewer', '2026-01-01T00:00:00Z')\n"
        "print(json.dumps(sorted(set(sys.modules) - before)))\n"
    ),
]


def _run_modules_probe(argv) -> "set[str]":
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=_REPO_ROOT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, (
        f"modules probe failed: rc={result.returncode}, stderr={result.stderr!r}"
    )
    import json as _json

    return set(_json.loads(result.stdout.strip().splitlines()[-1]))


def test_dispatch_leg_receipt_splice_imports_nothing_new():
    """AC12b(i): the receipt path (a delegate-reviewer dispatch, which
    reaches `_splice_review_receipt`) must import no module that a
    non-reviewer eligible dispatch (which reaches `_is_delegate_reviewer`
    and stops there, per `_provision`'s own unconditional call to it on
    every eligible dispatch) does not already load. Measured as a set
    difference in two fresh interpreters, never inspected by reading the
    source -- a real import graph, not an assumption about one."""
    baseline_modules = _run_modules_probe(_MODULES_BASELINE_ARGV)
    receipt_modules = _run_modules_probe(_MODULES_RECEIPT_ARGV)

    new_modules = receipt_modules - baseline_modules
    assert not new_modules, (
        "the review-receipt splice imported module(s) beyond what a "
        f"non-reviewer eligible dispatch already loads: {sorted(new_modules)}"
    )


def test_dispatch_leg_receipt_splice_never_reaches_resolve_owner_or_baton_assemble():
    """AC12b(ii): explicit, independent of the set-difference check above --
    a future change adding one of these two under a DIFFERENT already-loaded
    umbrella module would slip past a pure set-difference against a moving
    baseline that also grew. Named directly, both ways: the module dotted
    path must be absent from `sys.modules` outright."""
    receipt_modules = _run_modules_probe(_MODULES_RECEIPT_ARGV)
    banned = {
        m
        for m in receipt_modules
        if "commit_ledger.resolve_owner" in m or "baton_assemble" in m
    }
    assert not banned, (
        f"the review-receipt splice reached a banned module: {sorted(banned)} "
        "-- AC12b(ii) forbids commit_ledger.resolve_owner and baton_assemble "
        "on the dispatch path (§ Performance plan: 101.4ms cold, 4 orders of "
        "magnitude over the ceiling)."
    )


def test_dispatch_leg_receipt_splice_issues_zero_subprocess_spawns():
    """AC12b(iii): a `subprocess.Popen.__init__` census (same choke-point
    idiom as `test_gate_path_spawn_budget.py`'s own `_wrap_popen_for_git_
    spawn_count`, generalised to every spawn, not only `git`) around the
    exact compose-and-splice call the dispatch-time receipt performs, in
    this process -- no subprocess tree needed to prove ZERO children exist."""
    calls: "list[list[str]]" = []
    real_init = subprocess.Popen.__init__

    def _fake_init(self, args, *a, **kw):
        argv = args if isinstance(args, (list, tuple)) else [args]
        calls.append([str(x) for x in argv])
        return real_init(self, args, *a, **kw)

    import unittest.mock as mock

    with mock.patch.object(subprocess.Popen, "__init__", _fake_init):
        doc = _build_doc_text(
            REVIEWER_TYPE, "2026-01-01T00:00:00Z", None, lead_session_id="sid"
        )
        assert _is_delegate_reviewer(REVIEWER_TYPE, "")
        _splice_review_receipt(doc, "sid", "agent-1", REVIEWER_TYPE, "2026-01-01T00:00:00Z")

    assert calls == [], (
        f"the review-receipt splice issued {len(calls)} subprocess spawn(s), "
        f"expected 0: {calls}"
    )


#: AC12b(iv): < 1ms marginal cost, absolute -- against the plan's own
#: measured 0.0005ms marginal over a 0.1932ms compose+write (§ Performance
#: plan). Re-measured below rather than quoted, per this plan's own
#: discipline; the ceiling itself stays the fixed literal AC12b names, not
#: a multiple of whatever this file happens to observe.
DISPATCH_LEG_MARGINAL_COST_CEILING_MS: float = 1.0

_MARGINAL_MEASURE_REPS = 200


def _provision_once(
    *, git_root: Path, policy_path: Path, agent_type: str
) -> None:
    payload = {"agent_type": agent_type, "session_id": "sid-marginal-cost"}
    _provision(payload, str(policy_path), str(git_root))


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    subprocess.run(
        ["git", "init", "-q"],
        cwd=tmp_path,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return tmp_path


@pytest.fixture
def policy_path(tmp_path: Path) -> Path:
    import yaml

    policy = {
        "confined": [],
        "exempt": [],
        "sanctioned_dirs": [],
        "report_sidecar": [REVIEWER_TYPE, NON_REVIEWER_ELIGIBLE_TYPE],
    }
    path = tmp_path / "subagent-sandbox-policy.yaml"
    path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    return path


def test_dispatch_leg_receipt_marginal_cost_is_under_one_millisecond(
    git_repo: Path, policy_path: Path
):
    """AC12b(iv) backstop: median `time.process_time()` cost of a real
    `_provision()` call for a delegate-reviewer dispatch (receipt spliced
    in) minus the identical call for a non-reviewer eligible dispatch
    (receipt omitted, otherwise byte-identical compose+write shape) --
    both against the SAME git_repo/policy fixtures, no caller-supplied
    `provision_key` on either leg so each call takes the nonce-per-call
    branch (matches `_provision`'s own real dispatch-time code path,
    never the idempotent provision_key branch).

    Repeated `_MARGINAL_MEASURE_REPS` times per leg and MEDIANED (never a
    single sample) for the same reason `test_ceremony_brief_budget.py`
    medians its own `time.process_time()` reads: a sub-millisecond delta is
    well inside this box's GC/scheduler jitter on any single sample."""

    def _median_process_time_ms(agent_type: str) -> float:
        samples = []
        for _ in range(_MARGINAL_MEASURE_REPS):
            t0 = time.process_time()
            _provision_once(git_root=git_repo, policy_path=policy_path, agent_type=agent_type)
            samples.append((time.process_time() - t0) * 1000.0)
        samples.sort()
        return samples[len(samples) // 2]

    without_receipt_ms = _median_process_time_ms(NON_REVIEWER_ELIGIBLE_TYPE)
    with_receipt_ms = _median_process_time_ms(REVIEWER_TYPE)

    marginal_ms = with_receipt_ms - without_receipt_ms
    assert marginal_ms <= DISPATCH_LEG_MARGINAL_COST_CEILING_MS, (
        f"review-receipt marginal cost regressed: {marginal_ms}ms "
        f"(with-receipt median {with_receipt_ms}ms, without-receipt median "
        f"{without_receipt_ms}ms) exceeds the absolute "
        f"{DISPATCH_LEG_MARGINAL_COST_CEILING_MS}ms ceiling (§ Performance "
        "plan: measured 0.0005ms over a 0.1932ms write at this plan's own "
        "baseline -- this is a backstop, not the primary gate; the "
        "structural tests above are)."
    )
