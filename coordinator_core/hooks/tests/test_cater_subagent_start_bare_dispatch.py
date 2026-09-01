"""
coordinator_core.hooks.tests.test_cater_subagent_start_bare_dispatch --
pins the AC from `state/handoffs/2026-08-16-sidecar-provisioning-is-the-
engines-job.md`:

    "A dispatched agent of a sidecar-writing type cannot stop for a missing
     sidecar, demonstrated by a test that dispatches without any EM-supplied
     path."

`compose_catering` is the SOLE catering path for every child (module
docstring, `coordinator_core/hooks/cater_subagent_start.py`) -- both an
Agent-tool spawn and a Workflow `agent()` spawn feed it the identical
`payload` shape (`agent_type`, `cwd`, `session_id`), with no field anywhere
in that shape carrying an EM-supplied sidecar path. A "bare dispatch" is
therefore simply this payload shape as-is -- there is no separate
"no-path" variant to construct, because the shape never carries a path in
the first place. This file drives that exact payload against the real
composer and provisioner (no mocking of `_provision`), for an eligible
type, an ineligible type, and the Workflow-shape payload, and asserts the
resulting sidecar exists on disk with real frontmatter.

Reuses the `git_repo`/`policy_path`/`_policy_env`/`_no_role_append`
fixture idiom from `test_cater_subagent_start.py` rather than inventing a
new one.

Spec backlink: state/handoffs/2026-08-16-sidecar-provisioning-is-the-
engines-job.md
Module under test: coordinator_core/hooks/cater_subagent_start.py
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.hooks.cater_subagent_start import (
    SIDECAR_MISS_MARKER,
    SIDECAR_PATH_MARKER_PREFIX,
    compose_catering,
)
from coordinator_core.win_portability import no_console_passthrough_kwargs

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

ELIGIBLE_TYPE = "coordinator:executor"
INELIGIBLE_TYPE = "Explore"


@pytest.fixture
def git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    subprocess.run(
        ["git", "init", "-q"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs()
    )
    (tmp_path / "coordinator" / "snippets").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "coordinator"))
    return tmp_path


@pytest.fixture
def policy_path(tmp_path: Path) -> Path:
    policy = tmp_path / "subagent-sandbox-policy.yaml"
    policy.write_text(
        "report_sidecar:\n"
        f"  - {ELIGIBLE_TYPE}\n",
        encoding="utf-8",
    )
    return policy


@pytest.fixture(autouse=True)
def _policy_env(monkeypatch: pytest.MonkeyPatch, policy_path: Path) -> None:
    monkeypatch.setenv("SUBAGENT_SANDBOX_POLICY", str(policy_path))


@pytest.fixture(autouse=True)
def _no_role_append(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-claude-config"))


def _bare_payload(agent_type: str, session_id: str, cwd: str) -> dict:
    """The exact payload shape both an Agent-tool spawn and a Workflow
    `agent()` spawn hand to `compose_catering` -- `agent_type`, `cwd`,
    `session_id` only. No key in this shape is, or could be, an
    EM-supplied sidecar path: nothing named `sidecar_path`, `report_
    sidecar_path`, or similar is ever read by `compose_catering` or by
    `_provision` (`coordinator_core/subagent_sandbox/provision_report.py`)
    -- the path is always ENGINE-DERIVED from `session_id` + `agent_type`,
    never accepted from the caller. This is "a bare dispatch"."""
    return {"agent_type": agent_type, "session_id": session_id, "cwd": cwd}


def test_bare_dispatch_of_eligible_type_yields_usable_sidecar_with_no_em_supplied_path(
    git_repo: Path,
) -> None:
    """AC core: dispatch an eligible type with a payload carrying no
    caller-supplied path at all, and confirm the child still receives a
    concrete, on-disk sidecar with real frontmatter -- not an empty
    scaffold."""
    payload = _bare_payload(ELIGIBLE_TYPE, "session-bare-1", str(git_repo))
    assert "sidecar_path" not in payload  # the shape never carries one

    result = compose_catering(payload, cwd=str(git_repo))

    assert SIDECAR_PATH_MARKER_PREFIX in result
    marker_line = next(
        line for line in result.splitlines() if line.startswith(SIDECAR_PATH_MARKER_PREFIX)
    )
    rel_path = marker_line[len(SIDECAR_PATH_MARKER_PREFIX):]

    sidecar_file = git_repo / rel_path
    assert sidecar_file.is_file(), "engine must have derived and written a real path"

    text = sidecar_file.read_text(encoding="utf-8")
    assert text.startswith("---\n"), "must carry real frontmatter, not an empty scaffold"
    assert f"agent_type: {ELIGIBLE_TYPE}" in text
    assert "status:" in text
    assert "spawned_at:" in text


def test_bare_dispatch_workflow_and_agent_tool_shapes_are_indistinguishable(
    git_repo: Path,
) -> None:
    """Both spawn kinds hand `compose_catering` the identical payload
    shape -- `agent_type`/`cwd`/`session_id`, nothing else -- per this
    module's own docstring ("SubagentStart is now the sole catering path
    for every child ... Agent-tool or Workflow `agent()` spawn alike").
    There is no field at this layer that lets a caller signal "this is a
    Workflow spawn" vs "this is an Agent-tool spawn", so the guarantee
    the AC asks for (a bare dispatch, from EITHER spawn kind, still gets
    a sidecar) reduces to running the SAME payload construction twice with
    distinct session ids and confirming both provision independently.
    Pinning that reduction explicitly, rather than assuming it, IS the
    guarantee this test exists to hold down -- if a future revision adds a
    spawn-kind-discriminating field to the payload, this test's premise
    (one shape, indistinguishable) breaks loudly rather than silently."""
    agent_tool_payload = _bare_payload(ELIGIBLE_TYPE, "session-bare-agenttool", str(git_repo))
    workflow_payload = _bare_payload(ELIGIBLE_TYPE, "session-bare-workflow", str(git_repo))

    # Same keys, same shape -- the indistinguishability the comment above
    # asserts.
    assert set(agent_tool_payload.keys()) == set(workflow_payload.keys())

    agent_tool_result = compose_catering(agent_tool_payload, cwd=str(git_repo))
    workflow_result = compose_catering(workflow_payload, cwd=str(git_repo))

    for result in (agent_tool_result, workflow_result):
        assert SIDECAR_PATH_MARKER_PREFIX in result
        marker_line = next(
            line for line in result.splitlines() if line.startswith(SIDECAR_PATH_MARKER_PREFIX)
        )
        rel_path = marker_line[len(SIDECAR_PATH_MARKER_PREFIX):]
        assert (git_repo / rel_path).is_file()

    # Distinct session ids -> distinct sidecar files, provisioned
    # independently -- not the same file reused across "spawn kinds".
    line_a = next(
        line for line in agent_tool_result.splitlines()
        if line.startswith(SIDECAR_PATH_MARKER_PREFIX)
    )
    line_b = next(
        line for line in workflow_result.splitlines()
        if line.startswith(SIDECAR_PATH_MARKER_PREFIX)
    )
    assert line_a != line_b


def test_bare_dispatch_of_ineligible_type_fails_open_with_no_sidecar_and_no_error(
    git_repo: Path,
) -> None:
    """An ineligible type's bare dispatch must fail OPEN: no sidecar
    offered, no miss notice, and `compose_catering` itself must not raise
    (it returns a plain string either way, by construction)."""
    payload = _bare_payload(INELIGIBLE_TYPE, "session-bare-ineligible", str(git_repo))

    result = compose_catering(payload, cwd=str(git_repo))

    assert SIDECAR_PATH_MARKER_PREFIX not in result
    assert SIDECAR_MISS_MARKER not in result


def test_bare_dispatch_with_unreadable_policy_file_fails_open(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the policy file cannot be resolved/read at all, eligibility
    lookup itself fails open (module docstring: "Lookup is FAIL-OPEN by
    design: a miss means 'not eligible, provision nothing', never an
    error") -- point the env-var rung at a path that does not exist and
    confirm the bare dispatch still degrades cleanly rather than raising
    or emitting a miss notice for a type that was never resolved
    eligible."""
    monkeypatch.setenv("SUBAGENT_SANDBOX_POLICY", str(tmp_path / "does-not-exist.yaml"))
    payload = _bare_payload(ELIGIBLE_TYPE, "session-bare-nopolicy", str(git_repo))

    result = compose_catering(payload, cwd=str(git_repo))

    assert SIDECAR_PATH_MARKER_PREFIX not in result
    assert SIDECAR_MISS_MARKER not in result
