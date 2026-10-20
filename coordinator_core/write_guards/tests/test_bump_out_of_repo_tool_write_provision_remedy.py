"""bump-out-of-repo-tool-write names `repo.provision_local` in its deny text for a Write into a
provisioned or absent sibling, verdict unchanged, and names nothing for a target outside the
session repo's parent."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.bash_guards import _write_bump_session_start as session_start
from coordinator_core.ops.sibling_repo_provision_contract import MARKER_NAME, ROUTE_REMEDY
from coordinator_core.testing.home_sandbox import sandbox_home
from coordinator_core.tests.git_seed import seeded_repo
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.write_guards import bump_out_of_repo_tool_write as guard

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SESSION = "sess-tool-remedy"


@pytest.fixture()
def world(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    sandbox_home(monkeypatch, root / "home")
    own = seeded_repo(root / "work" / "own", name="Test")
    session_start.write_session_start_record(_SESSION, launch_cwd=str(own))

    provisioned = root / "work" / "provisioned"
    provisioned.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=provisioned, check=True, capture_output=True,
                   **no_console_creationflags())
    (provisioned / ".git" / MARKER_NAME).write_text(json.dumps({"name": "provisioned"}),
                                                   encoding="utf-8")
    plain = seeded_repo(root / "work" / "plain-foreign", name="Test")
    return {"own": own, "provisioned": provisioned, "absent": root / "work" / "absent",
            "plain": plain, "elsewhere": root / "elsewhere"}


def _payload(path, own):
    return {"tool_name": "Write", "tool_input": {"file_path": str(path)}, "session_id": _SESSION,
            "cwd": str(own), "agent_id": ""}


def _reason(result) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def test_write_into_provisioned_target_denies_and_names_route(world):
    result = guard.check(_payload(world["provisioned"] / "a.txt", world["own"]))
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert _reason(result).endswith(ROUTE_REMEDY)


def test_write_into_absent_sibling_names_route_when_it_denies(world):
    from coordinator_core.ops.sibling_repo_provision_contract import remedy_applies

    assert remedy_applies(world["absent"] / "a.txt", world["own"]) is True
    result = guard.check(_payload(world["absent"] / "a.txt", world["own"]))
    if result is not None:
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert _reason(result).endswith(ROUTE_REMEDY)


def test_write_into_unmarked_sibling_repo_has_no_route(world):
    result = guard.check(_payload(world["plain"] / "a.txt", world["own"]))
    assert result is not None
    assert ROUTE_REMEDY not in _reason(result)


def test_write_outside_parent_has_no_route(world):
    world["elsewhere"].mkdir()
    result = guard.check(_payload(world["elsewhere"] / "a.txt", world["own"]))
    if result is not None:
        assert ROUTE_REMEDY not in _reason(result)


def test_remedy_is_text_only_verdict_matches_unremedied(world, monkeypatch):
    payload = _payload(world["provisioned"] / "a.txt", world["own"])
    with_remedy = guard.check(payload)
    monkeypatch.setattr(
        "coordinator_core.ops.sibling_repo_provision_contract.remedy_applies",
        lambda *_a: False,
    )
    without = guard.check(payload)
    assert with_remedy is not None and without is not None
    assert _reason(with_remedy) == _reason(without) + "\n" + ROUTE_REMEDY
    assert with_remedy["hookSpecificOutput"]["permissionDecision"] == (
        without["hookSpecificOutput"]["permissionDecision"]
    )
