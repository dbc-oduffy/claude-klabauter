"""
coordinator_core.subagent_sandbox.tests.test_provision_report_touch_claim --
proves provision_report.py's four `open(doc_path, "x")` sites (session-keyed
nonce path, session-keyed provision_key path, plan-derivable path) now
record a `session.scope.touch()` claim for the dispatching session, closing
the same in-process-writer hole `test_in_process_writer_claim_path.py`
closes for `provision_report._provision` via its own entrypoint.

Fixtures mirror the existing `test_provision_report.py`'s `git_repo` /
`policy_path` conventions (real `git init`'d tmp_path repo, so
`resolve_git_root` behaves exactly as it does against a production
checkout).

Spec backlink: pln-in-process-engine-writers-decl-33016a C2
Module under test: coordinator_core/subagent_sandbox/provision_report.py
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.session import safe_commit_offer
from coordinator_core.session import core as session_core
from coordinator_core.subagent_sandbox.provision_report import _provision
from coordinator_core.subagent_sandbox.provision_report import main as provision_main
from coordinator_core.win_portability import no_console_passthrough_kwargs

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

REPORT_SIDECAR_TYPE = "coordinator:code-reviewer"
PLAN_DERIVABLE_TYPE = "coordinator:docs-checker"
BARE_HEX_AGENT_ID = "abc123def4567890"


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A real, empty git repo rooted at tmp_path (mirrors
    test_provision_report.py's identical fixture)."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True,
        **no_console_passthrough_kwargs(),
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    return tmp_path


@pytest.fixture
def policy_path(tmp_path: Path) -> Path:
    policy = {
        "confined": [],
        "exempt": [],
        "sanctioned_dirs": [],
        "report_sidecar": [REPORT_SIDECAR_TYPE, PLAN_DERIVABLE_TYPE],
    }
    path = tmp_path / "subagent-sandbox-policy.yaml"
    path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    return path


def _payload(
    *,
    agent_id: str = BARE_HEX_AGENT_ID,
    agent_type: str = REPORT_SIDECAR_TYPE,
    session_id: str,
    provision_key: str | None = None,
    plan_path: str | None = None,
) -> dict:
    payload: dict = {"agent_id": agent_id, "agent_type": agent_type, "session_id": session_id}
    if provision_key is not None:
        payload["provision_key"] = provision_key
    if plan_path is not None:
        payload["plan_path"] = plan_path
    return payload


def _run(payload: dict, policy_path: Path, git_root: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> str:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    exit_code = provision_main(["--policy", str(policy_path), "--cwd", str(git_root)])
    assert exit_code == 0
    captured = capsys.readouterr()
    envelope = json.loads(captured.out.splitlines()[0])
    return envelope["report_sidecar"]


# ---------------------------------------------------------------------------
# Each written sidecar reaches compute_offer(session)["safe_paths"] --
# the session whose spawn wrote it must be able to commit it.
# ---------------------------------------------------------------------------

def test_nonce_path_sidecar_reaches_safe_paths(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    session_id = "sess-touch-claim-nonce"
    session_core.init(session_id, cwd=str(git_repo))

    rel_path = _run(_payload(session_id=session_id), policy_path, git_repo, monkeypatch, capsys)
    assert (git_repo / rel_path).is_file()

    offer = safe_commit_offer.compute_offer(session_id, cwd=str(git_repo))
    assert rel_path in offer["safe_paths"], (
        f"safe_paths={offer['safe_paths']!r} orphans={offer['orphans']!r} "
        f"excluded={offer['excluded']!r}"
    )


def test_provision_key_path_sidecar_reaches_safe_paths(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    session_id = "sess-touch-claim-provkey"
    session_core.init(session_id, cwd=str(git_repo))

    rel_path = _run(
        _payload(session_id=session_id, provision_key="myplan.C2"),
        policy_path, git_repo, monkeypatch, capsys,
    )
    assert (git_repo / rel_path).is_file()

    offer = safe_commit_offer.compute_offer(session_id, cwd=str(git_repo))
    assert rel_path in offer["safe_paths"], (
        f"safe_paths={offer['safe_paths']!r} orphans={offer['orphans']!r} "
        f"excluded={offer['excluded']!r}"
    )


def test_plan_derivable_path_sidecar_reaches_safe_paths(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    session_id = "sess-touch-claim-plan-derivable"
    session_core.init(session_id, cwd=str(git_repo))

    rel_path = _run(
        _payload(
            agent_type=PLAN_DERIVABLE_TYPE,
            session_id=session_id,
            plan_path="docs/plans/2026-08-05-some-plan.md",
        ),
        policy_path, git_repo, monkeypatch, capsys,
    )
    assert rel_path == (
        ".coordinator-local/plan-sidecars/2026-08-05-some-plan.docs-check.md"
    )
    assert (git_repo / rel_path).is_file()

    offer = safe_commit_offer.compute_offer(session_id, cwd=str(git_repo))
    assert rel_path in offer["safe_paths"], (
        f"safe_paths={offer['safe_paths']!r} orphans={offer['orphans']!r} "
        f"excluded={offer['excluded']!r}"
    )


# ---------------------------------------------------------------------------
# AC6: no claim call materializes a session dir that did not already exist
# (the phantom-live-peer guard) -- this session's dir is NEVER created here.
# ---------------------------------------------------------------------------

def test_no_claim_when_dispatching_session_dir_absent_no_phantom_peer(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    session_id = "sess-touch-claim-no-init"
    # Deliberately never calling session_core.init(session_id, ...) here.

    rel_path = _run(_payload(session_id=session_id), policy_path, git_repo, monkeypatch, capsys)
    assert (git_repo / rel_path).is_file()

    sdir = session_core.session_dir(session_id, cwd=str(git_repo))
    assert not Path(sdir).exists(), (
        "recording a touch-claim for a session with no pre-existing "
        "session dir materialized a phantom live peer via touch()'s lazy "
        "core.init() -- this is exactly the hazard the guard exists for"
    )
