"""A mapped agent type gets a sidecar or a miss notice in a repo with no share dir and no policy env."""

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

AGENT_TYPE = "coordinator:code-reviewer"


@pytest.fixture
def bare_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q"], cwd=repo, check=True, **no_console_passthrough_kwargs()
    )
    for var in ("SUBAGENT_SANDBOX_POLICY", "CLAUDE_PLUGIN_ROOT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    monkeypatch.setattr("coordinator_core.content_root.read_content_root", lambda: "")
    monkeypatch.setattr("coordinator_core.hooks.cater_subagent_start.read_content_root", lambda: "")
    return repo


def _payload(repo: Path) -> dict:
    return {"agent_type": AGENT_TYPE, "session_id": "session-absent-share", "cwd": str(repo)}


def test_no_share_dir_and_no_policy_env_still_provisions_a_sidecar(
    bare_repo: Path, tmp_path: Path
) -> None:
    content = tmp_path / "claude-config" / "plugins" / "coordinator-claude" / "coordinator"
    (content / "snippets").mkdir(parents=True)
    (content / "subagent-sandbox-policy.yaml").write_text(
        f"report_sidecar:\n  - {AGENT_TYPE}\n", encoding="utf-8"
    )
    assert not (bare_repo / "state" / "subagent-share").exists()
    assert not (bare_repo / ".coordinator-local" / "subagent-share").exists()

    result = compose_catering(_payload(bare_repo), cwd=str(bare_repo))

    marker = next(
        line for line in result.splitlines() if line.startswith(SIDECAR_PATH_MARKER_PREFIX)
    )
    sidecar = bare_repo / marker[len(SIDECAR_PATH_MARKER_PREFIX):]
    assert sidecar.is_file()
    assert f"agent_type: {AGENT_TYPE}" in sidecar.read_text(encoding="utf-8")


def test_unresolvable_policy_emits_a_miss_notice_not_silence(bare_repo: Path) -> None:
    result = compose_catering(_payload(bare_repo), cwd=str(bare_repo))

    assert result != ""
    if SIDECAR_PATH_MARKER_PREFIX in result:
        marker = next(
            line for line in result.splitlines() if line.startswith(SIDECAR_PATH_MARKER_PREFIX)
        )
        assert (bare_repo / marker[len(SIDECAR_PATH_MARKER_PREFIX):]).is_file()
    else:
        assert SIDECAR_MISS_MARKER in result


def test_a_loaded_policy_that_omits_the_type_stays_silent(
    bare_repo: Path, tmp_path: Path
) -> None:
    content = tmp_path / "claude-config" / "plugins" / "coordinator-claude" / "coordinator"
    (content / "snippets").mkdir(parents=True)
    (content / "subagent-sandbox-policy.yaml").write_text(
        "report_sidecar:\n  - coordinator:executor\n", encoding="utf-8"
    )

    assert compose_catering(_payload(bare_repo), cwd=str(bare_repo)) == ""
