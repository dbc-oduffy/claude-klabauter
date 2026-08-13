"""Regression coverage for chunk C7b of
docs/plans/2026-08-12-message-text-stops-naming-an-unreachable-repo.md.

Purpose: pin that `gen_settings_hooks.py` and `prereq_probe.py` no longer
print prose naming an unreachable private repo (`example-doctrine-repo`,
`example-retrieval-repo-ue-addon`) at a reader who cannot navigate to it on a
published OSS mirror — while their functional identifiers (env var names,
registry keys, `EXAMPLE_GAME_REPO_UE_ROOT`, etc.) are untouched.

Spec backlink: docs/plans/2026-08-12-message-text-stops-naming-an-unreachable-repo.md § C7b
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.install import gen_settings_hooks, prereq_probe


def test_gen_settings_hooks_remediation_does_not_name_example_doctrine_repo(tmp_path: Path):
    out_path = tmp_path / "settings.json"
    (tmp_path / ".coordinator-hooks-enabled").touch()

    try:
        gen_settings_hooks.generate(
            out_path=str(out_path),
            coordinator_root_override="/definitely/does/not/exist",
        )
    except gen_settings_hooks.GenSettingsHooksError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected GenSettingsHooksError for a nonexistent coordinator root")

    assert "example-doctrine-repo" not in message
    assert "coordinator-claude repo is cloned" in message


def test_gen_settings_hooks_usage_text_keeps_functional_env_key():
    usage = gen_settings_hooks._usage_text()
    # Functional identifiers (env var name + registry key) stay untouched.
    assert "REPO_EXAMPLE_DOCTRINE_REPO" in usage
    assert "repos.example_doctrine_repo" in usage


def test_probe_git_lfs_remediation_does_not_name_example_retrieval_repo_ue_addon():
    line = prereq_probe.probe_git_lfs()
    assert "example-retrieval-repo-ue-addon" not in line


def test_probe_ue_functional_env_key_untouched():
    # EXAMPLE_GAME_REPO_UE_ROOT is a functional env-var identifier, not narrative
    # prose naming a repo — it must survive the sweep unchanged.
    line = prereq_probe.probe_ue()
    assert "EXAMPLE_GAME_REPO_UE_ROOT" in line
