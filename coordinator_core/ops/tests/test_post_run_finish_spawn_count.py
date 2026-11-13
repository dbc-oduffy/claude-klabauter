"""Exact-equality spawn count for `post_run.finish`, through the real commit_v2.

The gitignore verdict is computed in process and handed to commit_v2 as `ignored_untracked`, so
the commit runs no `git check-ignore`; the count does not grow with the number of pages. The
figure is read from the budget manifest, never chosen here.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.benchmarks.budget import load_manifest
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops import post_run_finish
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


def _budget() -> dict:
    return load_manifest()["overrides"]["post_run.finish"]["spawn_count_budget"]


@pytest.mark.parametrize("pages", [1, 6])
def test_finishing_a_run_commits_with_the_budgeted_spawns(tmp_path, monkeypatch, pages):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "f").write_text("1")
    _git(tmp_path, "add", "f")
    _git(tmp_path, "commit", "-q", "-m", "init")
    wiki = tmp_path / "docs" / "wiki" / "fam"
    wiki.mkdir(parents=True)
    touched = []
    for i in range(pages):
        (wiki / f"p{i}.md").write_text(f"# Wrong {i}\n\nBody {i}.\n", encoding="utf-8")
        touched.append(f"docs/wiki/fam/p{i}.md")
    ev = tmp_path / "state" / "evidence"
    ev.mkdir(parents=True)
    (ev / "e.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "00000000-0000-4000-8000-000000000000")
    params = {"pipeline": "lessons", "run_id": "r", "touched": touched, "evidence": ["state/evidence/e.json"]}

    with _count_spawns_attributed(monkeypatch) as spawns:
        out = post_run_finish._handler(params, repo_root=tmp_path)

    assert out["exit_code"] == 0 and out["commit"]["committed"] is True, out
    counted = [s for s in spawns if "_machine_local.py" not in " ".join(s.argv)]
    assert len(counted) == sum(_budget().values()), [s.argv for s in counted]
    tracked = _git(tmp_path, "ls-files").splitlines()
    assert "docs/wiki/fam/_index.md" in tracked and "state/evidence/e.json" in tracked
