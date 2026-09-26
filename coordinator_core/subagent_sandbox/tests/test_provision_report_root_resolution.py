"""
coordinator_core.subagent_sandbox.tests.test_provision_report_root_resolution
-- C4 (state/dispatch-briefs/2026-08-21-catering-costs-what-the-work-costs/):
proves the `contract_blocks` assembly leg resolves its snippet registry off
the coordinator-claude PLUGIN's own content root, not the spawning session's
git root.

Two-repo control, not a single-path assertion (this is what found the
defect, per C4's brief): the SAME payload, the SAME agent type, assembled
across a DoE-claude-SHAPED session cwd (where `git_root` and the plugin
content root used to coincide, masking the bug) and an unrelated,
non-DoE-shaped session cwd (every other real dispatch's actual shape) --
both must assemble the identical non-empty `injected_prompt_blocks` string.
Before the fix, only the DoE-shaped cwd composed anything; the other
returned ``None``.

`resolve_git_root` is monkeypatched to an identity stub (returns whatever
`cwd` it's handed) rather than exercised for real -- this test's subject is
the PLUGIN-root resolution axis, not git-root resolution, and a real `git
rev-parse` spawn against synthetic tmp_path trees would just add
noise/flakiness the substitution avoids. `CLAUDE_PLUGIN_ROOT` is
monkeypatched to a synthetic fixture plugin content root carrying a minimal
real `snippets/registry.toml` + one snippet body -- resolution off the env
var, not off any real DoE-claude checkout, keeps this test runnable with no
sibling-repo dependency.

Module under test: coordinator_core/subagent_sandbox/provision_report.py
Spec backlink: state/dispatch-briefs/2026-08-21-catering-costs-what-the-work-costs/C4.md
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.subagent_sandbox import provision_report
from coordinator_core.subagent_sandbox.provision_report import _provision
from coordinator_core.win_portability import no_console_passthrough_kwargs

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SNIPPET_NAME = "fixture-block"

_REGISTRY_TOML = f"""\
schema_version = 3

[snippet.{_SNIPPET_NAME}]
sentinel_begin = "<!-- BEGIN {_SNIPPET_NAME} -->"
sentinel_end = "<!-- END {_SNIPPET_NAME} -->"
consumers = []
delivery = "inject"
header_style = "comment-block"
"""

_SNIPPET_BODY = (
    "<!-- header line 1 -->\n"
    "<!-- header line 2 -->\n"
    "Fixture contract block body.\n"
)


def _make_fixture_plugin_root(tmp_path: Path) -> Path:
    plugin_root = tmp_path / "plugin-root"
    snippets_dir = plugin_root / "snippets"
    snippets_dir.mkdir(parents=True)
    (snippets_dir / "registry.toml").write_text(_REGISTRY_TOML, encoding="utf-8")
    (snippets_dir / f"{_SNIPPET_NAME}.md").write_text(_SNIPPET_BODY, encoding="utf-8")
    return plugin_root


def test_resolve_plugin_root_prefers_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_root = _make_fixture_plugin_root(tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    assert provision_report.resolve_plugin_root() == str(plugin_root)


def test_resolve_plugin_root_returns_none_on_full_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FULL miss means EVERY rung misses -- all three, not the two this test
    originally knew about.

    ``resolve_plugin_root`` grew a third rung (``machine_local_dir()/.doe-root``
    + ``coordinator``, the fleet's dev-clone pointer file) after this test was
    written; the test kept isolating only ``CLAUDE_PLUGIN_ROOT`` and
    ``claude_config_dir()``, so on any box carrying a real ``.doe-root`` the
    unisolated rung resolved a LIVE plugin root (the machine's own DoE-claude
    checkout) and the "full miss" this asserts was never actually constructed.
    Isolate
    each rung the resolver reads, so the scenario under test is the one named.
    """
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(
        provision_report,
        "claude_config_dir",
        lambda: Path("does-not-exist-anywhere"),
        raising=False,
    )
    machine_local = tmp_path / "machine-local"
    machine_local.mkdir()
    monkeypatch.setattr(
        provision_report,
        "machine_local_dir",
        lambda: machine_local,
        raising=False,
    )

    # No CLAUDE_PLUGIN_ROOT, an unresolvable claude_config_dir()-relative
    assert provision_report.resolve_plugin_root() is None


@pytest.mark.parametrize(
    "session_cwd_name",
    ["DoE-claude", "some-unrelated-session-repo"],
)
def test_assemble_contract_blocks_composes_regardless_of_session_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session_cwd_name: str
) -> None:
    plugin_root = _make_fixture_plugin_root(tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    session_cwd = tmp_path / session_cwd_name
    session_cwd.mkdir()
    (session_cwd / ".git").mkdir()

    payload = {
        "session_id": "test-session",
        "agent_type": "coordinator:some-agent",
        "contract_blocks": [_SNIPPET_NAME],
    }

    assembled = provision_report.assemble_contract_blocks_for_payload(
        payload, cwd=str(session_cwd), report_sidecar_path=None
    )

    assert assembled is not None
    assert "Fixture contract block body." in assembled


_TARGET_REPORT_SIDECAR_TYPE = "coordinator:code-reviewer"


def _init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path, check=True, **no_console_passthrough_kwargs(),
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, **no_console_passthrough_kwargs())
    return path


def _write_policy(path: Path) -> Path:
    policy_path = path / "subagent-sandbox-policy.yaml"
    policy_path.write_text(
        yaml.safe_dump(
            {
                "confined": [],
                "exempt": [],
                "sanctioned_dirs": [],
                "report_sidecar": [_TARGET_REPORT_SIDECAR_TYPE],
            }
        ),
        encoding="utf-8",
    )
    return policy_path


def test_provision_refuses_rather_than_guessing_when_cwd_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ambient_repo = _init_git_repo(tmp_path / "ambient-repo")
    policy_path = _write_policy(tmp_path)
    monkeypatch.chdir(ambient_repo)

    payload = {
        "agent_id": "abc123def4567890",
        "agent_type": _TARGET_REPORT_SIDECAR_TYPE,
        "session_id": "sess-k47-missing-cwd",
    }

    for absent_cwd in (None, ""):
        result = _provision(payload, str(policy_path), absent_cwd)
        assert result is None

    share_dir = ambient_repo / ".coordinator-local" / "subagent-share"
    assert not share_dir.exists(), (
        "a falsy cwd must never resolve against this process's ambient cwd -- "
        f"found {share_dir} written under the ambient (non-target) repo"
    )


def test_provision_keys_on_the_explicit_target_repo_not_the_ambient_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ambient_repo = _init_git_repo(tmp_path / "ambient-repo")
    target_repo = _init_git_repo(tmp_path / "target-repo")
    policy_path = _write_policy(tmp_path)
    monkeypatch.chdir(ambient_repo)

    payload = {
        "agent_id": "abc123def4567890",
        "agent_type": _TARGET_REPORT_SIDECAR_TYPE,
        "session_id": "sess-k47-explicit-cwd",
    }

    result = _provision(payload, str(policy_path), str(target_repo))
    assert result is not None
    assert (target_repo / result).is_file()
    assert not (ambient_repo / ".coordinator-local").exists()
