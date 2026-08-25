"""
coordinator_core.hooks.tests.test_cater_subagent_start_spawn_budget

C2 (state/dispatch-briefs/2026-08-21-catering-costs-what-the-work-costs/C2.md):
gives `cater_subagent_start.compose_catering` a SPAWN-COUNT budget of ZERO --
it must never shell out to `git`, on either of the two root-resolution call
sites this chunk repointed: `compose_catering`'s own `resolve_git_root` call
(for `resolve_effective_types`) and `assemble_contract_blocks_for_payload`'s
independent re-resolution (module docstring there: "deliberately NOT
threaded through from `_provision`").

Modelled on `coordinator_core/ops/ceremony/tests/test_commit_e2e_spawn_budget.py`
`_count_op_spawns_both_ways`: patches `subprocess.Popen` (the mechanism
`subprocess.run` itself delegates to internally, so a single patch point
catches both call shapes) over the WHOLE catering path and asserts zero
calls, rather than asserting on a measured figure -- AC1 is this guard, not
a number: a number decays, a guard does not (brief).

Scope note: this fixture deliberately selects an `agent_type` NOT present in
`policy.report_sidecar`, so `_resolve_sidecar_leg` short-circuits before ever
calling `_provision` -- `_provision`'s own `resolve_git_root` call is OUT OF
SCOPE for this chunk (brief: "Two call sites, not one ... compose_catering's
own resolve_git_root, and the re-resolution inside
assemble_contract_blocks_for_payload"), and still spawns via
`coordinator_core.subagent_sandbox.engine.resolve_git_root` (see that
module's own docstring for why it cannot walk instead: its caching-contract
pinning tests require an unconditional spawn). A payload that reached the
sidecar-provisioning leg would fail this test for a reason this chunk does
not own fixing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.hooks import cater_subagent_start

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    return repo


def _count_popen_spawns(fn):
    """Run *fn* with `subprocess.Popen` (the primitive `subprocess.run`
    itself delegates to) counted and passed through unmodified -- a single
    patch point that sees every spawn shape reachable from `fn`, mirroring
    `_count_op_spawns_both_ways`'s own `all_n` leg in the sibling module this
    test is modelled on."""
    calls = {"n": 0}
    orig_popen = subprocess.Popen

    def _popen_wrapper(*a, **kw):
        calls["n"] += 1
        return orig_popen(*a, **kw)

    subprocess.Popen = _popen_wrapper
    try:
        result = fn()
    finally:
        subprocess.Popen = orig_popen
    return calls["n"], result


def test_compose_catering_never_spawns_git(tmp_path_factory):
    """AC1: `compose_catering`, given a payload that reaches BOTH the
    `resolve_effective_types` leg and the `assemble_contract_blocks_for_
    payload` leg, issues ZERO `git` subprocesses -- a guard, not a
    measurement, per the brief."""
    tmp_path = tmp_path_factory.mktemp("cater-spawn-budget")
    repo = _init_repo(tmp_path)

    payload = {
        "agent_type": "some-unenumerated-ineligible-type",
        "session_id": "sess-spawn-budget",
        "cwd": str(repo),
        "contract_blocks": ["a-block-name-that-need-not-exist"],
    }

    n, _text = _count_popen_spawns(
        lambda: cater_subagent_start.compose_catering(payload, cwd=str(repo))
    )

    assert n == 0, (
        "compose_catering spawned %d subprocess(es) -- the two root-"
        "resolution call sites this chunk repointed to "
        "coordinator_core.git.repo_root.show_toplevel (a non-spawning "
        "walker) must never shell out to git; a regression here reintroduced "
        "a spawn on the catering path this chunk's whole point was to "
        "eliminate" % n
    )


def test_compose_catering_never_spawns_git_with_no_git_repo(tmp_path_factory):
    """Same guard, miss-mode: a `cwd` with no `.git` ancestor at all. The
    non-spawning walker fails open to `None` here (never a spawn to find
    out), matching this chunk's own eligibility argument: every leg fed by
    a missed root degrades to "" / no-blocks, never a wrong verdict."""
    tmp_path = tmp_path_factory.mktemp("cater-spawn-budget-miss")
    no_repo_dir = tmp_path / "not-a-repo"
    no_repo_dir.mkdir()

    payload = {
        "agent_type": "some-unenumerated-ineligible-type",
        "session_id": "sess-spawn-budget-miss",
        "cwd": str(no_repo_dir),
        "contract_blocks": ["a-block-name-that-need-not-exist"],
    }

    n, _text = _count_popen_spawns(
        lambda: cater_subagent_start.compose_catering(payload, cwd=str(no_repo_dir))
    )

    assert n == 0, (
        "compose_catering spawned %d subprocess(es) resolving a repo root "
        "for a cwd with no .git ancestor -- the walker must fail open to "
        "None without ever shelling out" % n
    )
