"""Default-argument `brief` + `apply` prune merged, zero-unique, non-recent remote branches."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REPO_ROOT = Path(__file__).resolve().parents[3]
OLD_DATE = "2020-01-01T00:00:00Z"
REMOTE_ONLY = "fix/remote-only"
WITH_LOCAL = "fix/with-local"


def _git(cwd: Path, *args: str, env: dict | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        env={**os.environ, **(env or {})}, **no_console_creationflags(),
    ).stdout


def _engine(cwd: Path, *argv: str) -> subprocess.CompletedProcess:
    """Drive `consolidate_assemble.main` (what coordinator/bin/consolidate-assemble.py reaches)."""
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    env.pop("COORDINATOR_OVERRIDE_BRANCH", None)
    code = "import sys; from coordinator_core.consolidate_assemble import main; sys.exit(main(sys.argv[1:]))"
    return subprocess.run(
        [sys.executable, "-c", code, *argv], cwd=str(cwd), capture_output=True, text=True,
        env=env, **no_console_creationflags(),
    )


@pytest.fixture()
def fixture_repo(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    clone = tmp_path / "clone"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "config", "user.email", "t@local")
    _git(clone, "config", "user.name", "t")
    _git(clone, "checkout", "-q", "-B", "main")
    old = {"GIT_COMMITTER_DATE": OLD_DATE, "GIT_AUTHOR_DATE": OLD_DATE}
    (clone / "a.txt").write_text("base\n", encoding="utf-8", newline="\n")
    _git(clone, "add", "a.txt")
    _git(clone, "commit", "-q", "-m", "seed", env=old)
    for name in (REMOTE_ONLY, WITH_LOCAL):
        _git(clone, "branch", name)
    (clone / "b.txt").write_text("next\n", encoding="utf-8", newline="\n")
    _git(clone, "add", "b.txt")
    _git(clone, "commit", "-q", "-m", "next", env=old)
    _git(clone, "push", "-q", "origin", "main", REMOTE_ONLY, WITH_LOCAL)
    _git(clone, "branch", "-D", REMOTE_ONLY)
    _git(clone, "checkout", "-q", "-b", "work/me")
    return origin, clone


def _origin_branches(origin: Path) -> set[str]:
    return set(_git(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads").split())


def test_default_brief_then_apply_prunes_both_remote_branches(fixture_repo) -> None:
    origin, clone = fixture_repo
    assert {REMOTE_ONLY, WITH_LOCAL} <= _origin_branches(origin)

    proc = _engine(clone, "brief")
    assert proc.returncode == 0, proc.stderr
    obj = json.loads(proc.stdout)
    deletes = [d for d in obj["directives"] if d["cli"] == "delete-only" and "origin" in d["args"]]
    for branch in (REMOTE_ONLY, WITH_LOCAL):
        matched = [d for d in deletes if d["args"][0].endswith(branch)]
        assert matched, f"no remote delete-only for {branch}: {obj['directives']}"
        assert all(d["depends_on"] is None for d in matched)

    applied = _engine(clone, "apply")
    assert applied.returncode == 0, applied.stdout + applied.stderr
    remaining = _origin_branches(origin)
    assert REMOTE_ONLY not in remaining
    assert WITH_LOCAL not in remaining
    assert "main" in remaining
