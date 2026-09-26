from __future__ import annotations

import pytest

from coordinator_core.artifact_subject import (
    CrossCuttingArtifact,
    Subject,
    classify,
    classify_or_raise,
    remediation_message,
)


def test_claude_klabauter_install_script_is_engine():
    assert classify("claude-klabauter/install.sh") == Subject.ENGINE


def test_coordinator_commands_install_is_doctrine():
    assert (
        classify("plugins/coordinator-claude/coordinator/commands/install.md")
        == Subject.DOCTRINE
    )


def test_to_claude_klabauter_memo_is_engine():
    assert (
        classify("state/memos/to-claude-klabauter-pcore-roadmap-q3.md")
        == Subject.ENGINE
    )


def test_dr207_shaped_is_cross_cutting():
    path = "docs/decisions/DR-207-tri-plane-contract-boundary.md"
    assert classify(path) == Subject.CROSS_CUTTING


def test_dr207_shaped_raises_with_remediation():
    path = "docs/decisions/DR-207-tri-plane-contract-boundary.md"
    with pytest.raises(CrossCuttingArtifact) as exc_info:
        classify_or_raise(path)
    assert exc_info.value.path == path
    assert exc_info.value.message == remediation_message(path)
    assert exc_info.value.message


def test_dr207_mixed_case_is_not_cross_cutting():
    # re.IGNORECASE. Falls through to the doctrine default.
    path = "docs/decisions/Dr-207-tri-plane-contract-boundary.md"
    assert classify(path) == Subject.DOCTRINE


def test_fleet_spine_emitter_binding_is_cross_cutting():
    path = "docs/plans/2026-07-04-fleet-spine-emitter-binding.md"
    assert classify(path) == Subject.CROSS_CUTTING


def test_fleet_spine_emitter_binding_raises_with_remediation():
    path = "docs/plans/2026-07-04-fleet-spine-emitter-binding.md"
    with pytest.raises(CrossCuttingArtifact) as exc_info:
        classify_or_raise(path)
    assert exc_info.value.message


@pytest.mark.parametrize(
    "path",
    [
        "plugins/coordinator-claude/coordinator/skills/handoff/SKILL.md",
        "docs/plans/2026-07-04-update-state-placement-law-wiki.md",
        "plugins/coordinator-claude/coordinator/hooks/block-blanket-git-add.sh",
        "CLAUDE.md",
        "plugins/coordinator-claude/coordinator/agents/code-reviewer.md",
    ],
)
def test_clear_doctrine_cases(path):
    assert classify(path) == Subject.DOCTRINE


@pytest.mark.parametrize(
    "path",
    [
        "coordinator_core/session_control/dispatcher.py",
        "docs/plans/2026-07-04-pcore-roadmap-q3.md",
        "docs/research/2026-07-04-resident-service-latency-model.md",
        "docs/research/2026-07-03-mcp-server-load-policy.md",
    ],
)
def test_clear_engine_cases(path):
    assert classify(path) == Subject.ENGINE


def test_coordinator_plugin_mcp_server_wiki_is_doctrine_not_engine():
    path = (
        "plugins/coordinator-claude/coordinator/docs/wiki/mcp-server-configuration.md"
    )
    assert classify(path) == Subject.DOCTRINE


def test_docs_wiki_mcp_server_is_doctrine_not_engine():
    assert classify("docs/wiki/mcp-server-configuration.md") == Subject.DOCTRINE


def test_skills_repo_setup_is_doctrine():
    assert (
        classify("plugins/coordinator-claude/coordinator/skills/repo-setup/SKILL.md")
        == Subject.DOCTRINE
    )


def test_claude_klabauter_install_slug_is_engine():
    assert (
        classify("docs/plans/2026-07-04-claude-klabauter-install-chain-rewire.md")
        == Subject.ENGINE
    )


def test_empty_path_raises_value_error():
    with pytest.raises(ValueError, match="coordinator_artifact_subject"):
        classify("")


def test_empty_path_raises_value_error_via_classify_or_raise():
    with pytest.raises(ValueError, match="coordinator_artifact_subject"):
        classify_or_raise("")
