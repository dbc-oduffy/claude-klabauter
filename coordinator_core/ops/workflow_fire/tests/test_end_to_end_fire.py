"""
coordinator_core.ops.workflow_fire.tests.test_end_to_end_fire — the loop
close: emit -> fire -> commit (AC9).

Purpose: this is the ONE test in the fleet that fires a real, emitted
workflow script end to end and checks its commit phase against a real git
repo, discharging AC9 -- the check that closes the loop the originating
memo opened. Every other test in this package family (``test_fire.py``)
monkeypatches ``subprocess.Popen``; this file is the deliberate exception,
scoped to two halves:

  1. Emit-only (``test_emit_synthetic_spine_is_top_level_and_has_a_commit_
     phase``) -- no child process, ordinary fast-tier test. Composes a
     synthetic one-row plan spine via ``dispatch.emit`` and asserts the
     emitted script text is TOP-LEVEL (never a defined-but-uninvoked
     ``async function run(ctx) { ... }`` wrapper -- C5 fixed this; an inert
     script would fire "successfully" while dispatching zero agents) and
     names a ``coordinator:git-commit-agent`` commit phase.
  2. Live-fire (``test_fired_workflow_lands_a_real_commit_in_the_scratch_
     repo``) -- spawns one real, detached ``claude -p`` child via
     ``fire.fire_workflow`` against the emitted script, polling
     ``fire.fire_status`` to (near-)completion. Marked ``cadence``
     (docs/reference/test-tiers.md: "Heavy suites, cadence-gate tier
     only") -- NEVER collected by the default fast tier. Running it costs
     one real headless Claude session driving several dispatched
     ``coordinator:*`` subagents (preflight, one executor wave, one
     commit, one test-runner phase) -- real wall time (minutes) and real
     tokens, not free, hence opt-in only.

Runs against a throwaway ``tmp_path`` git repo, NEVER this tree -- this
repo's branch is frozen under a git-history incident and nothing may
commit into it (this chunk's brief). The synthetic spine's ``writes:``
path and its co-located test file both live under that scratch repo, and
the fired child's working directory is pointed at it via ``os.chdir``
before firing (``fire.fire_workflow`` spawns via a bare ``subprocess.
Popen`` with no ``cwd=`` override -- see ``fire.py``'s ``popen_kwargs`` --
so the ONLY way to steer the fired session's working tree is the firing
process's own cwd at spawn time).

Known-red, honestly pinned, not worked around -- and the reason is LOCAL,
measured 2026-08-18, not the cross-repo gate an earlier draft of this
docstring blamed.

``fire.build_fire_command`` passes ``--allowedTools Workflow``. That grant
scopes the whole fired SESSION, not just its driver: every agent the
workflow spawns inherits it, so an executor phase's Write/Edit and a
test-runner phase's Bash are both refused inside the child. Two probes
settled it, same script, same plugin dir, differing only in the flag --
with ``--allowedTools Workflow`` the spawned ``coordinator:executor``
reported its write refused and no file appeared; with
``--allowedTools Workflow Write Edit`` the same executor wrote the file and
returned DONE. The spike this parameter came from only ever ran phases that
returned a value without editing or running anything, which is why the gap
survived to here.

Two consequences for whoever reads a red run:

  - The driver reports ``is_error: false``, ``terminal_reason: completed``,
    and an EMPTY ``permission_denials`` array while its phases did nothing.
    A per-phase refusal is invisible at the driver's summary. Read the
    fire's ``log_path``, never the exit status, to learn what the phases
    actually did.
  - The commit phase's ``coordinator:git-commit-agent`` and the CROSS-REPO
    ``SubagentStart``/``agent_type`` gate (doe-claude-em's ``hooks.json``
    sending the placeholder, making
    ``block_reviewer_bash_outside_allowlist`` confine the committer) sit
    DOWNSTREAM of this and have not been reached yet -- the executor phase
    is blocked first, so the commit phase finds nothing staged. Do not
    attribute a red here to that gate without evidence from the log that
    the run got that far.

Widening the grant is deliberately NOT done here: handing a detached,
unattended child file-write and Bash authority is a trust-surface decision
for the PM, not an executor's or this test's to take. This test does not
special-case, retry around, or soften the outcome in any way.

Spec backlink: docs/plans/2026-08-18-claude-klabauter-fires-the-workflows-it-emits.md
§ C6, AC9.

Negative-spec:
  - Does NOT touch claude-klabauter's own tree with any fired commit -- every
    write, both emitted-script writes: paths and the live commit target,
    resolve under a per-test ``tmp_path`` scratch repo.
  - Does NOT monkeypatch ``subprocess.Popen`` or the plugin-dir resolution
    seam -- that is ``test_fire.py``'s job; this file's live half is real
    on purpose.
  - Does NOT retry, xfail-wrap, or soften the known-red phase refusal
    described above -- a red here is reported as-is.
  - Does NOT widen ``--allowedTools`` to make itself pass. The grant a
    fired workflow's phases run under is a PM decision; a test that grants
    itself the authority it is measuring proves nothing.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
from coordinator_core.ops.workflow_fire import fire

#: Bounded wall-clock budget for polling a live fire to a terminal state.
#: Generous -- the fired session drives four real dispatched-agent phases
#: (preflight, one wave, one commit, one test) -- but not unbounded; see
#: module docstring's load-norm-aware cost note.
_LIVE_FIRE_POLL_BUDGET_S = 480
_LIVE_FIRE_POLL_INTERVAL_S = 5


def _git(args: list, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _seed_scratch_repo(tmp_path: Path) -> Path:
    """Build a throwaway git repo with one Python module and its co-located
    test file, both committed -- the synthetic spine's ``writes:`` target
    and the terminal test scope's required co-located test file (``pathspec.
    _map_written_path_to_test_target`` needs a real ``tests/test_<stem>.py``
    on disk to resolve a runnable test target, else the emit step refuses
    with ``NoTestTargetError``)."""
    repo_dir = tmp_path / "scratch-repo"
    repo_dir.mkdir()
    _git(["init", "-q"], repo_dir)
    _git(["config", "user.email", "fixture@example.invalid"], repo_dir)
    _git(["config", "user.name", "Fixture"], repo_dir)

    module_path = repo_dir / "marker_module.py"
    module_path.write_text('MARKER = "unfired"\n', encoding="utf-8")

    tests_dir = repo_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_marker_module.py").write_text(
        "from marker_module import MARKER\n\n\ndef test_marker():\n    assert MARKER\n",
        encoding="utf-8",
    )

    _git(["add", "-A"], repo_dir)
    _git(["commit", "-q", "-m", "seed scratch repo"], repo_dir)
    return repo_dir


_SYNTHETIC_PLAN = textwrap.dedent(
    """\
    ---
    title: "Synthetic end-to-end fire fixture"
    created: 2026-08-18
    author: test
    status: draft
    branch: "work/fixture"
    plan_id: "pln-e2e-fixture"
    deliverable_id: "dlv-e2e-fixture"
    initiative: null
    sizing_object: "state/sizings/e2e-fixture.yaml"
    scope_mode: feature
    problem_set: inline
    ---

    # Synthetic end-to-end fire fixture

    ## Tasks

    ```yaml plan-tasks
    - id: E1
      title: Flip the marker module's value
      change_kind: code-edit
      surface: marker_module.py
      writes:
        - marker_module.py
      reads: []
      queue_scope: project
      disposition: open
      body: |
        Change MARKER's string value in marker_module.py to "fired".
    ```
    """
)


def _emit_into(scratch_repo: Path) -> Path:
    plan_path = scratch_repo.parent / "e2e-plan.md"
    plan_path.write_text(_SYNTHETIC_PLAN, encoding="utf-8")
    output_path = scratch_repo / "e2e-emitted.mjs"

    result = _dispatch_emit(
        {
            "plan_path": str(plan_path),
            "output_path": str(output_path),
        },
        repo_root=scratch_repo,
    )
    assert result["ok"] is True, result["findings"]
    return output_path


def test_emit_synthetic_spine_is_top_level_and_has_a_commit_phase(tmp_path):
    scratch_repo = _seed_scratch_repo(tmp_path)
    output_path = _emit_into(scratch_repo)

    script = output_path.read_text(encoding="utf-8")

    # C5's fix: a top-level script, never a defined-but-uninvoked wrapper --
    # an inert script fires "successfully" while dispatching zero agents.
    assert "async function run(" not in script
    assert "async function run(ctx)" not in script

    assert "coordinator:git-commit-agent" in script
    assert "Commit wave 1" in script
    assert 'phase("Wave 1' in script or "phase('Wave 1" in script


@pytest.mark.cadence
@pytest.mark.real_home
def test_fired_workflow_lands_a_real_commit_in_the_scratch_repo(tmp_path):
    """Live fire -- see module docstring for cost, cwd-steering, and why a
    red here is currently the LOCAL ``--allowedTools`` grant rather than the
    cross-repo gate. A red needs the fired session's own log (this test's
    failure message names ``log_path``) read before concluding
    ``workflow_fire``/``dispatch_emit`` regressed.

    ``@pytest.mark.real_home`` -- the fired ``claude -p`` child needs the
    REAL credential store to authenticate; ``coordinator_core/conftest.py``'s
    autouse ``_quarantine_real_home`` redirects HOME/USERPROFILE into a
    throwaway dir for every other test, and ``fire.py``'s ``Popen`` passes no
    ``env=`` override, so a quarantined child inherits that throwaway HOME,
    finds no credentials, and dies at "Not logged in" before reaching any
    phase -- indistinguishable from a real auth outage without reading the
    log. This test is otherwise READ-ONLY against the real home: every
    write it makes (scratch repo, emitted script, fire registry) targets
    ``tmp_path``, asserted below, never the real home the marker restores.
    """
    scratch_repo = _seed_scratch_repo(tmp_path)
    assert scratch_repo.is_relative_to(tmp_path), (
        "scratch_repo must stay under tmp_path -- this test runs with "
        "@pytest.mark.real_home, so nothing of its own may write under the "
        "real home the marker hands back"
    )
    output_path = _emit_into(scratch_repo)

    head_before = _git(["rev-parse", "HEAD"], scratch_repo).stdout.strip()

    original_cwd = os.getcwd()
    os.chdir(scratch_repo)
    try:
        record = fire.fire_workflow(str(output_path), cwd=str(scratch_repo))
    finally:
        os.chdir(original_cwd)

    fire_id = record["fire_id"]
    log_path = record["log_path"]

    deadline = time.monotonic() + _LIVE_FIRE_POLL_BUDGET_S
    status = record
    while time.monotonic() < deadline:
        status = fire.fire_status(fire_id, cwd=str(scratch_repo))
        if status is not None and status.get("state") != "running":
            break
        time.sleep(_LIVE_FIRE_POLL_INTERVAL_S)

    head_after = _git(["rev-parse", "HEAD"], scratch_repo).stdout.strip()

    assert head_after != head_before, (
        "fired workflow's commit phase did not land a new commit in the "
        f"scratch repo within {_LIVE_FIRE_POLL_BUDGET_S}s -- fire_id="
        f"{fire_id!r}, final status={status!r}, read the fired session's "
        f"own log before concluding this repo's code regressed: {log_path}"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
