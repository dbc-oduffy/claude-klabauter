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
from coordinator_core import hooks as hooks_pkg
from coordinator_core.win_portability import no_console_passthrough_kwargs
from coordinator_core.hooks.cater_subagent_start import (
    OP_NAME,
    SIDECAR_MISS_MARKER,
    SIDECAR_MISS_NOTICE_LEAD,
    SIDECAR_PATH_MARKER_PREFIX,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

ELIGIBLE_TYPE = "coordinator:code-reviewer"
#: _canonical_agent_id / _BARE_HEX_RE, and passed through UNCHANGED by both
AGENT_ID = "abcdef0123456789"
SESSION_ID = "session-relay-1"


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
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


def _bookkeeping_params() -> dict:
    return {
        "session_id": SESSION_ID,
        "dispatched_agent_id": AGENT_ID,
        "dispatched_model": "claude-x",
        "subagent_type": ELIGIBLE_TYPE,
    }


def _cater_params(cwd: str) -> dict:
    return {
        "agent_id": AGENT_ID,
        "session_id": SESSION_ID,
        "cwd": cwd,
    }


def _additional_context(result: dict) -> str:
    return result.get("hookSpecificOutput", {}).get("additionalContext", "")


def test_cater_op_registered_under_documented_name_alongside_bookkeeping() -> None:
    """Both ops must be reachable off the SAME `ipc._REGISTRY` the
    SubagentStart shim's `dispatch_ops_from_hook` call resolves against --
    this op takes no separate hooks.json registration of its own.

    `_eager_import_all()` first, deliberately. Lazy is the only mode since
    2026-08-22 (`hooks/__init__.py`: "a bare `import coordinator_core.hooks`
    never does" register anything), so `_REGISTRY` holds a hooks op only
    after something imports its module -- this file's module-scope import
    for `cater_subagent_start`, and NOTHING for `track_dispatched_agents`.
    Read cold, this asserted a registration no code in this module causes.
    It passed anyway because the dispatch tests below reach the same op
    through `ipc`'s registry-miss fallback, which imports and registers it
    as a side effect -- so the assertion held on leftover state from an
    earlier test, in this file or another, and failed the moment this file
    ran alone. Forcing the registration path is what makes the pin measure
    its own subject: both ops register under their documented names when
    the documented full-load routine runs. Every production reader of
    `_REGISTRY` (`op_census/occupancy_scan.py`, `op_census/spawn_bearing_
    ops.py`) eagerly imports before reading it for the same reason."""
    hooks_pkg._eager_import_all()

    assert OP_NAME == "hooks.cater_subagent_start"
    assert ipc._REGISTRY.get(OP_NAME) is not None
    assert ipc._REGISTRY.get("hooks.track_dispatched_agents") is not None


def test_bookkeeping_first_caters_the_named_dispatch_via_backpointer(git_repo: Path) -> None:
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
    type gets no sidecar OFFER. This is the nondeterminism the plan names,
    made deterministic and asserted on directly.

    It no longer gets silence. This test asserted `results[0] == {}` until
    `2c6783315` ("the sidecar miss marker fires for a named dispatch whose
    type never resolved") deliberately replaced that silence with a miss
    notice, and never followed. `AGENT_ID` here is bare-hex and
    `_cater_params` carries no `agent_type`, so both type legs come back
    empty and `_resolve_sidecar_leg`'s nothing-resolved arm takes the
    no-path body -- no name leg exists to key a sentinel off, so
    `SIDECAR_MISS_MARKER` (not the path key) is the right observable for
    THIS population, unlike the named-dispatch tests in
    `test_named_dispatch_catering_resolves.py`."""
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

    context = _additional_context(results[0])
    assert SIDECAR_MISS_NOTICE_LEAD in context, context
    assert SIDECAR_MISS_MARKER in context, context
    assert SIDECAR_PATH_MARKER_PREFIX not in context, (
        "no name leg resolved, so no sentinel is derivable -- a path here "
        "would name a file nothing wrote"
    )


def test_sequential_not_concurrent_same_result_regardless_of_python_scheduling(
    git_repo: Path,
) -> None:
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

