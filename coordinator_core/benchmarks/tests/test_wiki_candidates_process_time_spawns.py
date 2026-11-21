"""Pins for the wiki-candidates bench: spawn kinds, baseline loader, verdict, exit codes."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.benchmarks import wiki_candidates_process_time as bench

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SUFFIX = "wiki_update_candidates"
_OP = f"ceremony.{_SUFFIX}"


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo, capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return res.stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_PATHS_UPDATE_DOCS_WORKLIST_CACHE", str(tmp_path / "cache"))
    _git(root, "init", "-q", "-b", "main")
    _write(root, "docs" + "/wiki/alpha.md", "# alpha\n\nSee `src/a.py`.\n")
    _write(
        root, "docs/plans/2026-11/p1.md",
        "---\ntitle: p1\nstatus: landed\n---\n\n# p1\n\nbody\n\n"
        "```yaml plan-tasks\n- id: C1\n  writes:\n    - src/a.py\n  body: x\n```\n",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "c")
    return root


def test_unknown_sha_is_refused(repo) -> None:
    with pytest.raises(RuntimeError):
        bench.load_op_module(repo, _OP, "0" * 40)
