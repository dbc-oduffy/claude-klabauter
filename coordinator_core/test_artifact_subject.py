"""Table-driven parity tests for coordinator_core.artifact_subject, mirroring
the bash oracle case-for-case.

Port of: coordinator-artifact-subject.test.sh (example-doctrine-repo 6fb5fb37, 2026-07-22)
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md
"""
from __future__ import annotations

import pytest

from coordinator_core.artifact_subject import (
    CrossCuttingArtifact,
    Subject,
    classify,
    classify_or_raise,
    remediation_message,
)


# ---------------------------------------------------------------------------
# Install-chain collision disambiguation
# ---------------------------------------------------------------------------
def test_claude_klabauter_install_script_is_engine():
    assert classify("claude-klabauter/install.sh") == Subject.ENGINE


def test_coordinator_commands_install_is_doctrine():
    assert (
        classify("plugins/coordinator/commands/install.md")
        == Subject.DOCTRINE
    )


# ---------------------------------------------------------------------------
# Claude-Klabauter-addressed memo
# ---------------------------------------------------------------------------
def test_to_claude_klabauter_memo_is_engine():
    assert (
        classify("state/memos/to-claude-klabauter-pcore-roadmap-q3.md")
        == Subject.ENGINE
    )


# ---------------------------------------------------------------------------
# Cross-cutting
# ---------------------------------------------------------------------------
def test_dr207_shaped_is_cross_cutting():
    path = "docs/decisions/DR-207-tri-plane-contract-boundary.md"
    assert classify(path) == Subject.CROSS_CUTTING


def test_dr207_shaped_raises_with_remediation():
    path = "docs/decisions/DR-207-tri-plane-contract-boundary.md"
    with pytest.raises(CrossCuttingArtifact) as exc_info:
        classify_or_raise(path)
    assert exc_info.value.path == path
    assert exc_info.value.message == remediation_message(path)
    assert exc_info.value.message  # non-empty


def test_dr207_mixed_case_is_not_cross_cutting():
    # Review: code-reviewer -- bash oracle's `case` alternation matches only
    # the literal `DR-207`/`dr-207` casings (no `shopt -s nocasematch`); a
    # third casing like `Dr-207` must NOT match, faithfully reproducing the
    # oracle's limitation rather than silently broadening it via
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
    assert exc_info.value.message  # non-empty


# ---------------------------------------------------------------------------
# Clear doctrine cases
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    [
        "plugins/coordinator/skills/handoff/SKILL.md",
        "docs/plans/2026-07-04-update-state-placement-law-wiki.md",
        "plugins/coordinator/hooks/block-blanket-git-add.sh",
        "CLAUDE.md",
        "plugins/coordinator/agents/code-reviewer.md",
    ],
)
def test_clear_doctrine_cases(path):
    assert classify(path) == Subject.DOCTRINE


# ---------------------------------------------------------------------------
# Clear engine cases
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# MCP-server doctrine pre-emption (must NOT misclassify as engine)
# ---------------------------------------------------------------------------
def test_coordinator_plugin_mcp_server_wiki_is_doctrine_not_engine():
    path = (
        "plugins/coordinator/docs/wiki/mcp-server-configuration.md"
    )
    assert classify(path) == Subject.DOCTRINE


def test_docs_wiki_mcp_server_is_doctrine_not_engine():
    assert classify("docs/wiki/mcp-server-configuration.md") == Subject.DOCTRINE


# ---------------------------------------------------------------------------
# Install-chain edge cases
# ---------------------------------------------------------------------------
def test_skills_repo_setup_is_doctrine():
    assert (
        classify("plugins/coordinator/skills/repo-setup/SKILL.md")
        == Subject.DOCTRINE
    )


def test_claude_klabauter_install_slug_is_engine():
    assert (
        classify("docs/plans/2026-07-04-claude-klabauter-install-chain-rewire.md")
        == Subject.ENGINE
    )


# ---------------------------------------------------------------------------
# Usage-error cases
# ---------------------------------------------------------------------------
def test_empty_path_raises_value_error():
    with pytest.raises(ValueError, match="coordinator_artifact_subject"):
        classify("")


def test_empty_path_raises_value_error_via_classify_or_raise():
    with pytest.raises(ValueError, match="coordinator_artifact_subject"):
        classify_or_raise("")
