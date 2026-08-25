"""Regression coverage for chunk C7b of
docs/plans/2026-08-12-message-text-stops-naming-an-unreachable-repo.md.

Purpose: pin that `gen_settings_hooks.py` and `prereq_probe.py` no longer
print prose naming an unreachable private repo (`DoE-claude`,
`project-rag-ue-addon`) at a reader who cannot navigate to it on a
published OSS mirror — while their functional identifiers (env var names,
registry keys, `HOLODECK_UE_ROOT`, etc.) are untouched.

Spec backlink: pln-message-text-stops-naming-a-re-5c92dd § C7b
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.install import gen_settings_hooks, prereq_probe


def test_gen_settings_hooks_remediation_does_not_name_doe_claude(tmp_path: Path):
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

    assert "DoE-claude" not in message
    assert "coordinator-claude repo is cloned" in message


def test_gen_settings_hooks_usage_text_keeps_functional_env_key():
    usage = gen_settings_hooks._usage_text()
    # Functional identifiers (env var name + registry key) stay untouched.
    assert "REPO_DOE_CLAUDE" in usage
    assert "repos.doe_claude" in usage


def test_probe_git_lfs_remediation_does_not_name_project_rag_ue_addon():
    line = prereq_probe.probe_git_lfs()
    assert "project-rag-ue-addon" not in line


def test_probe_ue_functional_env_key_untouched(monkeypatch, tmp_path: Path):
    # HOLODECK_UE_ROOT is a functional env-var identifier, not narrative
    # prose naming a repo — it must survive the sweep unchanged.
    #
    # Negative spec: do NOT call probe_ue() bare. Its pass branch reports the
    # engine path and names no env var, so a bare call asserts a string that
    # only appears on hosts WITHOUT Unreal installed — green on CI, red on
    # every developer box that has it. Pin the probe to a deterministic
    # not-found state so the assertion is about the message text, which is
    # what this module covers, and not about the host's UE install.
    monkeypatch.setenv("HOLODECK_UE_ROOT", str(tmp_path / "no-such-ue-root"))
    line = prereq_probe.probe_ue()
    assert "HOLODECK_UE_ROOT" in line
