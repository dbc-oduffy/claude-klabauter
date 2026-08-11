"""
coordinator_core.ops.tests.test_sizing_decline — the "sizing.decline" applier
(2026-08-10, docs/plans/2026-08-10-a-terminal-status-for-a-declined-sizing.md § C2).

Spec backlink: docs/plans/2026-08-10-a-terminal-status-for-a-declined-sizing.md § C2, AC3

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_sizing_decline.py -q
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.sizing_decline as decline_mod
from coordinator_core.frontmatter.primitives import read_fm_field_unquoted

# Declared, not excused: this file spawns a real process (git/python) because
# the property under test is that binary's own behaviour, which no fixture
# stands in for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for a new file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.spawns_process]

_handler = decline_mod._handler

_GIT_ENV = {"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    import os
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env={**os.environ, **_GIT_ENV},
        timeout=15, stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # popup-safe-env-suppressed
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _run(params: dict, repo_root: Path) -> dict:
    # `_handler` is a plain `def` (sync dispatch branch) — see its docstring.
    return _handler(params, repo_root=repo_root)


def _sizing_body(*, status: str = "routed") -> str:
    """A schema-valid whole-document sizing-object YAML body."""
    return "\n".join([
        "schema: sizing-object",
        "intent: Test intent, verbatim.",
        "estimate:",
        "  tshirt: M",
        "  provisional: true",
        "route: plan",
        "detents: []",
        "fork: null",
        "xl_exit: null",
        f"status: {status}",
        "premise:",
        "  provenance: read",
        "  evidence: test fixture, no real premise verified",
    ]) + "\n"


def _seed_sizing(repo: Path, name: str, *, status: str = "routed") -> Path:
    path = repo / "state" / "sizings" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_sizing_body(status=status), encoding="utf-8")
    return path


def _seed_dr(repo: Path, name: str = "DR-999-test-decline.md") -> Path:
    path = repo / "docs" / "decisions" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntitle: test\nstatus: accepted\nid: DR-999\n---\n\n# test\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Happy path — routed -> declined
# ---------------------------------------------------------------------------


def test_declines_a_routed_sizing(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, "20260101-a.yaml", status="routed")
    dr = _seed_dr(repo)

    result = _run(
        {"sizing_path": "state/sizings/20260101-a.yaml", "decision_record": "docs/decisions/DR-999-test-decline.md"},
        repo_root=repo / ".git",
    )

    assert result["exit_code"] == 0
    assert result["applied"] is True
    text = sizing.read_text(encoding="utf-8")
    assert read_fm_field_unquoted(text, "status") == "declined"


def test_absolute_sizing_path_accepted(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, "20260101-a.yaml", status="routed")
    dr = _seed_dr(repo)

    result = _run(
        {"sizing_path": str(sizing), "decision_record": str(dr)},
        repo_root=repo / ".git",
    )
    assert result["exit_code"] == 0
    assert result["applied"] is True


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_already_declined_is_idempotent_noop(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, "20260101-a.yaml", status="declined")
    dr = _seed_dr(repo)
    before = sizing.read_text(encoding="utf-8")

    result = _run(
        {"sizing_path": "state/sizings/20260101-a.yaml", "decision_record": "docs/decisions/DR-999-test-decline.md"},
        repo_root=repo / ".git",
    )

    assert result["exit_code"] == 0
    assert result["applied"] is False
    assert sizing.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["draft", "sized", "superseded", "shipped"])
def test_refuses_decline_from_non_routed_status(tmp_path, status):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, "20260101-a.yaml", status=status)
    dr = _seed_dr(repo)
    before = sizing.read_text(encoding="utf-8")

    result = _run(
        {"sizing_path": "state/sizings/20260101-a.yaml", "decision_record": "docs/decisions/DR-999-test-decline.md"},
        repo_root=repo / ".git",
    )

    assert result["exit_code"] == 1
    assert "refusing" in result["error"]
    assert sizing.read_text(encoding="utf-8") == before


def test_missing_sizing_path_param_rejected(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    result = _run({"decision_record": "docs/decisions/DR-999-test-decline.md"}, repo_root=repo / ".git")
    assert result["exit_code"] == 1
    assert "sizing_path" in result["error"]


def test_missing_decision_record_param_rejected(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo, "20260101-a.yaml", status="routed")
    result = _run({"sizing_path": "state/sizings/20260101-a.yaml"}, repo_root=repo / ".git")
    assert result["exit_code"] == 1
    assert "decision_record" in result["error"]


def test_decision_record_must_exist_on_disk(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, "20260101-a.yaml", status="routed")
    before = sizing.read_text(encoding="utf-8")

    result = _run(
        {
            "sizing_path": "state/sizings/20260101-a.yaml",
            "decision_record": "docs/decisions/DR-does-not-exist.md",
        },
        repo_root=repo / ".git",
    )

    assert result["exit_code"] == 1
    assert "does not resolve to a real file" in result["error"]
    assert sizing.read_text(encoding="utf-8") == before


def test_decision_record_outside_docs_decisions_rejected(tmp_path):
    # Review: coordinator-code-reviewer — P2, pins the real-but-wrong-location
    # gap the P1 fix closes (a readable file outside docs/decisions/ must not
    # satisfy the gate).
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, "20260101-a.yaml", status="routed")
    before = sizing.read_text(encoding="utf-8")

    result = _run(
        {
            "sizing_path": "state/sizings/20260101-a.yaml",
            "decision_record": "README.md",
        },
        repo_root=repo / ".git",
    )
    assert result["exit_code"] == 1
    assert "escapes docs/decisions/" in result["error"]
    assert sizing.read_text(encoding="utf-8") == before


def test_decision_record_absolute_path_outside_repo_rejected(tmp_path):
    # Review: coordinator-code-reviewer — P2, absolute-path variant of the same gap.
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, "20260101-a.yaml", status="routed")
    outside = tmp_path / "outside-decision.md"
    outside.write_text("not a real decision record\n", encoding="utf-8")

    result = _run(
        {
            "sizing_path": "state/sizings/20260101-a.yaml",
            "decision_record": str(outside),
        },
        repo_root=repo / ".git",
    )
    assert result["exit_code"] == 1
    assert "escapes docs/decisions/" in result["error"]


def test_decision_record_as_the_declined_sizing_itself_rejected(tmp_path):
    # Review: coordinator-code-reviewer — P2, sharpest variant: the sizing-object
    # being declined passed as its own decision_record must not satisfy the gate.
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, "20260101-a.yaml", status="routed")
    before = sizing.read_text(encoding="utf-8")

    result = _run(
        {
            "sizing_path": "state/sizings/20260101-a.yaml",
            "decision_record": "state/sizings/20260101-a.yaml",
        },
        repo_root=repo / ".git",
    )
    assert result["exit_code"] == 1
    assert "escapes docs/decisions/" in result["error"]
    assert sizing.read_text(encoding="utf-8") == before


def test_sizing_path_escaping_state_sizings_rejected(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_dr(repo)
    outside = repo / "state" / "handoffs" / "not-a-sizing.yaml"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text(_sizing_body(status="routed"), encoding="utf-8")

    result = _run(
        {
            "sizing_path": "state/handoffs/not-a-sizing.yaml",
            "decision_record": "docs/decisions/DR-999-test-decline.md",
        },
        repo_root=repo / ".git",
    )
    assert result["exit_code"] == 1
    assert "escapes state/sizings/" in result["error"]


def test_sizing_path_not_found_rejected(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    _seed_dr(repo)

    result = _run(
        {
            "sizing_path": "state/sizings/does-not-exist.yaml",
            "decision_record": "docs/decisions/DR-999-test-decline.md",
        },
        repo_root=repo / ".git",
    )
    assert result["exit_code"] == 1
    assert "not found on disk" in result["error"]


def test_missing_repo_root_rejected(tmp_path):
    result = _handler({"sizing_path": "x", "decision_record": "y"}, repo_root=None)
    assert result["exit_code"] == 1
    assert "repo_root is required" in result["error"]


# ---------------------------------------------------------------------------
# Registration quad (fifth surface too) — a new op that skips any of the four
# registration tables silently falls out of dispatch; catch it locally rather
# than relying only on the repo-wide drift guard.
# ---------------------------------------------------------------------------


def test_registered_in_all_quad_surfaces():
    from coordinator_core.authz.classification import OP_CLASSIFICATION
    from coordinator_core.op_scopes import _OP_KEY_SCOPE
    from coordinator_core.ops._registry_map import OP_MODULE_MAP
    from coordinator_core.ops import _EAGER_OP_MODULES

    assert OP_CLASSIFICATION.get("sizing.decline") is not None
    assert _OP_KEY_SCOPE.get("sizing.decline") == "common_dir"
    assert OP_MODULE_MAP.get("sizing.decline") == "coordinator_core.ops.sizing_decline"
    assert "coordinator_core.ops.sizing_decline" in {m for m, _ in _EAGER_OP_MODULES}
