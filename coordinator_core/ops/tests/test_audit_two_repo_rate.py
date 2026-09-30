"""Contract pins for `goal.kr2_two_repo_rate` (docs/plans/2026-07-20-kr-baselining-package.md § Design)."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import audit_two_repo_rate as mod
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SINCE = "2026-03-10"
UNTIL = "2026-03-20"
IN_WINDOW = "2026-03-15T12:00:00+00:00"


def _git(repo: Path, *args: str, when: str | None = None) -> str:
    env = dict(os.environ)
    if when:
        env["GIT_AUTHOR_DATE"] = when
        env["GIT_COMMITTER_DATE"] = when
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
        **no_console_creationflags(),
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout.strip()


def _init(repo: Path) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _commit(repo: Path, files: dict[str, str], when: str = IN_WINDOW) -> None:
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", "c " + ",".join(files), when=when)


def _run(repo: Path, **params) -> dict:
    params.setdefault("since", SINCE)
    params.setdefault("until", UNTIL)
    return asyncio.run(mod._handler({"repo_root": str(repo), **params}))


def test_counts_engine_paths_and_ignores_docs_only(tmp_path):
    repo = _init(tmp_path / "r")
    _commit(repo, {"coordinator_core/x.py": "1"})
    _commit(repo, {"bin/y": "1"})
    _commit(repo, {"docs/z.md": "1"})
    _commit(repo, {"coordinator_core/a.py": "1", "docs/b.md": "1"})
    out = _run(repo)
    assert out["exit_code"] == 0
    assert out["record"]["engine_tool_commits"] == 3


def test_merge_commit_not_counted_merged_change_counted_once(tmp_path):
    repo = _init(tmp_path / "r")
    _commit(repo, {"docs/base.md": "1"})
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, {"coordinator_core/side.py": "1"})
    _git(repo, "checkout", "-q", "main")
    _commit(repo, {"docs/main.md": "1"})
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side", when=IN_WINDOW)
    out = _run(repo)
    assert out["exit_code"] == 0
    assert out["record"]["engine_tool_commits"] == 1


def test_window_bounds_inclusive_and_outside_excluded(tmp_path):
    repo = _init(tmp_path / "r")
    _commit(repo, {"coordinator_core/before.py": "1"}, when="2026-03-09T23:59:00+00:00")
    _commit(repo, {"coordinator_core/lo.py": "1"}, when="2026-03-10T00:00:00+00:00")
    _commit(repo, {"coordinator_core/mid.py": "1"})
    _commit(repo, {"coordinator_core/hi.py": "1"}, when="2026-03-20T23:59:00+00:00")
    _commit(repo, {"coordinator_core/after.py": "1"}, when="2026-03-21T00:01:00+00:00")
    out = _run(repo)
    assert out["record"]["engine_tool_commits"] == 3
    assert out["record"]["window"] == {"since": SINCE, "until": UNTIL}


def test_shallow_clone_refused(tmp_path):
    src = _init(tmp_path / "src")
    _commit(src, {"coordinator_core/a.py": "1"})
    _commit(src, {"coordinator_core/b.py": "2"})
    dst = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "--depth", "1", f"file://{src}", str(dst))
    out = _run(dst)
    assert out["exit_code"] == 2
    assert out["error"] == "shallow-history"
    assert "record" not in out


def test_non_repo_directory_refused(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    out = _run(plain)
    assert out["error"] == "not-a-git-repo"
    assert "record" not in out


def test_pairing_leg_unmeasured_and_nulls(tmp_path):
    repo = _init(tmp_path / "r")
    _commit(repo, {"coordinator_core/a.py": "1"})
    record = _run(repo)["record"]
    assert record["pairing"]["status"] == "unmeasured"
    assert record["pairing"]["evidence_source"] is None
    assert record["paired_commits"] is None
    assert record["single_repo_rate"] is None


def test_schema_and_prefixes_echoed(tmp_path):
    repo = _init(tmp_path / "r")
    _commit(repo, {"coordinator_core/a.py": "1"})
    record = _run(repo)["record"]
    assert record["engine_path_prefixes"] == ["coordinator_core/", "bin/"]
    assert record["schema"] == "kr2-two-repo-rate/v1"
