"""ASSERTION and EXECUTION legs for distill.scope's `run_git` site in the composition gate
(`_LEGITIMIZED_SITES`, keyed `"distill.scope"`, in test_no_uncounted_spawn_on_budgeted_path.py).

The site is `run_watermark.changed_since`'s git fallback: with a watermark and no example-retrieval-repo
doc_files view, it issues exactly ONE `git diff --name-only` per run, never one per file.
The counter patches `subprocess.Popen` itself (seam-independent), so a hidden extra spawn shows.
"""
from __future__ import annotations

import subprocess

import pytest

from coordinator_core import run_watermark
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed

pytestmark = [pytest.mark.spawns_process]


def _repo(tmp_path):
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("a\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    for name in ("b.md", "c.md", "d.md"):
        (tmp_path / "docs" / name).write_text(name)
    git("add", ".")
    git("commit", "-qm", "more")
    return base


def test_git_fallback_issues_exactly_one_spawn_for_many_changed_files(tmp_path, monkeypatch):
    base = _repo(tmp_path)
    monkeypatch.setattr(run_watermark, "registry_get", lambda key: None)
    wm = run_watermark.Watermark("distill", base)
    with _count_spawns_attributed(monkeypatch) as spawns:
        changed = run_watermark.changed_since(tmp_path, wm, scope_dirs=["docs"])
    assert sorted(changed) == ["docs/b.md", "docs/c.md", "docs/d.md"]
    assert len(spawns) == 1
    assert spawns[0].origin == "run_git"


def test_first_run_spawns_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(run_watermark, "registry_get", lambda key: None)
    with _count_spawns_attributed(monkeypatch) as spawns:
        run_watermark.changed_since(tmp_path, None, scope_dirs=["docs"])
    assert spawns == []
