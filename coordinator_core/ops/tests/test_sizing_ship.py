"""
coordinator_core.ops.tests.test_sizing_ship — the "sizing.ship" applier
(2026-08-13, PM ruling; see coordinator_core/ops/sizing_ship.py docstring).

Spec backlink: PM ruling 2026-08-13 (verbatim in sizing_ship.py module docstring);
debt entry state/debt-backlog/2026-08-13-a-spec-dispatch-sizing-that-never-mints-
5393ec7d7f83.yaml.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_sizing_ship.py -q
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.sizing_ship as ship_mod
from coordinator_core.frontmatter.primitives import read_fm_field_unquoted

# Declared, not excused: this file spawns a real process (git) because the
# property under test is that binary's own behaviour, which no fixture stands
# in for. Mirrors test_sizing_decline.py's own identical rationale.
pytestmark = [pytest.mark.spawns_process]

_handler = ship_mod._handler

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
        "route: spec-dispatch",
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


# ---------------------------------------------------------------------------
# Happy path — legal predecessors ship
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["sized", "routed"])
def test_ships_from_each_legal_predecessor(tmp_path, status):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, "20260101-a.yaml", status=status)

    result = _run(
        {"sizing_path": "state/sizings/20260101-a.yaml"},
        repo_root=repo / ".git",
    )

    assert result["exit_code"] == 0
    assert result["applied"] is True
    text = sizing.read_text(encoding="utf-8")
    assert read_fm_field_unquoted(text, "status") == "shipped"


def test_absolute_sizing_path_accepted(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, "20260101-a.yaml", status="sized")

    result = _run(
        {"sizing_path": str(sizing)},
        repo_root=repo / ".git",
    )
    assert result["exit_code"] == 0
    assert result["applied"] is True


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_already_shipped_is_idempotent_noop(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, "20260101-a.yaml", status="shipped")
    before = sizing.read_text(encoding="utf-8")

    result = _run(
        {"sizing_path": "state/sizings/20260101-a.yaml"},
        repo_root=repo / ".git",
    )

    assert result["exit_code"] == 0
    assert result["applied"] is False
    assert sizing.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_refuses_ship_from_draft(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, "20260101-a.yaml", status="draft")
    before = sizing.read_text(encoding="utf-8")

    result = _run(
        {"sizing_path": "state/sizings/20260101-a.yaml"},
        repo_root=repo / ".git",
    )

    assert result["exit_code"] == 1
    assert "refusing" in result["error"]
    assert sizing.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("status", ["declined", "superseded"])
def test_refuses_ship_over_a_different_terminal_status(tmp_path, status):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, "20260101-a.yaml", status=status)
    before = sizing.read_text(encoding="utf-8")

    result = _run(
        {"sizing_path": "state/sizings/20260101-a.yaml"},
        repo_root=repo / ".git",
    )

    assert result["exit_code"] == 1
    assert "refusing" in result["error"]
    assert sizing.read_text(encoding="utf-8") == before


def test_missing_sizing_path_param_rejected(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    result = _run({}, repo_root=repo / ".git")
    assert result["exit_code"] == 1
    assert "sizing_path" in result["error"]


def test_sizing_path_escaping_state_sizings_rejected(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    outside = repo / "state" / "handoffs" / "not-a-sizing.yaml"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text(_sizing_body(status="sized"), encoding="utf-8")

    result = _run(
        {"sizing_path": "state/handoffs/not-a-sizing.yaml"},
        repo_root=repo / ".git",
    )
    assert result["exit_code"] == 1
    assert "escapes state/sizings/" in result["error"]


def test_sizing_path_not_found_rejected(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "state" / "sizings").mkdir(parents=True, exist_ok=True)

    result = _run(
        {"sizing_path": "state/sizings/does-not-exist.yaml"},
        repo_root=repo / ".git",
    )
    assert result["exit_code"] == 1
    assert "not found on disk" in result["error"]


def test_missing_repo_root_rejected(tmp_path):
    result = _handler({"sizing_path": "x"}, repo_root=None)
    assert result["exit_code"] == 1
    assert "repo_root is required" in result["error"]


# ---------------------------------------------------------------------------
# No other field mutated
# ---------------------------------------------------------------------------


def test_ship_writes_only_status_field(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, "20260101-a.yaml", status="sized")
    before_lines = [
        line for line in sizing.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("status:")
    ]

    result = _run(
        {"sizing_path": "state/sizings/20260101-a.yaml"},
        repo_root=repo / ".git",
    )
    assert result["exit_code"] == 0

    after_lines = [
        line for line in sizing.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("status:")
    ]
    assert after_lines == before_lines


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

    assert OP_CLASSIFICATION.get("sizing.ship") is not None
    assert _OP_KEY_SCOPE.get("sizing.ship") == "common_dir"
    assert OP_MODULE_MAP.get("sizing.ship") == "coordinator_core.ops.sizing_ship"
    assert "coordinator_core.ops.sizing_ship" in {m for m, _ in _EAGER_OP_MODULES}
