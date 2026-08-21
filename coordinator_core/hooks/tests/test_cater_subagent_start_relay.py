"""
coordinator_core.hooks.tests.test_cater_subagent_start_relay -- pytest harness
for the C3 relay: `hooks.cater_subagent_start` registered so the existing
SubagentStart shim can relay it via `ipc.py :: dispatch_ops_from_hook`
alongside `hooks.track_dispatched_agents`, bookkeeping op FIRST.

Spec backlink: docs/plans/2026-08-21-catering-rides-subagentstart.md (C3)
Module under test: coordinator_core/hooks/cater_subagent_start.py

ORDER MATTERS AND IS TESTABLE (plan body): `resolve_effective_types` resolves
a named dispatch's `subagent_type` through a back-pointer chain
(`.agents/<agent_id>/em-session-id.txt` -> `<em_sid>/dispatched-agents.txt`)
that `hooks.track_dispatched_agents` writes on this SAME SubagentStart event.
The tests below build a cater payload carrying no `agent_type` at all, so
eligibility can ONLY be resolved through that back-pointer -- the
discriminator that makes bookkeeping-first vs. bookkeeping-second produce a
DIFFERENT, deterministically observable result rather than a flaky one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.ipc as ipc
from coordinator_core.hooks.cater_subagent_start import OP_NAME

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

ELIGIBLE_TYPE = "coordinator:code-reviewer"
#: Bare-hex, unnamed-agent id shape (>=12 hex chars) -- accepted by both
#: track_dispatched_agents._valid_agent_id and subagent_sandbox.engine's
#: _canonical_agent_id / _BARE_HEX_RE, and passed through UNCHANGED by both
#: (no teammate-form normalization to reason about here).
AGENT_ID = "abcdef0123456789"
SESSION_ID = "session-relay-1"


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
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
    """`compose_catering` always resolves policy via `load_policy(None)`'s
    own cascade -- the env-var rung is this fixture's control point."""
    monkeypatch.setenv("SUBAGENT_SANDBOX_POLICY", str(policy_path))


@pytest.fixture(autouse=True)
def _no_role_append(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate from whatever plugin happens to be installed on the machine
    running this test -- role framing fails open to "" and stays out of the
    way of the assertions below, which are about the sidecar leg."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-claude-config"))


def _bookkeeping_params() -> dict:
    return {
        "session_id": SESSION_ID,
        "dispatched_agent_id": AGENT_ID,
        "dispatched_model": "claude-x",
        "subagent_type": ELIGIBLE_TYPE,
    }


def _cater_params(cwd: str) -> dict:
    # Deliberately NO agent_type: the sidecar leg's eligibility check
    # (_resolve_sidecar_leg) OR-resolves agent_type with subagent_type, and
    # subagent_type is resolvable ONLY through the back-pointer
    # hooks.track_dispatched_agents just wrote -- the exact seam the plan
    # names.
    return {
        "agent_id": AGENT_ID,
        "session_id": SESSION_ID,
        "cwd": cwd,
    }


def _additional_context(result: dict) -> str:
    return result.get("hookSpecificOutput", {}).get("additionalContext", "")


# ---------------------------------------------------------------------------
# Registration -- no second registration (Anti-scope)
# ---------------------------------------------------------------------------

def test_cater_op_registered_under_documented_name_alongside_bookkeeping() -> None:
    """Both ops must be reachable off the SAME `ipc._REGISTRY` the
    SubagentStart shim's `dispatch_ops_from_hook` call resolves against --
    this op takes no separate hooks.json registration of its own."""
    assert OP_NAME == "hooks.cater_subagent_start"
    assert ipc._REGISTRY.get(OP_NAME) is not None
    assert ipc._REGISTRY.get("hooks.track_dispatched_agents") is not None


# ---------------------------------------------------------------------------
# Order -- pinned, not left to timing
# ---------------------------------------------------------------------------

def test_bookkeeping_first_caters_the_named_dispatch_via_backpointer(git_repo: Path) -> None:
    """Bookkeeping op FIRST (the documented order): the back-pointer it
    writes is in place by the time the cater op reads it, so the eligible
    type resolved purely through `subagent_type` gets its sidecar offer (or
    at minimum the miss notice -- never silence)."""
    results = ipc.dispatch_ops_from_hook(
        [
            ("hooks.track_dispatched_agents", _bookkeeping_params()),
            (OP_NAME, _cater_params(str(git_repo))),
        ],
        origin_worktree=str(git_repo),
    )

    assert len(results) == 2
    for result in results:
        assert not isinstance(result, ipc.HookDispatchError), result

    context = _additional_context(results[1])
    assert "sidecar_path: " in context or "sidecar_provisioning: missed" in context, (
        "bookkeeping-first must resolve subagent_type via the back-pointer and cater it"
    )


def test_reversed_order_fails_to_cater_the_named_dispatch(git_repo: Path) -> None:
    """The reverse order -- cater op dispatched BEFORE the bookkeeping leg
    writes the back-pointer -- resolves no subagent_type (lookup-miss,
    fail-open per `_read_backpointer_subagent_type`), so the same eligible
    type gets NEITHER offer nor miss notice. This is the nondeterminism the
    plan names, made deterministic and asserted on directly."""
    results = ipc.dispatch_ops_from_hook(
        [
            (OP_NAME, _cater_params(str(git_repo))),
            ("hooks.track_dispatched_agents", _bookkeeping_params()),
        ],
        origin_worktree=str(git_repo),
    )

    assert len(results) == 2
    for result in results:
        assert not isinstance(result, ipc.HookDispatchError), result

    # Cater op ran first: no back-pointer exists yet, so it resolves
    # neither agent_type nor subagent_type -> not eligible -> no_advisory().
    assert results[0] == {}


def test_sequential_not_concurrent_same_result_regardless_of_python_scheduling(
    git_repo: Path,
) -> None:
    """dispatch_ops_from_hook awaits each op to completion before starting the
    next (module contract, not an implementation detail) -- run the
    bookkeeping-first case twice and require byte-identical additionalContext,
    ruling out a race that only sometimes reads the back-pointer."""
    first = ipc.dispatch_ops_from_hook(
        [
            ("hooks.track_dispatched_agents", _bookkeeping_params()),
            (OP_NAME, _cater_params(str(git_repo))),
        ],
        origin_worktree=str(git_repo),
    )
    second = ipc.dispatch_ops_from_hook(
        [
            ("hooks.track_dispatched_agents", _bookkeeping_params()),
            (OP_NAME, _cater_params(str(git_repo))),
        ],
        origin_worktree=str(git_repo),
    )

    assert _additional_context(first[1]) == _additional_context(second[1])


# test_module_is_eagerly_imported_by_the_hooks_package lived here. It asserted
# list membership for this ONE module; that is now covered for EVERY hooks
# module declaring @register_op by
# tests/test_eager_hook_modules_covers_every_register_op.py, which additionally
# resolves the declarations from source with `ast` rather than relying on this
# file's own module-scope import. Deleted rather than left alongside it: two
# guards over the same invariant drift, and the narrower one goes.
