"""coordinator_core/ops/tests/test_plan_gated_criteria_met.py — "plan.gated_criteria_met"."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops import plan_gated_criteria_met as mod
from coordinator_core.ops.ceremony import git_native

PLAN = """---
title: t
status: executing
gated_exit_criteria:
  - brightline: multi-os-first-class
    statement: >-
      works everywhere
    met: false
  - brightline: no-single-machine-assumptions
    statement: s
    met: false
other: 1
---
body
"""


def _git(root, *a):
    subprocess.run(["git", *a], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "a@b")
    _git(tmp_path, "config", "user.name", "a")
    (tmp_path / "docs/plans").mkdir(parents=True)
    (tmp_path / "docs/plans/p.md").write_text(PLAN)
    (tmp_path / "x.txt").write_text("x")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    commits = []

    def fake_commit(rel, content, msg, cwd, **kw):
        commits.append(rel)
        return git_native.GitResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(git_native, "commit_authored_content", fake_commit)
    monkeypatch.setattr(mod, "main_worktree_root", lambda r: r)
    return tmp_path, commits


def _sha(root):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()


def _run(root, rows):
    return mod._handler({"plan": "docs/plans/p.md", "rows": rows}, root)


def test_flip_sets_met_and_evidence_and_commits(repo):
    root, commits = repo
    sha = _sha(root)
    t0 = time.process_time()
    r = _run(root, [{"brightline": "multi-os-first-class", "evidence": sha[:12]},
                    {"brightline": "no-single-machine-assumptions", "evidence": "x.txt"}])
    assert r["committed"] and commits == ["docs/plans/p.md"]
    text = (root / "docs/plans/p.md").read_text()
    assert f"    met: true\n    evidence: {sha[:12]}\n" in text or f"evidence: '{sha[:12]}'" in text
    assert text.count("met: true") == 2 and "met: false" not in text
    assert "evidence: x.txt" in text and "other: 1\n---\nbody" in text
    assert time.process_time() - t0 < 1.0


def test_unknown_brightline_refused_nothing_written(repo):
    root, commits = repo
    with pytest.raises(ValueError, match="unknown brightline: nope"):
        _run(root, [{"brightline": "nope", "evidence": "x.txt"}])
    assert (root / "docs/plans/p.md").read_text() == PLAN and not commits


@pytest.mark.parametrize("ev", ["missing.txt", "../x", "deadbeefdeadbeef"])
def test_bad_evidence_refused(repo, ev):
    root, commits = repo
    with pytest.raises(ValueError):
        _run(root, [{"brightline": "multi-os-first-class", "evidence": ev}])
    assert (root / "docs/plans/p.md").read_text() == PLAN and not commits


def test_non_ancestor_sha_refused(repo):
    root, _ = repo
    _git(root, "checkout", "-q", "-b", "side")
    (root / "y").write_text("y")
    _git(root, "add", "y")
    _git(root, "commit", "-qm", "side")
    side = _sha(root)
    _git(root, "checkout", "-q", "-")
    with pytest.raises(ValueError, match="not in HEAD's ancestry"):
        _run(root, [{"brightline": "multi-os-first-class", "evidence": side}])


def test_idempotent_rerun_writes_and_commits_nothing(repo):
    root, commits = repo
    rows = [{"brightline": "multi-os-first-class", "evidence": "x.txt"}]
    _run(root, rows)
    before = (root / "docs/plans/p.md").read_text()
    r = _run(root, rows)
    assert r["committed"] is False and r["changed"] == []
    assert (root / "docs/plans/p.md").read_text() == before and len(commits) == 1
