"""
coordinator_core.ops.tests.test_sizing_resize — the "sizing.resize" writer.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_sizing_resize.py -q
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

import coordinator_core.ops.sizing_resize as resize_mod

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_handler = resize_mod._handler

_GIT_ENV = {"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
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


def _seed(repo: Path, *, tshirt: str, route: str, status: str = "routed") -> Path:
    path = repo / "state" / "sizings" / "20260101-a.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join([
            "schema: sizing-object",
            "intent: Test intent, verbatim.",
            "estimate:",
            f"  tshirt: {tshirt}",
            "  provisional: true",
            f"route: {route}",
            "detents: []",
            "fork: null",
            "xl_exit: null",
            f"status: {status}",
            "premise:",
            "  provenance: read",
            "  evidence: test fixture, no real premise verified",
        ]) + "\n",
        encoding="utf-8",
    )
    return path


def _run(repo: Path, **kw) -> dict:
    params = {"sizing": "state/sizings/20260101-a.yaml", "basis": "scope grew"}
    params.update(kw)
    return _handler(params, repo_root=repo / ".git")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    _init_repo(r)
    return r


def test_s_resized_to_m_routes_plan(repo):
    path = _seed(repo, tshirt="S", route="spec-dispatch")
    res = _run(repo, tshirt="M")
    assert res["exit_code"] == 0 and res["applied"] is True, res
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert doc["estimate"] == {"tshirt": "M", "provisional": True}
    assert doc["route"] == "plan"
    assert doc["em_analysis"]["tshirt_resize_basis"] == "scope grew"


def test_xs_resized_to_s_routes_spec_dispatch(repo):
    path = _seed(repo, tshirt="XS", route="dispatch")
    res = _run(repo, tshirt="S")
    assert res["exit_code"] == 0 and res["applied"] is True, res
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert doc["estimate"]["tshirt"] == "S"
    assert doc["route"] == "spec-dispatch"


def test_same_size_and_route_is_noop(repo):
    path = _seed(repo, tshirt="S", route="spec-dispatch")
    before = path.read_text(encoding="utf-8")
    res = _run(repo, tshirt="S")
    assert res["exit_code"] == 0 and res["applied"] is False, res
    assert path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("status", ["shipped", "declined", "superseded"])
def test_terminal_status_refuses(repo, status):
    path = _seed(repo, tshirt="S", route="spec-dispatch", status=status)
    before = path.read_text(encoding="utf-8")
    res = _run(repo, tshirt="M")
    assert res["exit_code"] == 1 and "terminal" in res["error"], res
    assert path.read_text(encoding="utf-8") == before


def test_empty_basis_refuses(repo):
    _seed(repo, tshirt="S", route="spec-dispatch")
    res = _run(repo, tshirt="M", basis="  ")
    assert res["exit_code"] == 1 and "basis" in res["error"], res


def test_unknown_tshirt_refuses(repo):
    _seed(repo, tshirt="S", route="spec-dispatch")
    assert _run(repo, tshirt="HUGE")["exit_code"] == 1
