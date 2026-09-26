
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.subagent_sandbox.provision_report import _provision
from coordinator_core.ops.session import safe_commit_offer
from coordinator_core.session import core
from coordinator_core.win_portability import no_console_passthrough_kwargs

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

SIDECAR_ELIGIBLE_TYPE = "coordinator:executor"


def _make_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs()
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs()
    )
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    return tmp_path


@pytest.fixture
def policy_path(tmp_path: Path) -> Path:
    path = tmp_path / "subagent-sandbox-policy.yaml"
    path.write_text(
        yaml.safe_dump({"report_sidecar": [SIDECAR_ELIGIBLE_TYPE]}), encoding="utf-8"
    )
    return path


def test_provisioned_subagent_sidecar_is_committable_by_its_own_session(
    tmp_path: Path, policy_path: Path
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    repo = _make_repo(repo_root)
    session_id = "sess-in-process-writer"
    core.init(session_id, cwd=str(repo))
    sdir = Path(core.session_dir(session_id, cwd=str(repo)))
    (sdir / "started_at").write_text("2000-01-01T00:00:00Z")

    sidecar_rel = _provision(
        {"agent_type": SIDECAR_ELIGIBLE_TYPE, "session_id": session_id},
        str(policy_path),
        str(repo),
    )
    assert sidecar_rel is not None, "fixture failure: sidecar was not provisioned"
    assert (repo / sidecar_rel).is_file(), "fixture failure: sidecar not on disk"

    offer = safe_commit_offer.compute_offer(session_id, cwd=str(repo))

    assert sidecar_rel in offer["safe_paths"], (
        "the session whose spawn wrote this sidecar cannot commit it: "
        f"safe_paths={offer['safe_paths']!r} orphans={offer['orphans']!r} "
        f"excluded={offer['excluded']!r}"
    )


def test_provision_does_not_materialize_a_phantom_session_dir(
    tmp_path: Path, policy_path: Path
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    repo = _make_repo(repo_root)
    session_id = "never-spawned-session"

    assert core.session_dir(session_id, cwd=str(repo)) == "" or not Path(
        core.session_dir(session_id, cwd=str(repo))
    ).is_dir(), "fixture failure: session dir already exists before provisioning"

    sidecar_rel = _provision(
        {"agent_type": SIDECAR_ELIGIBLE_TYPE, "session_id": session_id},
        str(policy_path),
        str(repo),
    )
    assert sidecar_rel is not None, "fixture failure: sidecar was not provisioned"
    assert (repo / sidecar_rel).is_file(), "fixture failure: sidecar not on disk"

    assert not Path(core.session_dir(session_id, cwd=str(repo))).is_dir(), (
        "provisioning a sidecar for a never-initialized session must not "
        "materialize a phantom session dir via scope.touch's lazy core.init()"
    )
