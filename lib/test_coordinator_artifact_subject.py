"""test_coordinator_artifact_subject.py — table-driven tests for
coordinator_artifact_subject.

Purpose: verifies the subject-matter classifier against the required test
surface from the W2.2 spec — which tri-plane subject a given artifact path
belongs to (doctrine / engine / cross-cutting), plus the rc and stderr
contract each verdict carries.

Port of: coordinator-artifact-subject.test.sh (DoE 6fb5fb37, 2026-07-22).
Retargets the SUT from the deleted bash lib to the Python port; assertions
are unchanged (same cases, same expected token/rc pairs).

COLLECTABILITY (2026-07-28). This file was named
`coordinator-artifact-subject.test.py` and held its 22 cases in a
hand-rolled `main()` under `if __name__ == "__main__"` until 2026-07-28 —
structurally uncollectable for two INDEPENDENT reasons: the dotted stem
made it un-importable as a module, and `coordinator/lib` was not in
`testpaths`. Same residue class the 2026-07-25 migration (3e818e6b)
cleared out of `coordinator/bin`; that migration's scope never reached
`coordinator/lib`. Both reasons are now fixed — renamed to the `test_*.py`
convention, and `coordinator/lib` admitted to `testpaths` in
pyproject.toml. The 22 cases are converted 1:1 to collected test nodes;
case text, paths, and expected token/rc pairs are preserved exactly.
The ratchet that keeps this from recurring is
`coordinator/bin/tests/test_zero_test_module_ratchet.py`.

Run: python3 -m pytest coordinator/lib/test_coordinator_artifact_subject.py

Spec backlink: docs/plans/2026-07-04-doe-authoring-repo-build-subject-matter-.md § W2.2 [DEAD-CITATION: plan file never committed to this repo]

NEGATIVE SPEC
    - Does NOT test the CLI wrapper around the classifier — these call
      `coordinator_artifact_subject()` in-process. Argument parsing and
      exit-code plumbing are the caller's surface, not this table's.
    - Does NOT assert exact stderr TEXT for the cross-cutting cases, only
      that remediation stderr is non-empty alongside rc=2. Pinning the
      wording would make every prose edit a test failure; the contract is
      "the operator is told something", not a specific sentence.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

# Module name has hyphens, so import via importlib rather than a bare `import`.
_SPEC = importlib.util.spec_from_file_location(
    "coordinator_artifact_subject",
    os.path.join(_THIS_DIR, "coordinator-artifact-subject.py"),
)
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)  # type: ignore[union-attr]

coordinator_artifact_subject = _MOD.coordinator_artifact_subject


# ---------------------------------------------------------------------------
# (description, path, expected_stdout, expected_rc) — the classification table.
# Grouping comments preserve the original suite's section headers so a reader
# can still see which spec requirement each block discharges.
#
# The third element is the SUT's stdout, not a separate token:
# `coordinator_artifact_subject` returns (stdout, stderr, returncode), so the
# "empty stdout" case below asserts exactly what its description claims.
# ---------------------------------------------------------------------------
_CLASSIFICATION_CASES = [
    # --- Install-chain collision (two distinct cases required by spec) ---
    ("claude-klabauter install script -> engine", "claude-klabauter/install.sh", "engine", 0),
    (
        "coordinator commands/install.md -> doctrine",
        "plugins/coordinator-claude/coordinator/commands/install.md",
        "doctrine",
        0,
    ),
    # --- Claude-Klabauter-addressed memo -> engine ---
    (
        "to-claude-klabauter memo -> engine",
        "state/memos/to-claude-klabauter-pcore-roadmap-q3.md",
        "engine",
        0,
    ),
    # --- Cross-cutting: DR-207-shaped artifact ---
    (
        "DR-207-shaped artifact -> cross-cutting + rc=2",
        "docs/decisions/DR-207-tri-plane-contract-boundary.md",
        "cross-cutting",
        2,
    ),
    # --- Cross-cutting: fleet-spine emitter-binding plan ---
    (
        "fleet-spine emitter-binding plan -> cross-cutting + rc=2",
        "docs/plans/2026-07-04-fleet-spine-emitter-binding.md",
        "cross-cutting",
        2,
    ),
    # --- Clear doctrine cases (at least one required by spec) ---
    (
        "skills/** file -> doctrine",
        "plugins/coordinator-claude/coordinator/skills/handoff/SKILL.md",
        "doctrine",
        0,
    ),
    (
        "docs/wiki/** plan -> doctrine",
        "docs/plans/2026-07-04-update-state-placement-law-wiki.md",
        "doctrine",
        0,
    ),
    (
        "hooks/ file -> doctrine",
        "plugins/coordinator-claude/coordinator/hooks/block-blanket-git-add.sh",
        "doctrine",
        0,
    ),
    ("CLAUDE.md -> doctrine", "CLAUDE.md", "doctrine", 0),
    (
        "agents/ file -> doctrine",
        "plugins/coordinator-claude/coordinator/agents/code-reviewer.md",
        "doctrine",
        0,
    ),
    # --- Clear engine cases (at least one required by spec) ---
    (
        "coordinator_core/** -> engine",
        "coordinator_core/session_control/dispatcher.py",
        "engine",
        0,
    ),
    ("pcore roadmap -> engine", "docs/plans/2026-07-04-pcore-roadmap-q3.md", "engine", 0),
    (
        "resident-service research -> engine",
        "docs/research/2026-07-04-resident-service-latency-model.md",
        "engine",
        0,
    ),
    (
        "mcp-server research -> engine",
        "docs/research/2026-07-03-mcp-server-load-policy.md",
        "engine",
        0,
    ),
    # Review: code-reviewer F1 — coordinator-doctrine mcp-server path must NOT
    # misclassify as engine; Phase 2 pre-emption in coordinator_artifact_subject
    # routes coordinator-plugin and docs/wiki mcp-server paths to doctrine
    # before the narrowed engine MCP pattern fires.
    (
        "coordinator-plugin mcp-server wiki -> doctrine (not engine)",
        "plugins/coordinator-claude/coordinator/docs/wiki/mcp-server-configuration.md",
        "doctrine",
        0,
    ),
    (
        "docs/wiki mcp-server -> doctrine (not engine)",
        "docs/wiki/mcp-server-configuration.md",
        "doctrine",
        0,
    ),
    # --- Install-chain edge cases (slug variants) ---
    (
        "skills/repo-setup -> doctrine",
        "plugins/coordinator-claude/coordinator/skills/repo-setup/SKILL.md",
        "doctrine",
        0,
    ),
    (
        "claude-klabauter-install slug -> engine",
        "docs/plans/2026-07-04-claude-klabauter-install-chain-rewire.md",
        "engine",
        0,
    ),
    # --- Usage error (rc=1 contract) ---
    # Review: code-reviewer F3 — no-arg / empty-arg usage-error path was
    # untested; consumers (W2.3 coordinator_state_root) rely on rc=1 to
    # detect bad calls.
    ("empty arg -> rc=1, empty stdout", "", "", 1),
]

# Cross-cutting verdicts must ALSO emit remediation stderr — a bare rc=2 with
# a silent stderr leaves the operator with a rejection and no next step.
_STDERR_REQUIRED_CASES = [
    (
        "DR-207-shaped artifact emits stderr remediation",
        "docs/decisions/DR-207-tri-plane-contract-boundary.md",
    ),
    (
        "fleet-spine emitter-binding plan emits stderr remediation",
        "docs/plans/2026-07-04-fleet-spine-emitter-binding.md",
    ),
]


@pytest.mark.parametrize(
    "path,expected_stdout,expected_rc",
    [(p, out, rc) for _desc, p, out, rc in _CLASSIFICATION_CASES],
    ids=[desc for desc, _p, _out, _rc in _CLASSIFICATION_CASES],
)
def test_classification(path: str, expected_stdout: str, expected_rc: int) -> None:
    """Each spec case classifies to its expected stdout verdict and rc."""
    got_stdout, _stderr, got_rc = coordinator_artifact_subject(path)
    assert (got_stdout, got_rc) == (expected_stdout, expected_rc), (
        f'path="{path}" expected stdout="{expected_stdout}" rc={expected_rc}; '
        f'got stdout="{got_stdout}" rc={got_rc}'
    )


@pytest.mark.parametrize(
    "path",
    [p for _desc, p in _STDERR_REQUIRED_CASES],
    ids=[desc for desc, _p in _STDERR_REQUIRED_CASES],
)
def test_cross_cutting_emits_stderr_remediation(path: str) -> None:
    """A cross-cutting verdict carries rc=2 AND non-empty remediation stderr."""
    _stdout, stderr, rc = coordinator_artifact_subject(path)
    assert rc == 2, f'path="{path}" expected rc=2, got rc={rc}'
    assert stderr, f'path="{path}" rc=2 but stderr was empty'


def test_empty_arg_usage_error_names_the_function() -> None:
    """An empty path is a usage error (rc=1) whose stderr names the caller.

    Review: code-reviewer F3 — consumers (W2.3 coordinator_state_root) rely
    on rc=1 to detect bad calls, and on the name to locate the bad call.
    """
    _stdout, stderr, rc = coordinator_artifact_subject("")
    assert rc == 1, f"expected rc=1 for empty arg, got rc={rc}"
    assert "coordinator_artifact_subject" in stderr, (
        f'stderr does not name the function: "{stderr}"'
    )
